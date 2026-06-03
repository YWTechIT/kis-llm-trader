"""실시간 체결가 웹소켓 구독 + 자동 재연결 + 하트비트 + 끊김 알림.

공식 `ka.KISWebSocket` + `ccnl_krx`(국내주식 실시간체결가)를 사용한다.
`KISWebSocket.start()`는 내부에서 asyncio 루프를 돌리고 자체 재시도 후 반환하므로,
이 모듈이 **별도 스레드 + 외곽 supervisor 루프**로 감싸 끊기면 backoff 재연결하고
디스코드 경고를 보낸다. 최신가는 스레드 안전한 스냅샷으로 노출한다(메인 루프/가드레일 참조용).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from kis_bootstrap import ka, kws, settings

logger = logging.getLogger(__name__)


def _to_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


class MarketStream:
    def __init__(self, codes: list[str],
                 notifier: Optional[Callable[[str, str], None]] = None,
                 *, heartbeat_timeout: float = 60.0) -> None:
        self._codes = codes
        self._notify = notifier
        self._heartbeat_timeout = heartbeat_timeout

        self._lock = threading.Lock()
        self._prices: dict[str, int] = {}
        self._last_msg_ts = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 공개 API ──
    def latest_price(self, code: str) -> Optional[int]:
        with self._lock:
            return self._prices.get(code)

    def all_prices(self) -> dict[str, int]:
        with self._lock:
            return dict(self._prices)

    def is_alive(self) -> bool:
        """마지막 메시지 이후 heartbeat_timeout 이내면 정상으로 간주."""
        with self._lock:
            if self._last_msg_ts == 0.0:
                return False
            return (time.monotonic() - self._last_msg_ts) < self._heartbeat_timeout

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="market-stream",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── 내부 ──
    def _on_result(self, ws, tr_id, df, columns) -> None:
        try:
            if df is None or df.empty:
                return
            for _, row in df.iterrows():
                code = str(row.get("MKSC_SHRN_ISCD", "")).strip()
                price = _to_int(row.get("STCK_PRPR"))
                if code and price > 0:
                    with self._lock:
                        self._prices[code] = price
            with self._lock:
                self._last_msg_ts = time.monotonic()
        except Exception:  # noqa: BLE001 — 콜백 예외가 스트림을 죽이지 않게
            logger.exception("스트림 콜백 처리 오류")

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ka.auth_ws()
                client = ka.KISWebSocket(api_url="/tryitout", max_retries=3)
                # 관심종목 실시간 체결가 구독
                client.subscribe(
                    request=kws.ccnl_krx,
                    data=list(self._codes),
                    kwargs={"env_dv": settings.env_dv},
                )
                logger.info("웹소켓 구독 시작: %s", self._codes)
                # start()는 blocking(asyncio.run). 정상/오류로 반환되면 재연결 대상.
                client.start(on_result=self._on_result)
            except Exception as exc:  # noqa: BLE001
                logger.error("웹소켓 연결 오류: %s", exc)

            if self._stop.is_set():
                break

            self._warn("웹소켓 끊김",
                       f"실시간 시세 연결이 끊겼습니다. {backoff:.0f}초 후 재연결 시도.")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)  # 지수 backoff (상한 30s)
        logger.info("웹소켓 supervisor 종료")

    def _warn(self, title: str, message: str) -> None:
        logger.warning("%s — %s", title, message)
        if self._notify is not None:
            try:
                self._notify(title, message)
            except Exception:  # noqa: BLE001
                logger.exception("notifier 호출 실패")
