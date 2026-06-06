"""KIS REST 래퍼 — 시세/잔고/주문/체결.

설계
- **모든 KIS REST 호출의 try/catch·재시도·throttle는 이 계층에서만** 처리한다.
  상위(가드레일/메인 루프)는 정규화된 dict/list만 받고 KIS 응답 포맷을 모른다.
- 초당 호출 제한(거래소 ~20건) 대응 throttle + 지수 backoff 재시도.
- 토큰 만료/인증 오류 감지 시 1회 재인증 후 재시도.
- 실패 시 주입된 notifier(디스코드)로 경고 후 BrokerError를 올린다.

주의: 반드시 `kis_bootstrap`을 통해 vendor 모듈을 로드한다(config→kis_auth 순서 보장).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

import pandas as pd

from kis_bootstrap import ka, kb, settings

logger = logging.getLogger(__name__)

# 인증/토큰 관련으로 추정되는 오류 메시지 토큰(재인증 트리거용)
_AUTH_ERROR_HINTS = ("token", "expired", "기간이 만료", "유효하지 않은", "401", "EGW00123")
# 초당 호출 제한 등 일시적 과부하(재시도 시 더 길게 backoff)
_RATE_LIMIT_HINTS = ("초당 거래건수", "egw00201", "rate limit", "500")


class BrokerError(RuntimeError):
    """KIS 호출이 재시도 후에도 실패했을 때."""


def _to_int(value: Any, default: int = 0) -> int:
    """KIS 숫자 문자열('70,000', '70000.0' 등)을 안전하게 int로."""
    if value is None:
        return default
    try:
        return int(float(str(value).replace(",", "").strip() or default))
    except (ValueError, TypeError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).replace(",", "").strip() or default)
    except (ValueError, TypeError):
        return default


def _first_row(df: Optional[pd.DataFrame]) -> dict:
    """DataFrame 첫 행을 dict로. 비었으면 {}."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    return df.iloc[0].to_dict()


