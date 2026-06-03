"""디스코드 Webhook 알림 (embed 카드).

- 알림 전송 실패가 **메인 매매 루프를 죽이지 않도록** 모든 전송은 예외를 삼킨다(로깅).
- 크리덴셜/시크릿은 어떤 메시지에도 포함하지 않는다(호출자가 책임).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# embed 색상
_COLOR = {
    "buy": 0x2ECC71,     # green
    "sell": 0xE67E22,    # orange
    "stop": 0xE74C3C,    # red (손절)
    "error": 0xC0392B,   # dark red
    "info": 0x3498DB,    # blue
    "summary": 0x9B59B6, # purple
}
_TIMEOUT = 10


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, tradelog_url: str = "",
                 username: str = "ywtechit-llm-trader") -> None:
        """webhook_url: 중요 이벤트(체결/손절/차단/요약/에러)용 일반 채널.
        tradelog_url: 매 사이클 LLM 결정 전부를 보낼 별도 채널. 비어 있으면
        결정 알림도 일반 채널로 폴백한다.
        """
        self._url = webhook_url
        self._tradelog_url = tradelog_url or webhook_url
        self._username = username

    def _post(self, embed: dict, *, url: str | None = None) -> None:
        target = url if url is not None else self._url
        if not target:
            logger.warning("Discord webhook 미설정 — 알림 생략: %s", embed.get("title"))
            return
        try:
            resp = requests.post(
                target,
                json={"username": self._username, "embeds": [embed]},
                timeout=_TIMEOUT,
            )
            if resp.status_code >= 400:
                logger.error("디스코드 전송 실패 status=%s body=%s",
                             resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            # 알림 실패는 흐름을 막지 않는다.
            logger.error("디스코드 전송 예외: %s", exc)

    def _embed(self, title: str, kind: str, fields: list[dict] | None = None,
               description: str = "") -> dict:
        return {
            "title": title,
            "description": description,
            "color": _COLOR.get(kind, _COLOR["info"]),
            "fields": fields or [],
        }

    # ── 이벤트별 헬퍼 ──
    def order_filled(self, *, side: str, code: str, name: str, qty: int,
                     price: int, reason: str, signal: str = "", rule: str = "",
                     balance: dict | None = None, is_paper: bool | None = None) -> None:
        kind = "buy" if side == "buy" else "sell"
        emoji = "🟢 매수" if side == "buy" else "🟠 매도"
        env_tag = self._env_tag(is_paper)
        fields = [
            {"name": "종목", "value": f"{name or ''} ({code})", "inline": True},
            {"name": "수량", "value": f"{qty:,}주", "inline": True},
            {"name": "가격", "value": f"{price:,}원", "inline": True},
        ]
        if env_tag:
            fields.append({"name": "서버", "value": env_tag, "inline": True})
        # 근거를 명확히 분리 노출(있으면), 없으면 기존 reason 한 줄.
        if signal:
            fields.append({"name": "📈 신호", "value": signal[:1000], "inline": False})
        if rule:
            fields.append({"name": "📋 전략 근거", "value": rule[:1000], "inline": False})
        if not (signal or rule):
            fields.append({"name": "LLM 사유", "value": (reason or "-")[:1000], "inline": False})
        fields.extend(self._balance_fields(balance))
        self._post(self._embed(f"{emoji} 체결 — {name or code}", kind, fields=fields))

    @staticmethod
    def _env_tag(is_paper: bool | None) -> str:
        """매매 서버 구분 라벨. None이면 표기 생략."""
        if is_paper is None:
            return ""
        return "🧪 모의투자" if is_paper else "🔴 실제투자"

    @staticmethod
    def _balance_fields(balance: dict | None) -> list[dict]:
        """매매 후 계좌 잔액 요약 필드. balance가 없으면 빈 리스트."""
        if not balance:
            return []
        fields: list[dict] = []
        # 잔액 정보는 키가 누락돼도 알림을 막지 않는다.
        if "cash" in balance:
            fields.append({"name": "💰 예수금", "value": f"{balance['cash']:,}원", "inline": True})
        if "available_cash" in balance:
            fields.append({"name": "주문가능", "value": f"{balance['available_cash']:,}원", "inline": True})
        if "total_eval" in balance:
            fields.append({"name": "총평가금액", "value": f"{balance['total_eval']:,}원", "inline": True})
        return fields

    def stop_loss(self, *, code: str, name: str, qty: int, pnl_rate: float,
                  reason: str = "") -> None:
        fields = [
            {"name": "종목", "value": f"{name or ''} ({code})", "inline": True},
            {"name": "수량", "value": f"{qty:,}주", "inline": True},
            {"name": "손익률", "value": f"{pnl_rate:.2f}%", "inline": True},
        ]
        if reason:
            fields.append({"name": "사유", "value": reason[:1000], "inline": False})
        self._post(self._embed(f"🛑 손절 발동 — {name or code}", "stop", fields=fields))

    def decision(self, *, code: str, name: str, llm_action: str, llm_qty: int,
                 final_action: str, final_qty: int, approved: bool, blocked: bool,
                 price: int, signal: str = "", rule: str = "",
                 guard_note: str = "") -> None:
        """매 사이클 LLM 결정 1건 — trade-log 채널 전용.

        HOLD/승인/축소/차단을 모두 기록해 의사결정 전체를 추적 가능하게 한다.
        """
        if blocked:
            emoji, kind, status = "⛔", "stop", "차단"
        elif final_action == "HOLD":
            emoji, kind, status = "⚪", "info", "관망"
        elif final_action == "BUY":
            emoji, kind, status = "🟢", "buy", "승인"
        else:
            emoji, kind, status = "🟠", "sell", "승인"

        llm_str = "HOLD" if llm_action == "HOLD" else f"{llm_action} {llm_qty:,}주"
        final_str = "HOLD" if final_action == "HOLD" else f"{final_action} {final_qty:,}주"
        fields = [
            {"name": "LLM 결정", "value": llm_str, "inline": True},
            {"name": "최종 실행", "value": final_str, "inline": True},
            {"name": "상태", "value": f"{'❌' if (blocked or not approved) and final_action != 'HOLD' else '✅'} {status}",
             "inline": True},
        ]
        if price > 0:
            fields.append({"name": "현재가", "value": f"{price:,}원", "inline": True})
        if signal:
            fields.append({"name": "📈 신호", "value": signal[:1000], "inline": False})
        if rule:
            fields.append({"name": "📋 전략 근거", "value": rule[:1000], "inline": False})
        if guard_note:
            fields.append({"name": "🛡️ 가드레일", "value": guard_note[:1000], "inline": False})
        self._post(
            self._embed(f"{emoji} 결정 — {name or code} ({code})", kind, fields=fields),
            url=self._tradelog_url,
        )

    def blocked(self, *, action: str, code: str, reason: str) -> None:
        self._post(self._embed(
            f"⛔ 가드레일 차단 — {action} {code}", "info",
            description=(reason or "-")[:1500],
        ))

    def daily_summary(self, *, realized_pnl: int, trades: int, bh_pnl: int | None = None,
                      extra: str = "") -> None:
        fields = [
            {"name": "당일 실현손익", "value": f"{realized_pnl:,}원", "inline": True},
            {"name": "거래 횟수", "value": f"{trades}회", "inline": True},
        ]
        if bh_pnl is not None:
            fields.append({"name": "Buy&Hold 가상손익", "value": f"{bh_pnl:,}원", "inline": True})
        self._post(self._embed("📊 일일 요약", "summary", fields=fields, description=extra))

    def info(self, title: str, message: str = "") -> None:
        self._post(self._embed(title, "info", description=message[:1500]))

    def error(self, title: str, message: str = "") -> None:
        self._post(self._embed(f"⚠️ {title}", "error", description=message[:1500]))
