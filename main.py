"""장중 메인 루프 — 스냅샷 → (손절 우선) → LLM 판단 → 가드레일 검문 → 주문 → 기록/알림.

의사결정 2단 구조: LLM 판단은 출발점이고, 코드 가드레일을 통과해야만 실제 주문이 나간다.
하드 손절은 LLM을 거치지 않고 즉시 청산한다. 모든 외부 호출은 try/catch로 감싸
무인 실행 중 침묵 실패를 방지하고, 미처리 예외도 디스코드 경고 후 안전 종료한다.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import config
from adapter.broker import Broker, BrokerError
from adapter.market_stream import MarketStream
from notify.discord import DiscordNotifier
from notify.discord_bot import TraderBot
from risk import guardrails
from risk.guardrails import AccountState, GuardrailConfig
from store.journal import Journal
from strategy.context import build_snapshot
from strategy.llm_decider import LLMDecider

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"


def _setup_logging(level_name: str, log_file: str) -> None:
    """콘솔(INFO)과 파일(설정 레벨, 로테이션)을 동시에 설정한다.

    파일 핸들러 생성 실패(권한/경로)는 콘솔 로깅까지 막지 않도록 경고만 남긴다.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 핸들러별로 레벨을 다르게 두기 위해 루트는 최저로
    fmt = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, level_name, logging.DEBUG))
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("파일 로그 초기화 실패(%s) — 콘솔 로깅만 진행: %s", log_file, exc)


logger = logging.getLogger("main")


