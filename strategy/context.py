"""LLM에 넘길 전략 명세 + 시세/포지션/현금 스냅샷 구성.

전략 명세(STRATEGY_SPEC)는 사람이 직접 다듬는 상세 룰이다. LLM은 이 룰과
스냅샷을 보고 BUY/SELL/HOLD 중 하나를 고른다(자유 매매 아님). 안전장치는
코드(risk/guardrails.py)가 별도로 강제하므로 여기엔 룰의 '의도'만 적는다.
"""

from __future__ import annotations

from datetime import datetime

# ── 상세 전략 명세 (직접 편집) ──────────────────────────────────────────────
# 진입/청산/분할매수 규칙을 구체적으로 기술. LLM은 이 안에서만 판단한다.
STRATEGY_SPEC = """\
[운용 목표]
- 소액(약 50만원) 단기 스윙. 큰 손실 회피가 수익보다 우선.

[진입(BUY) 조건]
- 전일 종가 대비 현재가가 -2% 이상 하락한 눌림목에서 분할 매수 고려.
- 한 번에 전량 매수하지 말고 1주 단위로 분할 진입.
- 이미 해당 종목 비중이 큰 경우 추가 진입을 자제.

[청산(SELL) 조건]
- 매수 평균가 대비 +3% 이상이면 일부 익절 고려.
- 추세가 꺾였다고 판단되면(고점 대비 빠른 하락 등) 보유분 축소.

[관망(HOLD) 조건]
- 방향성이 불분명하거나 변동성이 과도하면 HOLD.
- 확신이 없으면 항상 HOLD를 선택(불필요한 매매 금지).

[주의]
- 손절/한도/거래횟수 등 안전장치는 코드가 강제하므로 여기서 신경 쓰지 않는다.
- 너는 BUY/SELL/HOLD 중 하나와 수량만 제안한다. 실행 여부는 코드가 최종 결정한다.
"""


def build_snapshot(broker, watch_codes: list[str]) -> dict:
    """현재 시세/포지션/현금 스냅샷. broker 호출 실패는 broker 계층이 처리."""
    balance = broker.get_balance()
    market: dict[str, dict] = {}
    for code in watch_codes:
        try:
            market[code] = broker.get_price(code)
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
