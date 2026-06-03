"""LLM에 넘길 전략 명세 + 시세/포지션/현금 스냅샷 구성.

전략 명세는 strategy/presets.py 의 활성 프리셋이 제공한다. LLM은 그 명세와
스냅샷을 보고 BUY/SELL/HOLD 중 하나를 고른다(자유 매매 아님). 안전장치는
코드(risk/guardrails.py)가 별도로 강제하므로 여기엔 룰의 '의도'만 담는다.

활성 전략은 config.settings.strategy_name 으로 고르고, 각 전략이 스냅샷에
넣을 지표(signals 함수)와 SPEC 텍스트를 함께 가진다. 전략 교체는 .env의
STRATEGY_NAME 한 줄로 끝난다.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from config import settings
from strategy.presets import get_preset

# 활성 전략 프리셋(기동 시 1회 결정). config가 미등록 키를 이미 막아준다.
_ACTIVE = get_preset(settings.strategy_name)

# 하위 호환: 기존 코드가 import 하던 모듈 상수 = 활성 전략의 SPEC.
STRATEGY_SPEC = _ACTIVE.spec

# 일봉은 활성 전략이 요구하는 최소치보다 여유 있게 받는다(주말/휴일·결측 대비).
_OHLCV_COUNT = min(100, max(30, _ACTIVE.min_days + 10))


def build_snapshot(broker, watch_codes: list[str]) -> dict:
    """현재 시세/포지션/현금 스냅샷. broker 호출 실패는 broker 계층이 처리."""
    balance = broker.get_balance()
    market: dict[str, dict] = {}
    for code in watch_codes:
        try:
            info = broker.get_price(code)
            try:
                if _ACTIVE.min_days > 0:
                    candles = broker.get_daily_ohlcv(code, count=_OHLCV_COUNT)
                    df = pd.DataFrame(candles)  # 과거→현재 정렬, OHLCV 컬럼
                else:
                    df = pd.DataFrame()  # 52주 신고가처럼 일봉이 필요 없는 전략
                info.update(_ACTIVE.signals(df, info))
            except Exception:  # noqa: BLE001 — 지표 실패가 현재가까지 막지 않게
                info["error"] = "지표 계산 실패"
            market[code] = info
        except Exception:  # noqa: BLE001 — 일부 종목 실패가 전체를 막지 않게
            market[code] = {"code": code, "price": 0, "error": "조회 실패"}

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cash": balance["cash"],
        "available_cash": balance["available_cash"],
        "total_eval": balance["total_eval"],
        "positions": balance["positions"],
        "market": market,
    }


def build_decision_input(snapshot: dict, code: str) -> dict:
    """특정 종목 결정을 위해 LLM에 넘길 컨텍스트(시세+해당 포지션+현금)."""
    return {
        "timestamp": snapshot["timestamp"],
        "target_code": code,
        "market": snapshot["market"].get(code, {"code": code, "price": 0}),
        "current_position": snapshot["positions"].get(code, None),
        "available_cash": snapshot["available_cash"],
        "total_eval": snapshot["total_eval"],
    }