class Broker:
    def __init__(
        self,
        notifier: Optional[Callable[[str, str], None]] = None,
        max_retries: int = 3,
    ) -> None:
        """
        notifier: (title, message) -> None 형태의 경고 콜백(디스코드). None이면 로깅만.
        """
        self._notify = notifier
        self._max_retries = max_retries
        self._env = settings.env_dv  # "demo" | "real"
        # 모의는 초당 호출 제한이 빡빡(EGW00201) → 간격을 넉넉히(≤1콜/초)
        self._min_interval = 1.1 if settings.is_paper else 0.1
        self._last_call_ts = 0.0
        # 매매 루프(메인 스레드)와 조회 봇(asyncio 스레드)이 같은 Broker를 공유한다.
        # 모든 KIS REST 호출을 직렬화해 throttle 시계 경쟁/초당 호출제한 위반을 막는다.
        # 재인증 중첩 호출의 자기교착을 피하려고 RLock 사용.
        self._lock = threading.RLock()
        self._authed = False
        self._holiday_cache: dict[str, bool] = {}  # {YYYYMMDD: 개장일여부} (1일 1회 조회)

    # ── 인증 ──
    def authenticate(self) -> None:
        """기동 시 1회 호출. 실패하면 BrokerError."""
        with self._lock:  # _call과 동일 락으로 직렬화(봇 조회와 인증 경쟁 방지)
            try:
                ka.auth(svr=settings.kis_env)
            except Exception as exc:  # noqa: BLE001 — 외부 호출 경계
                self._warn("KIS 인증 실패", f"{type(exc).__name__}: {exc}")
                raise BrokerError(f"KIS 인증 실패: {exc}") from exc

            trenv = ka.getTREnv()
            if not getattr(trenv, "my_acct", ""):
                # auth()는 실패 시 조용히 반환(print)하므로 환경값으로 성공 여부를 확인
                self._warn("KIS 인증 실패", "토큰/계좌 환경이 설정되지 않았습니다(.env 확인).")
                raise BrokerError("KIS 인증 실패: 계좌 환경 미설정")
            self._authed = True
            logger.info("KIS 인증 완료 (env=%s)", settings.kis_env)

    @property
    def _account(self) -> tuple[str, str]:
        trenv = ka.getTREnv()
        return trenv.my_acct, trenv.my_prod

    # ── 내부 호출 래퍼: throttle + backoff + 재인증 ──
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_ts = time.monotonic()

    def _call(self, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # 모든 KIS REST 호출의 단일 관문. 락으로 감싸 매매 루프/조회 봇이 동시에 호출해도
        # throttle·backoff가 섞이지 않고 초당 호출제한을 지킨다. 락은 한 호출(재시도 포함)
        # 동안만 잡는다 — 봇이 매매 사이클 중 잠깐 대기할 수 있으나 한도 위반보다 안전하다.
        with self._lock:
            last_exc: Optional[Exception] = None
            for attempt in range(1, self._max_retries + 1):
                self._throttle()
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — 외부 호출 경계
                    last_exc = exc
                    msg = str(exc).lower()
                    is_auth = any(h.lower() in msg for h in _AUTH_ERROR_HINTS)
                    is_rate = any(h.lower() in msg for h in _RATE_LIMIT_HINTS)
                    logger.warning(
                        "%s 실패 (%d/%d): %s%s",
                        label, attempt, self._max_retries, exc,
                        " [재인증 시도]" if is_auth else (" [rate limit]" if is_rate else ""),
                    )
                    if is_auth:
                        try:
                            ka.auth(svr=settings.kis_env)
                        except Exception:  # noqa: BLE001
                            pass
                    # rate limit이면 더 길게 쉰다(초당 제한 회복 대기)
                    base = 1.5 if is_rate else 0.5
                    time.sleep(min(2 ** attempt * base, 8.0))  # 지수 backoff (상한 8s)
            self._warn(f"{label} 반복 실패", f"{type(last_exc).__name__}: {last_exc}")
            raise BrokerError(f"{label} 실패: {last_exc}") from last_exc

    def _warn(self, title: str, message: str) -> None:
        logger.error("%s — %s", title, message)
        if self._notify is not None:
            try:
                self._notify(title, message)
            except Exception:  # noqa: BLE001 — 알림 실패가 본 흐름을 죽이지 않게
                logger.exception("notifier 호출 실패")

    # ── 공개 API ──
    def get_price(self, code: str) -> dict:
        """현재가 조회. 반환: {code, name, price, open, high, low, prdy_ctrt(전일대비율)}.

        name(HTS 한글 종목명)은 보유하지 않은 종목도 알림에 종목명을 함께
        표기하기 위해 포함한다(예: 피에스케이홀딩스(031980)).
        """
        df = self._call(
            f"get_price({code})",
            kb.inquire_price,
            env_dv=self._env,
            fid_cond_mrkt_div_code="J",
            fid_input_iscd=code,
        )
        row = _first_row(df)
        if not row:
            raise BrokerError(f"get_price({code}): 빈 응답")
        return {
            "code": code,
            "name": str(row.get("hts_kor_isnm", "")).strip(),
            "price": _to_int(row.get("stck_prpr")),
            "open": _to_int(row.get("stck_oprc")),
            "high": _to_int(row.get("stck_hgpr")),
            "low": _to_int(row.get("stck_lwpr")),
            "prdy_ctrt": _to_float(row.get("prdy_ctrt")),
        }

    def get_daily_ohlcv(self, code: str, count: int = 30) -> list[dict]:
        """최근 일봉 OHLCV 조회(과거→현재 정렬). 이동평균 등 지표 계산용 원천 데이터.

        count는 최근 영업일 수(KIS 한 번 호출 상한 100). 조회 시작일은 달력 기준으로
        넉넉히(주말/공휴일 감안) 잡고, 받은 일봉 중 마지막 count개만 돌려준다.
        반환: [{date, open, high, low, close, volume}, ...] (오래된 날짜가 앞).
        """
        from datetime import datetime, timedelta

        count = max(1, min(count, 100))
        end = datetime.now()
        # 영업일 ~count개를 확보하려면 달력일은 더 길게 잡아야 한다(주말·휴일 보정 ×2 + 여유).
        start = end - timedelta(days=count * 2 + 10)
        _, df = self._call(
            f"get_daily_ohlcv({code})",
            kb.inquire_daily_itemchartprice,
            env_dv=self._env,
            fid_cond_mrkt_div_code="J",
            fid_input_iscd=code,
            fid_input_date_1=start.strftime("%Y%m%d"),
            fid_input_date_2=end.strftime("%Y%m%d"),
            fid_period_div_code="D",
            fid_org_adj_prc="0",  # 수정주가(액면분할 등 보정) — 이평선 연속성 확보
        )
        if not isinstance(df, pd.DataFrame) or df.empty:
            raise BrokerError(f"get_daily_ohlcv({code}): 빈 응답")

        candles: list[dict] = []
        for _, r in df.iterrows():
            row = r.to_dict()
            close = _to_int(row.get("stck_clpr"))
            if close <= 0:  # 휴장/결측 행 방어
                continue
            candles.append({
                "date": str(row.get("stck_bsop_date", "")).strip(),
                "open": _to_int(row.get("stck_oprc")),
                "high": _to_int(row.get("stck_hgpr")),
                "low": _to_int(row.get("stck_lwpr")),
                "close": close,
                "volume": _to_int(row.get("acml_vol")),
            })
        if not candles:
            raise BrokerError(f"get_daily_ohlcv({code}): 유효 일봉 없음")

        # KIS는 최신→과거 순으로 주므로 과거→현재로 뒤집고 최근 count개만 남긴다.
        candles.sort(key=lambda c: c["date"])
        return candles[-count:]

    def get_balance(self) -> dict:
        """잔고 조회. 반환: {cash, total_eval, positions: {code: {...}}}."""
        cano, prod = self._account
        df_holdings, df_summary = self._call(
            "get_balance",
            kb.inquire_balance,
            env_dv=self._env,
            cano=cano,
            acnt_prdt_cd=prod,
            afhr_flpr_yn="N",
            inqr_dvsn="02",
            unpr_dvsn="01",
            fund_sttl_icld_yn="N",
            fncg_amt_auto_rdpt_yn="N",
            prcs_dvsn="00",
        )

        positions: dict[str, dict] = {}
        if isinstance(df_holdings, pd.DataFrame) and not df_holdings.empty:
            for _, r in df_holdings.iterrows():
                row = r.to_dict()
                qty = _to_int(row.get("hldg_qty"))
                if qty <= 0:
                    continue
                code = str(row.get("pdno", "")).strip()
                positions[code] = {
                    "code": code,
                    "name": str(row.get("prdt_name", "")).strip(),
                    "qty": qty,
                    "avg_price": _to_int(row.get("pchs_avg_pric")),
                    "current_price": _to_int(row.get("prpr")),
                    "eval_amt": _to_int(row.get("evlu_amt")),
                    "pnl_amt": _to_int(row.get("evlu_pfls_amt")),
                    "pnl_rate": _to_float(row.get("evlu_pfls_rt")),
                }

        summary = _first_row(df_summary)
        return {
            "cash": _to_int(summary.get("dnca_tot_amt")),  # 예수금 총금액
            "available_cash": _to_int(
                summary.get("prvs_rcdl_excc_amt", summary.get("dnca_tot_amt"))
            ),  # 가용(주문가능 추정)
            "total_eval": _to_int(summary.get("tot_evlu_amt")),  # 총평가금액
            "positions": positions,
        }

    def get_orderable_cash(self, code: str, price: int = 0) -> dict:
        """매수가능 금액/수량 조회 (inquire_psbl_order).

        ord_dvsn='01'(시장가)로 호출해야 종목증거금율이 반영된 가능수량이 나온다(벤더 권고).
        price=0이면 시장가 기준으로 조회한다.
        반환: {code, nrcvb_buy_amt(미수없는 매수금액), max_buy_amt(최대 매수금액),
              nrcvb_buy_qty(미수없는 매수수량), max_buy_qty(최대 매수수량), ord_unpr}.
        실패 시 BrokerError (상위에서 처리). 빈 응답도 BrokerError.
        """
        cano, prod = self._account
        df = self._call(
            f"get_orderable_cash({code})",
            kb.inquire_psbl_order,
            env_dv=self._env,
            cano=cano,
            acnt_prdt_cd=prod,
            pdno=code,
            ord_unpr=str(int(price)),   # 시장가 가능수량 조회 시 0 허용
            ord_dvsn="01",              # 01:시장가 — 증거금율 반영(벤더 docstring 권고)
            cma_evlu_amt_icld_yn="N",
            ovrs_icld_yn="N",
        )
        row = _first_row(df)
        if not row:
            raise BrokerError(f"get_orderable_cash({code}): 빈 응답")
        return {
            "code": code,
            "nrcvb_buy_amt": _to_int(row.get("nrcvb_buy_amt")),
            "max_buy_amt": _to_int(row.get("max_buy_amt")),
            "nrcvb_buy_qty": _to_int(row.get("nrcvb_buy_qty")),
            "max_buy_qty": _to_int(row.get("max_buy_qty")),
            "ord_unpr": int(price),
        }

    def place_order(self, code: str, side: str, qty: int, *, market: bool = True,
                    price: int = 0) -> dict:
        """주문 전송. side: 'buy'|'sell'. 기본 시장가.

        반환: {code, side, qty, odno(주문번호), order_time, raw}.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"side는 'buy'|'sell' (받은값: {side})")
        if qty <= 0:
            raise ValueError(f"주문수량은 1 이상이어야 합니다 (받은값: {qty})")

        cano, prod = self._account
        ord_dvsn = "01" if market else "00"        # 01:시장가, 00:지정가
        ord_unpr = "0" if market else str(int(price))
        df = self._call(
            f"place_order({side} {code} x{qty})",
            kb.order_cash,
            env_dv=self._env,
            ord_dv=side,
            cano=cano,
            acnt_prdt_cd=prod,
            pdno=code,
            ord_dvsn=ord_dvsn,
            ord_qty=str(int(qty)),
            ord_unpr=ord_unpr,
            excg_id_dvsn_cd="KRX",
            sll_type="01" if side == "sell" else "",
        )
        row = _first_row(df)
        return {
            "code": code,
            "side": side,
            "qty": int(qty),
            "odno": str(row.get("ODNO", row.get("odno", ""))).strip(),
            "order_time": str(row.get("ORD_TMD", row.get("ord_tmd", ""))).strip(),
            "raw": row,
        }

    def get_filled(self, code: str = "", *, ccld_only: bool = True) -> list[dict]:
        """당일 주문체결 조회. code 비우면 전체."""
        from datetime import datetime

        cano, prod = self._account
        today = datetime.now().strftime("%Y%m%d")
        df_list, _ = self._call(
            "get_filled",
            kb.inquire_daily_ccld,
            env_dv=self._env,
            pd_dv="inner",
            cano=cano,
            acnt_prdt_cd=prod,
            inqr_strt_dt=today,
            inqr_end_dt=today,
            sll_buy_dvsn_cd="00",
            ccld_dvsn="01" if ccld_only else "00",
            inqr_dvsn="00",
            inqr_dvsn_3="00",
            pdno=code,
        )
        fills: list[dict] = []
        if isinstance(df_list, pd.DataFrame) and not df_list.empty:
            for _, r in df_list.iterrows():
                row = r.to_dict()
                fills.append({
                    "code": str(row.get("pdno", "")).strip(),
                    "name": str(row.get("prdt_name", "")).strip(),
                    "side": "sell" if str(row.get("sll_buy_dvsn_cd", "")).strip() == "01" else "buy",
                    "odno": str(row.get("odno", "")).strip(),
                    "order_qty": _to_int(row.get("ord_qty")),
                    "filled_qty": _to_int(row.get("tot_ccld_qty")),
                    "avg_price": _to_int(row.get("avg_prvs", row.get("ccld_prvs"))),
                    "filled_amt": _to_int(row.get("tot_ccld_amt")),
                })
        return fills

    def get_buyable(self, code: str, price: int = 0) -> dict:
        """매수가능금액 조회. price=0이면 시장가 기준(증거금율 반영)으로 조회한다.

        반환: {code, ord_psbl_cash, nrcvb_buy_amt, nrcvb_buy_qty,
               max_buy_amt, max_buy_qty}.
        ※ 미수 미사용 기준은 nrcvb_*(미수없는…), 신용 포함 최대는 max_*.
        """
        cano, prod = self._account
        market = price <= 0
        df = self._call(
            f"get_buyable({code})",
            kb.inquire_psbl_order,
            env_dv=self._env,
            cano=cano,
            acnt_prdt_cd=prod,
            pdno=code,
            ord_unpr="0" if market else str(int(price)),
            ord_dvsn="01" if market else "00",  # 01:시장가(증거금율 반영), 00:지정가
            cma_evlu_amt_icld_yn="N",
            ovrs_icld_yn="N",
        )
        row = _first_row(df)
        return {
            "code": code,
            "ord_psbl_cash": _to_int(row.get("ord_psbl_cash")),  # 주문가능현금
            "nrcvb_buy_amt": _to_int(row.get("nrcvb_buy_amt")),  # 미수없는 매수금액
            "nrcvb_buy_qty": _to_int(row.get("nrcvb_buy_qty")),  # 미수없는 매수수량
            "max_buy_amt": _to_int(row.get("max_buy_amt")),      # 최대 매수금액(신용 포함)
            "max_buy_qty": _to_int(row.get("max_buy_qty")),      # 최대 매수수량
        }

    def is_trading_day(self, date: str = "") -> bool:
        """해당일 개장(주문가능)일 여부. KIS 휴장일조회(chk_holiday)의 opnd_yn 사용.

        '1일 1회 호출' 권고에 따라 날짜별로 캐시한다. 조회 실패 시 안전하게
        False(매매 중단)를 반환하되 캐시하지 않아 다음 사이클에 재시도한다.
        """
        day = date or datetime.now().strftime("%Y%m%d")
        if day in self._holiday_cache:
            return self._holiday_cache[day]
        try:
            df = self._call("is_trading_day", kb.chk_holiday, bass_dt=day)
        except BrokerError:
            return False  # 불확실하면 매매하지 않는다(미캐시 → 다음 사이클 재시도)

        opnd = False
        if isinstance(df, pd.DataFrame) and not df.empty:
            row = df[df["bass_dt"].astype(str) == day]
            target = row.iloc[0] if not row.empty else df.iloc[0]
            opnd = str(target.get("opnd_yn", "")).strip().upper() == "Y"
        self._holiday_cache[day] = opnd
        logger.info("영업일 조회 %s: 개장=%s", day, opnd)
        return opnd


def _smoke(code: str) -> None:
    """`uv run python -m adapter.broker --smoke 005930` — 인증+시세 1건."""
    logging.basicConfig(level=logging.INFO)
    b = Broker()
    b.authenticate()
    print("PRICE:", b.get_price(code))
    bal = b.get_balance()
    print("CASH:", bal["cash"], "TOTAL_EVAL:", bal["total_eval"],
          "POSITIONS:", list(bal["positions"].keys()))


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "--smoke":
        _smoke(sys.argv[2])
    else:
        print("usage: python -m adapter.broker --smoke <code>")
