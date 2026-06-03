"""가드레일 단위테스트 — 크리덴셜 불필요. `uv run pytest`로 실행."""

from datetime import datetime

import pytest

from risk.guardrails import (
    AccountState,
    Decision,
    GuardrailConfig,
    check,
    force_stop_loss,
    is_kill_switch_active,
    is_market_open,
)

CFG = GuardrailConfig(
    max_order_krw=100_000,
    max_position_pct=0.4,
    daily_max_loss_krw=30_000,
    max_trades_per_day=5,
    hard_stop_loss_pct=-10.0,
)

# 장중 평일 시각 (월요일 10:00)
OPEN_NOW = datetime(2026, 6, 1, 10, 0)


def _state(**kw) -> AccountState:
    base = dict(
        cash=500_000, available_cash=500_000, total_eval=500_000,
        positions={}, trades_today=0, realized_pnl_today=0,
        recent_order_keys=set(), now=OPEN_NOW,
    )
    base.update(kw)
    return AccountState(**base)


# ── 장 운영시간 ──
def test_market_hours():
    assert is_market_open(datetime(2026, 6, 1, 10, 0)) is True   # 월 10:00
    assert is_market_open(datetime(2026, 6, 1, 8, 59)) is False  # 개장 전
    assert is_market_open(datetime(2026, 6, 1, 15, 31)) is False # 마감 후
    assert is_market_open(datetime(2026, 6, 6, 10, 0)) is False  # 토요일


def test_order_blocked_outside_market_hours():
    st = _state(now=datetime(2026, 6, 1, 16, 0))
    d = Decision("BUY", 1, "사고싶음", "005930")
    res = check(d, st, CFG, current_price=70_000)
    assert res.approved is False and res.blocked is True


# ── HOLD ──
def test_hold_passes_through():
    res = check(Decision("HOLD", 0, "관망", "005930"), _state(), CFG, 70_000)
    assert res.approved is False and res.blocked is False and res.action == "HOLD"


# ── 1회 매수금액 한도 → 자동 축소 ──
def test_buy_reduced_by_order_limit():
    # 한도 10만 / 가격 7만 → 최대 1주
    d = Decision("BUY", 10, "10주 매수", "005930")
    res = check(d, _state(), CFG, current_price=70_000)
    assert res.approved is True
    assert res.quantity == 1
    assert res.adjusted is True


def test_buy_blocked_when_price_exceeds_order_limit():
    # 가격 15만 > 1회 한도 10만 → 0주 → 차단
    d = Decision("BUY", 1, "비싼주식", "005930")
    res = check(d, _state(), CFG, current_price=150_000)
    assert res.approved is False and res.blocked is True


# ── 종목당 최대 비중 ──
def test_buy_reduced_by_position_concentration():
    # total_eval 100만, 한도 40% = 40만. 기존 보유 35만 → 여유 5만 → 가격 1만 → 5주
    st = _state(
        total_eval=1_000_000, available_cash=1_000_000,
        positions={"005930": {"qty": 35, "eval_amt": 350_000, "pnl_rate": 0.0,
                              "avg_price": 10_000, "current_price": 10_000}},
    )
    d = Decision("BUY", 50, "더 사자", "005930")
    res = check(d, st, CFG, current_price=10_000)
    assert res.approved is True
    assert res.quantity == 5
    assert res.adjusted is True


# ── 가용 현금 한도 ──
def test_buy_reduced_by_cash():
    st = _state(available_cash=20_000, total_eval=1_000_000)
    d = Decision("BUY", 5, "매수", "005930")  # 한도/비중은 여유, 현금만 2만
    res = check(d, st, CFG, current_price=10_000)
    assert res.quantity == 2 and res.approved is True


# ── 하루 거래 횟수 ──
def test_trade_count_limit():
    st = _state(trades_today=5)
    res = check(Decision("BUY", 1, "x", "005930"), st, CFG, 10_000)
    assert res.approved is False and res.blocked is True


# ── kill switch ──
def test_kill_switch_blocks_all():
    st = _state(realized_pnl_today=-30_000)
    assert is_kill_switch_active(st, CFG) is True
    res = check(Decision("BUY", 1, "x", "005930"), st, CFG, 10_000)
    assert res.approved is False and res.blocked is True
    # 매도도 막힌다(강제 손절은 별도 경로)
    st2 = _state(realized_pnl_today=-50_000,
                 positions={"005930": {"qty": 10, "eval_amt": 100_000, "pnl_rate": -2.0}})
    res2 = check(Decision("SELL", 5, "x", "005930"), st2, CFG, 10_000)
    assert res2.approved is False


# ── 멱등성(중복 주문) ──
def test_idempotency_blocks_duplicate():
    st = _state(recent_order_keys={"005930:buy"})
    res = check(Decision("BUY", 1, "또 사기", "005930"), st, CFG, 10_000)
    assert res.approved is False and res.blocked is True
    # 다른 방향은 통과
    res2 = check(Decision("SELL", 1, "팔기", "005930"),
                 _state(recent_order_keys={"005930:buy"},
                        positions={"005930": {"qty": 5, "eval_amt": 50_000, "pnl_rate": 0}}),
                 CFG, 10_000)
    assert res2.approved is True


# ── SELL ──
def test_sell_clamped_to_holdings():
    st = _state(positions={"005930": {"qty": 3, "eval_amt": 30_000, "pnl_rate": 1.0}})
    res = check(Decision("SELL", 10, "전량매도", "005930"), st, CFG, 10_000)
    assert res.approved is True and res.quantity == 3 and res.adjusted is True


def test_sell_blocked_when_no_holdings():
    res = check(Decision("SELL", 1, "x", "005930"), _state(), CFG, 10_000)
    assert res.approved is False and res.blocked is True


# ── 하드 손절(LLM 무시) ──
def test_force_stop_loss():
    st = _state(positions={
        "005930": {"qty": 10, "eval_amt": 90_000, "pnl_rate": -12.0},   # 손절 대상
        "000660": {"qty": 5, "eval_amt": 50_000, "pnl_rate": -3.0},     # 유지
    })
    decisions = force_stop_loss(st, CFG)
    assert len(decisions) == 1
    assert decisions[0].code == "005930"
    assert decisions[0].action == "SELL" and decisions[0].quantity == 10


def test_unknown_action_becomes_hold():
    res = check(Decision("YOLO", 1, "?", "005930"), _state(), CFG, 10_000)
    assert res.action == "HOLD" and res.approved is False
