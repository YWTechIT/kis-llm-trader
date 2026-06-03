"""코드 강제 안전장치 — LLM 위에서 최종 검문하는 차선/브레이크.

LLM이 무엇을 고르든 **주문 직전 이 모듈이 무조건 검증**한다. 위반 시 차단하거나
한도 내로 자동 축소한다. 외부 의존성이 없는 순수 로직이라 크리덴셜 없이 단위테스트 가능.

핵심 규칙
- kill switch: 당일 실현손실이 한도 도달 시 당일 신규 매매 전면 중단(HOLD).
- 1회 매수금액 한도 / 종목당 최대 비중 → 초과 시 수량 자동 축소(0이면 차단).
- 하루 최대 거래 횟수 초과 → 차단.
- 멱등성: 같은 (종목, 방향) 주문이 직전에 이미 나갔으면 차단.
- 장운영시간 외 → 차단.
- 하드 손절: `force_stop_loss()`는 LLM과 무관하게 즉시 청산 신호를 만든다(check를 거치지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from math import floor

Action = str  # "BUY" | "SELL" | "HOLD"


@dataclass(frozen=True)
class GuardrailConfig:
    max_order_krw: int
    max_position_pct: float       # 0~1, 총평가금액 대비 종목 비중 상한
    daily_max_loss_krw: int       # 양수로 보관(예: 30000원). 실현손실이 -이 값 이하면 kill switch
    max_trades_per_day: int
    hard_stop_loss_pct: float     # 음수(예: -10.0). 보유 종목 손익률이 이 값 이하면 강제청산

    @classmethod
    def from_settings(cls, s) -> "GuardrailConfig":
        return cls(
            max_order_krw=s.max_order_krw,
            max_position_pct=s.max_position_pct,
            daily_max_loss_krw=abs(s.daily_max_loss_krw),
            max_trades_per_day=s.max_trades_per_day,
            hard_stop_loss_pct=s.hard_stop_loss_pct,
        )


@dataclass(frozen=True)
class Decision:
    action: Action
    quantity: int
    reason: str            # 사람이 읽을 한 줄 요약(= summary). 다운스트림 호환을 위해 문자열 유지.
    code: str
    # 구조화 근거(LLM tool use로 강제). 알림/저널에 풀어 보여주기 위함. 손절 등 코드 생성 결정은 비어있을 수 있음.
    signal: str = ""       # 결정 트리거 — 스냅샷의 구체적 수치를 인용
    rule: str = ""         # 근거한 전략 명세 조항


@dataclass
class AccountState:
    cash: int                       # 예수금
    available_cash: int             # 주문가능 현금
    total_eval: int                 # 총평가금액(현금+주식)
    positions: dict                 # {code: {qty, avg_price, current_price, pnl_rate}}
    trades_today: int = 0
    realized_pnl_today: int = 0     # 당일 실현손익(원). 손실이면 음수
    recent_order_keys: set = field(default_factory=set)  # {"code:side"} 멱등성용
    now: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class GuardrailResult:
    action: Action          # 최종 실행할 액션(차단 시 "HOLD")
    quantity: int           # 최종 수량(축소 가능)
    approved: bool          # 주문을 실제 보낼지
    reason: str             # 사람이 읽을 사유(원 결정 + 가드레일 조치)
    adjusted: bool = False  # 수량이 축소되었는가
    blocked: bool = False   # 가드레일이 막았는가
    signal: str = ""        # LLM 근거(트리거) — 알림에 그대로 전달
    rule: str = ""          # LLM이 근거한 전략 조항
    guard_note: str = ""    # 가드레일이 가한 조치(축소/차단 사유)만 분리 보관


def _hold(reason: str, blocked: bool = True) -> GuardrailResult:
    return GuardrailResult(action="HOLD", quantity=0, approved=False,
                           reason=reason, blocked=blocked, guard_note=reason)


# ── 장 운영시간 (KRX 정규장 09:00–15:30, 평일). 공휴일은 토이 범위 밖. ──
_MARKET_OPEN = time(9, 0)
_MARKET_CLOSE = time(15, 30)


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:  # 토(5)·일(6)
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def is_kill_switch_active(state: AccountState, cfg: GuardrailConfig) -> bool:
    """당일 실현손실이 한도에 도달했는가."""
    return state.realized_pnl_today <= -cfg.daily_max_loss_krw


def force_stop_loss(state: AccountState, cfg: GuardrailConfig) -> list[Decision]:
    """하드 손절선 도달 종목에 대한 강제 청산(SELL) 결정 목록.

    LLM 의견과 무관하며 check()를 거치지 않고 즉시 실행되어야 한다.
    """
    decisions: list[Decision] = []
    for code, pos in state.positions.items():
        qty = int(pos.get("qty", 0))
        pnl_rate = float(pos.get("pnl_rate", 0.0))
        if qty > 0 and pnl_rate <= cfg.hard_stop_loss_pct:
            decisions.append(Decision(
                action="SELL", quantity=qty, code=code,
                reason=f"하드 손절 발동: 손익률 {pnl_rate:.2f}% ≤ {cfg.hard_stop_loss_pct:.2f}%",
            ))
    return decisions


def check(decision: Decision, state: AccountState, cfg: GuardrailConfig,
          current_price: int) -> GuardrailResult:
    """LLM 결정에 대한 최종 검문. 통과 시에만 approved=True."""
    # 0) HOLD는 항상 통과(주문 없음)
    if decision.action == "HOLD":
        return GuardrailResult(action="HOLD", quantity=0, approved=False,
                               reason=decision.reason or "HOLD", blocked=False,
                               signal=decision.signal, rule=decision.rule)

    if decision.action not in ("BUY", "SELL"):
        return _hold(f"알 수 없는 액션 '{decision.action}' → HOLD 처리")

    # 1) kill switch: 당일 손실 한도 도달 → 신규 매매 전면 중단
    if is_kill_switch_active(state, cfg):
        return _hold(
            f"Kill switch: 당일 실현손실 {state.realized_pnl_today:,}원 "
            f"(한도 -{cfg.daily_max_loss_krw:,}원) → 당일 매매 중단"
        )

    # 2) 장 운영시간 외 차단
    if not is_market_open(state.now):
        return _hold(f"장 운영시간 외({state.now:%H:%M}) 주문 차단")

    # 3) 하루 거래 횟수 초과
    if state.trades_today >= cfg.max_trades_per_day:
        return _hold(
            f"하루 거래 횟수 초과: {state.trades_today}/{cfg.max_trades_per_day}"
        )

    # 4) 멱등성: 같은 (종목, 방향) 직전 주문 중복 방지
    key = f"{decision.code}:{decision.action.lower()}"
    if key in state.recent_order_keys:
        return _hold(f"중복 주문 차단: {decision.code} {decision.action} 직전 주문과 동일")

    if current_price <= 0:
        return _hold(f"{decision.code} 현재가를 알 수 없어 주문 보류")

    if decision.quantity <= 0:
        return _hold(f"수량 {decision.quantity} → 주문 불가")

    if decision.action == "BUY":
        return _check_buy(decision, state, cfg, current_price)
    return _check_sell(decision, state, current_price)


def _check_buy(decision: Decision, state: AccountState, cfg: GuardrailConfig,
               price: int) -> GuardrailResult:
    qty = decision.quantity
    notes: list[str] = []

    # 1회 매수금액 한도 → 수량 축소
    max_by_order = floor(cfg.max_order_krw / price)
    if qty > max_by_order:
        notes.append(f"1회 한도({cfg.max_order_krw:,}원)로 {qty}→{max_by_order}주 축소")
        qty = max_by_order

    # 종목당 최대 비중 → 수량 축소
    if state.total_eval > 0:
        existing_val = int(state.positions.get(decision.code, {}).get("eval_amt", 0))
        max_pos_val = cfg.max_position_pct * state.total_eval
        room = max_pos_val - existing_val
        max_by_pos = floor(room / price) if room > 0 else 0
        if qty > max_by_pos:
            notes.append(
                f"종목비중 한도({cfg.max_position_pct:.0%})로 {qty}→{max(max_by_pos,0)}주 축소"
            )
            qty = max(max_by_pos, 0)

    # 가용 현금 한도 → 수량 축소
    max_by_cash = floor(state.available_cash / price)
    if qty > max_by_cash:
        notes.append(f"가용현금({state.available_cash:,}원)으로 {qty}→{max_by_cash}주 축소")
        qty = max_by_cash

    if qty <= 0:
        return _hold(f"BUY 차단: 한도/현금 부족으로 매수 가능 수량 0주. ({'; '.join(notes)})")

    adjusted = qty != decision.quantity
    guard_note = "; ".join(notes)
    reason = decision.reason
    if notes:
        reason = f"{decision.reason} | 가드레일: {guard_note}"
    return GuardrailResult(action="BUY", quantity=qty, approved=True,
                           reason=reason, adjusted=adjusted, blocked=False,
                           signal=decision.signal, rule=decision.rule,
                           guard_note=guard_note)


def _check_sell(decision: Decision, state: AccountState, price: int) -> GuardrailResult:
    held = int(state.positions.get(decision.code, {}).get("qty", 0))
    if held <= 0:
        return _hold(f"SELL 차단: {decision.code} 보유 수량 0주")

    qty = min(decision.quantity, held)
    adjusted = qty != decision.quantity
    guard_note = ""
    reason = decision.reason
    if adjusted:
        guard_note = f"보유 {held}주로 {decision.quantity}→{qty}주 축소"
        reason = f"{decision.reason} | 가드레일: {guard_note}"
    return GuardrailResult(action="SELL", quantity=qty, approved=True,
                           reason=reason, adjusted=adjusted, blocked=False,
                           signal=decision.signal, rule=decision.rule,
                           guard_note=guard_note)