class Trader:
    def __init__(self) -> None:
        self.settings = config.settings
        _setup_logging(self.settings.log_level, self.settings.log_file)
        self.discord = DiscordNotifier(
            self.settings.discord_webhook_url,
            tradelog_url=self.settings.discord_tradelog_url,
        )
        # broker/stream 경고는 디스코드로 보냄(알림 실패는 흐름을 막지 않음)
        self.broker = Broker(notifier=self.discord.error)
        # 양방향 조회 봇(선택). broker를 공유하되 RLock으로 매매 루프와 직렬화된다.
        self.bot: TraderBot | None = None
        if self.settings.discord_bot_enabled:
            self.bot = TraderBot(
                self.settings.discord_bot_token,
                self.broker,
                allowed_channel_id=self.settings.discord_bot_channel_id,
                is_paper=self.settings.is_paper,
            )
        self.stream = MarketStream(self.settings.watch_codes, notifier=self.discord.error)
        self.decider = LLMDecider(self.settings.anthropic_api_key, self.settings.anthropic_model)
        self.journal = Journal(self.settings.journal_db)
        self.guard_cfg = GuardrailConfig.from_settings(self.settings)

        self._running = True
        self._last_decision_ts = 0.0
        self._recent_order_keys: set[str] = set()

    # ── 수명주기 ──
    def startup(self) -> None:
        self.broker.authenticate()
        self.stream.start()
        # 조회 봇 기동 — 실패해도 매매는 계속(봇은 부가 기능)
        if self.bot is not None:
            try:
                self.bot.run_in_thread()
                logger.info("Discord 조회 봇 스레드 기동")
            except Exception:  # noqa: BLE001 — 봇 기동 실패가 매매를 막지 않게
                logger.exception("Discord 봇 기동 실패 — 매매는 계속 진행")
        self.discord.info("🤖 트레이더 기동", f"환경={self.settings.kis_env}, 관심종목={self.settings.watch_codes}")
        logger.info("기동 완료")

    def shutdown(self, reason: str = "") -> None:
        self._running = False
        if self.bot is not None:
            try:
                self.bot.stop()
            except Exception:  # noqa: BLE001 — 봇 종료 실패가 셧다운을 막지 않게
                logger.exception("Discord 봇 종료 실패")
        try:
            self.stream.stop()
        finally:
            self._daily_summary()
            self.discord.info("🛑 트레이더 종료", reason or "정상 종료")
            self.journal.close()

    def request_stop(self, *_args) -> None:
        logger.info("종료 신호 수신")
        self._running = False

    # ── 가격 조회: 스트림 우선, 없으면 REST ──
    def _price(self, code: str) -> int:
        p = self.stream.latest_price(code)
        if p:
            return p
        try:
            return self.broker.get_price(code)["price"]
        except BrokerError:
            return 0

    def _account_state(self, snapshot: dict, today: str) -> AccountState:
        return AccountState(
            cash=snapshot["cash"],
            available_cash=snapshot["available_cash"],
            total_eval=snapshot["total_eval"],
            positions=snapshot["positions"],
            trades_today=self.journal.count_today_orders(today),
            realized_pnl_today=0,  # TODO: 체결 기반 당일 실현손익 집계로 정교화
            recent_order_keys=set(self._recent_order_keys),
            now=datetime.now(),
        )

    # ── 1) 하드 손절(LLM 무시) ──
    def _run_stop_loss(self, state: AccountState) -> None:
        for d in guardrails.force_stop_loss(state, self.guard_cfg):
            logger.warning("강제 손절: %s", d.reason)
            pos = state.positions.get(d.code, {})
            self._execute(d.code, "sell", d.quantity, reason=d.reason, is_stop=True,
                          pnl_rate=float(pos.get("pnl_rate", 0.0)),
                          name=str(pos.get("name", "")))

    # ── 2) LLM 판단 → 가드레일 → 주문 ──
    def _run_decisions(self, snapshot: dict, state: AccountState) -> None:
        for code in self.settings.watch_codes:
            decision, meta = self.decider.decide(snapshot, code)
            price = self._price(code)
            result = guardrails.check(decision, state, self.guard_cfg, price)

            # journal: LLM 원 결정 + 가드레일 결과
            self.journal.log_decision(
                code=code, action=decision.action, quantity=decision.quantity,
                reason=decision.reason, model=meta.get("model", ""),
                snapshot=meta.get("payload", {}),
                final_action=result.action, final_quantity=result.quantity,
                approved=result.approved, guard_reason=result.reason,
            )

            name = str(snapshot["positions"].get(code, {}).get("name", ""))
            # trade-log 채널: 매 사이클 결정 전부(HOLD/축소/차단 포함) 기록
            self.discord.decision(
                code=code, name=name,
                llm_action=decision.action, llm_qty=decision.quantity,
                final_action=result.action, final_qty=result.quantity,
                approved=result.approved, blocked=result.blocked, price=price,
                signal=result.signal, rule=result.rule, guard_note=result.guard_note,
            )

            if result.blocked:
                self.discord.blocked(action=decision.action, code=code, reason=result.reason)
                continue
            if not result.approved:  # HOLD
                continue

            side = "buy" if result.action == "BUY" else "sell"
            self._execute(code, side, result.quantity, reason=result.reason, name=name,
                          signal=result.signal, rule=result.rule)

    # ── 주문 실행 + 기록 + 알림 (멱등성 키 등록) ──
    def _execute(self, code: str, side: str, qty: int, *, reason: str,
                 name: str = "", is_stop: bool = False, pnl_rate: float = 0.0,
                 signal: str = "", rule: str = "") -> None:
        if qty <= 0:
            return
        try:
            order = self.broker.place_order(code, side, qty)
        except BrokerError as exc:
            logger.error("주문 실패 %s %s x%d: %s", side, code, qty, exc)
            return  # broker가 이미 디스코드 경고를 보냄

        self._recent_order_keys.add(f"{code}:{side}")
        self.journal.log_order(code=code, side=side, quantity=qty,
                               odno=order.get("odno", ""), raw=order.get("raw", {}))
        price = self._price(code)
        if is_stop:
            self.discord.stop_loss(code=code, name=name, qty=qty, pnl_rate=pnl_rate,
                                   reason=reason)
        else:
            # 매매 직후 계좌 잔액을 함께 알린다. 조회 실패는 알림을 막지 않는다.
            balance: dict | None = None
            try:
                balance = self.broker.get_balance()
            except Exception:  # noqa: BLE001
                logger.exception("체결 알림용 잔고 조회 실패 — 잔액 생략")
            self.discord.order_filled(side=side, code=code, name=name, qty=qty,
                                      price=price, reason=reason,
                                      signal=signal, rule=rule, balance=balance,
                                      is_paper=self.settings.is_paper)

    def _daily_summary(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            bal = self.broker.get_balance()
            trades = self.journal.count_today_orders(today)
            self.journal.upsert_daily_pnl(trade_date=today, realized_pnl=0,
                                          trades=trades, total_eval=bal["total_eval"])
            self.discord.daily_summary(realized_pnl=0, trades=trades)
        except Exception:  # noqa: BLE001
            logger.exception("일일 요약 생성 실패")

    # ── 메인 루프 ──
    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.startup()

        interval = self.settings.decision_interval_min * 60
        try:
            while self._running:
                if not guardrails.is_market_open(datetime.now()):
                    logger.info("장 운영시간 외 — 대기")
                    self._sleep(30)
                    continue

                today = datetime.now().strftime("%Y-%m-%d")
                try:
                    snapshot = build_snapshot(self.broker, self.settings.watch_codes)
                except BrokerError as exc:
                    logger.error("스냅샷 실패: %s", exc)
                    self._sleep(15)
                    continue

                state = self._account_state(snapshot, today)

                # 매 사이클 손절 우선 점검
                self._run_stop_loss(state)

                # 결정 간격 도달 시에만 LLM 호출(매 틱 호출 금지)
                now = time.monotonic()
                if now - self._last_decision_ts >= interval:
                    self._recent_order_keys.clear()  # 사이클마다 멱등성 키 리셋
                    self._run_decisions(snapshot, state)
                    self._last_decision_ts = now

                self._sleep(10)
        except Exception as exc:  # noqa: BLE001 — 미처리 예외도 안전 종료
            logger.exception("메인 루프 치명적 오류")
            self.discord.error("메인 루프 중단", f"{type(exc).__name__}: {exc}")
        finally:
            self.shutdown()

    def _sleep(self, seconds: float) -> None:
        """종료 신호에 반응하며 잘게 쪼개 대기."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.5)


if __name__ == "__main__":
    Trader().run()
