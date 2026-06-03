"""전략 프리셋 레지스트리.

reference/open-trading-api/strategy_builder 의 10개 프리셋 전략을 이 프로젝트
구조(코드는 지표 계산만, 매매 판단은 LLM)에 맞게 옮긴 것이다. 즉 reference의
`if 조건: return Signal(BUY)` 결정 로직은 가져오지 않고, 대신:

  - signals(df, price_info) → LLM 스냅샷에 넣을 지표 dict (임계값 판정 결과 포함)
  - spec                    → 그 지표를 어떻게 해석할지 LLM에게 주는 SPEC 텍스트

로 분해했다. 최종 BUY/SELL/HOLD는 항상 LLM(strategy/llm_decider.py)이 정한다.

활성 전략은 config의 STRATEGY_NAME(.env)으로 고른다. 지금은 golden_cross만
기본 활성이고, 나머지는 후보로 등록만 되어 있다 — 전환은 .env 한 줄로 끝난다.

새 전략 추가 절차:
  1) 필요한 지표 계산을 strategy/indicators.py 에 함수로 추가
  2) signals 함수 + spec 텍스트를 가진 StrategyPreset 을 만들어 PRESETS 에 등록
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from strategy import indicators

# signals(df, price_info) -> dict.  df는 과거→현재 OHLCV, price_info는 get_price 결과.
SignalFn = Callable[[pd.DataFrame, dict], dict]


@dataclass(frozen=True)
class StrategyPreset:
    key: str          # config STRATEGY_NAME 으로 지정하는 식별자
    name: str         # 사람이 읽는 이름(한국어)
    min_days: int     # signals 계산에 필요한 최소 일봉 수
    signals: SignalFn
    spec: str         # LLM 시스템 프롬프트에 들어갈 전략 명세 텍스트


# ── 공통 운용 목표/주의 (모든 전략 SPEC 앞뒤에 붙는 머리말·꼬리말) ──────────────
_HEADER = """\
[운용 목표]
- 소액(약 50만원) 단기 스윙. 큰 손실 회피가 수익보다 우선.
- 한 번에 전량 매수하지 말고 1주 단위로 분할 진입. 비중이 큰 종목은 추가 진입 자제.
"""

_FOOTER = """\
[관망(HOLD) 조건]
- 방향성이 불분명하거나 변동성이 과도하면 HOLD.
- 확신이 없으면 항상 HOLD를 선택(불필요한 매매 금지).
- 아래 지표가 결측(error 키 존재)이면 판단 불가로 보고 HOLD.

[주의]
- 손절/한도/거래횟수 등 안전장치는 코드가 강제하므로 여기서 신경 쓰지 않는다.
- 너는 BUY/SELL/HOLD 중 하나와 수량만 제안한다. 실행 여부는 코드가 최종 결정한다.
"""


def _spec(body: str) -> str:
    return f"{_HEADER}\n{body}\n{_FOOTER}"


# ─────────────────────────────────────────────────────────────────────────────
# 01. 골든크로스 — MA5/MA20 교차
# ─────────────────────────────────────────────────────────────────────────────
def _golden_cross_signals(df: pd.DataFrame, price_info: dict) -> dict:
    return indicators.ma_cross_signals(df, short_period=5, long_period=20)


GOLDEN_CROSS = StrategyPreset(
    key="golden_cross",
    name="골든크로스",
    min_days=21,
    signals=_golden_cross_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- 골든크로스(golden_cross=true): MA5가 MA20을 아래에서 위로 막 돌파한 초입에서 매수 고려.
- 이미 ma_short_above_long=true 로 한참 진행된 추세 후반에는 신규 진입 자제.

[청산(SELL) 조건]
- 데드크로스(dead_cross=true): MA5가 MA20을 하향 돌파하면 추세 이탈로 보고 보유분 축소.
- 매수 평균가 대비 +3% 이상이면 일부 익절 고려."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 02. 모멘텀 — 60일 수익률
# ─────────────────────────────────────────────────────────────────────────────
def _momentum_signals(df: pd.DataFrame, price_info: dict) -> dict:
    lookback = 60
    if len(df) < lookback:
        return {"error": f"모멘텀 계산용 일봉 부족(>= {lookback}개 필요)"}
    ret = indicators.calc_returns(df, lookback).iloc[-1]
    if pd.isna(ret):
        return {"error": "수익률 계산 실패"}
    ret_pct = round(float(ret) * 100, 1)
    return {
        "momentum_return_pct": ret_pct,        # 60일 수익률(%)
        "momentum_strong_up": ret_pct >= 30.0,  # 매수 후보 구간
        "momentum_strong_down": ret_pct <= -20.0,  # 매도 후보 구간
    }


MOMENTUM = StrategyPreset(
    key="momentum",
    name="모멘텀",
    min_days=65,
    signals=_momentum_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- momentum_strong_up=true (60일 수익률 +30% 이상): 강한 상승 모멘텀 → 매수 고려.

[청산(SELL) 조건]
- momentum_strong_down=true (60일 수익률 -20% 이하): 모멘텀 붕괴 → 보유분 축소."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 03. 52주 신고가 — 현재가 vs 52주 최고가 (일봉 불필요, price_info 사용)
# ─────────────────────────────────────────────────────────────────────────────
def _week52_high_signals(df: pd.DataFrame, price_info: dict) -> dict:
    price = price_info.get("price", 0)
    w52_high = price_info.get("w52_high", 0)
    if not price or not w52_high:
        return {"error": "현재가/52주 고가 정보 없음"}
    return {
        "w52_high": w52_high,
        "price_vs_w52_high_pct": round(price / w52_high * 100, 1),
        "breakout_w52_high": price > w52_high,  # 신고가 돌파
    }


WEEK52_HIGH = StrategyPreset(
    key="week52_high",
    name="52주 신고가",
    min_days=0,  # 현재가 API의 52주 고가만 사용
    signals=_week52_high_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- breakout_w52_high=true (현재가가 52주 최고가 돌파): 신고가 돌파 → 매수 고려.

[청산(SELL) 조건]
- 별도 매도 신호 없음(매수 전용). 매수 평균가 대비 손실 확대 시에만 보유분 축소 고려."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 04. 연속 상승/하락
# ─────────────────────────────────────────────────────────────────────────────
def _consecutive_signals(df: pd.DataFrame, price_info: dict) -> dict:
    if len(df) < 6:
        return {"error": "연속 상승/하락 계산용 일봉 부족(>= 6개 필요)"}
    up = indicators.calc_consecutive_days(df, "up")
    down = indicators.calc_consecutive_days(df, "down")
    return {
        "consecutive_up_days": up,
        "consecutive_down_days": down,
        "up_streak_5plus": up >= 5,
        "down_streak_5plus": down >= 5,
    }


CONSECUTIVE = StrategyPreset(
    key="consecutive",
    name="연속 상승/하락",
    min_days=10,
    signals=_consecutive_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- up_streak_5plus=true (5일 이상 연속 상승): 상승 추세 지속 → 매수 고려.

[청산(SELL) 조건]
- down_streak_5plus=true (5일 이상 연속 하락): 하락 추세 지속 → 보유분 축소."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 05. 이격도 — 20일 이동평균 대비
# ─────────────────────────────────────────────────────────────────────────────
def _disparity_signals(df: pd.DataFrame, price_info: dict) -> dict:
    period = 20
    if len(df) < period:
        return {"error": f"이격도 계산용 일봉 부족(>= {period}개 필요)"}
    disp = indicators.calc_disparity(df, period).iloc[-1]
    if pd.isna(disp):
        return {"error": "이격도 계산 실패"}
    disp = round(float(disp), 1)
    return {
        "disparity_20": disp,
        "oversold": disp < 90.0,    # 과매도(매수 후보)
        "overbought": disp > 110.0,  # 과매수(매도 후보)
    }


DISPARITY = StrategyPreset(
    key="disparity",
    name="이격도",
    min_days=30,
    signals=_disparity_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- oversold=true (이격도 20일선 대비 90 미만, 과매도): 반등 기대 → 매수 고려.

[청산(SELL) 조건]
- overbought=true (이격도 110 초과, 과매수): 단기 과열 → 보유분 축소."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 06. 돌파 실패 — 최근 고점 돌파 후 되밀림 (매도 전용)
# ─────────────────────────────────────────────────────────────────────────────
def _breakout_fail_signals(df: pd.DataFrame, price_info: dict) -> dict:
    lookback, within = 20, 3
    if len(df) < lookback + within + 1:
        return {"error": f"돌파 실패 계산용 일봉 부족(>= {lookback + within + 1}개 필요)"}
    recent_high = float(df["high"].iloc[-within:].max())
    prev_high = float(df["high"].iloc[:-within].max())
    curr_close = float(df["close"].iloc[-1])
    broke_out = recent_high > prev_high
    change_from_high = round((curr_close - recent_high) / recent_high * 100, 1)
    return {
        "recent_high": recent_high,
        "prev_high": prev_high,
        "broke_prev_high": broke_out,
        "pct_from_recent_high": change_from_high,
        # 돌파했는데 고점 대비 -3% 이상 되밀림 → 돌파 실패
        "breakout_failed": broke_out and change_from_high <= -3.0,
    }


BREAKOUT_FAIL = StrategyPreset(
    key="breakout_fail",
    name="돌파 실패",
    min_days=24,
    signals=_breakout_fail_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- 별도 매수 신호 없음(매도 전용 전략).

[청산(SELL) 조건]
- breakout_failed=true (최근 고점 돌파 후 고점 대비 -3% 이상 되밀림): 돌파 실패 → 보유분 축소."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 07. 강한 종가 — 종가가 당일 고가 근처에서 마감 (장마감 후 권장)
# ─────────────────────────────────────────────────────────────────────────────
def _strong_close_signals(df: pd.DataFrame, price_info: dict) -> dict:
    if df.empty:
        return {"error": "일봉 없음"}
    ratio = indicators.calc_strong_close_ratio(df)
    if ratio is None:
        return {"error": "종가 위치 계산 실패"}
    ratio = float(ratio)
    return {
        "close_position_pct": round(ratio * 100, 0),  # 종가의 (저가~고가) 내 위치(%)
        "strong_close": bool(ratio >= 0.8),            # 고가 상위 80% 이상 마감
    }


STRONG_CLOSE = StrategyPreset(
    key="strong_close",
    name="강한 종가",
    min_days=1,
    signals=_strong_close_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- strong_close=true (종가가 당일 고저 범위 상위 80% 이상에서 마감): 매수세 강함 → 매수 고려.
- 주의: 장중에는 고저/종가가 확정되지 않아 신호가 부정확하다. 장마감 후 판단이 정확.

[청산(SELL) 조건]
- 별도 매도 신호 없음(매수 전용)."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 08. 변동성 확장 — 변동성 최저 수축 후 당일 급등
# ─────────────────────────────────────────────────────────────────────────────
def _volatility_signals(df: pd.DataFrame, price_info: dict) -> dict:
    lookback = 10
    if len(df) < lookback + 1:
        return {"error": f"변동성 계산용 일봉 부족(>= {lookback + 1}개 필요)"}
    vol = indicators.calc_volatility(df, lookback)
    if vol.empty or pd.isna(vol.iloc[-1]):
        return {"error": "변동성 계산 실패"}
    current_vol = float(vol.iloc[-1])
    min_vol = float(vol.iloc[-lookback:].min())
    change_pct = indicators.calc_daily_change(df)
    at_low_vol = bool(current_vol <= min_vol * 1.1)  # 변동성 최저 수축 구간
    return {
        "at_low_volatility": at_low_vol,
        "daily_change_pct": round(float(change_pct), 1) if change_pct is not None else None,
        # 수축 후 당일 +3% 이상 → 변동성 확장 돌파
        "volatility_breakout": bool(
            at_low_vol and change_pct is not None and change_pct >= 3.0
        ),
    }


VOLATILITY = StrategyPreset(
    key="volatility",
    name="변동성 확장",
    min_days=20,
    signals=_volatility_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- volatility_breakout=true (변동성 최저 수축 후 당일 +3% 이상 상승): 확장 돌파 → 매수 고려.

[청산(SELL) 조건]
- 별도 매도 신호 없음(매수 전용)."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 09. 평균회귀 — 5일 이동평균 대비 이탈
# ─────────────────────────────────────────────────────────────────────────────
def _mean_reversion_signals(df: pd.DataFrame, price_info: dict) -> dict:
    period = 5
    if len(df) < period:
        return {"error": f"평균회귀 계산용 일봉 부족(>= {period}개 필요)"}
    ma = indicators.calc_ma(df, period)
    ma_value = ma.iloc[-1]
    curr_close = float(df["close"].iloc[-1])
    if pd.isna(ma_value) or ma_value == 0:
        return {"error": "이동평균 계산 실패"}
    deviation = round((curr_close - float(ma_value)) / float(ma_value) * 100, 1)
    return {
        "ma5": round(float(ma_value), 1),
        "deviation_from_ma5_pct": deviation,
        "below_ma_3pct": deviation <= -3.0,  # 평균 대비 -3% 이하(매수 후보)
        "above_ma_3pct": deviation >= 3.0,   # 평균 대비 +3% 이상(매도 후보)
    }


MEAN_REVERSION = StrategyPreset(
    key="mean_reversion",
    name="평균회귀",
    min_days=10,
    signals=_mean_reversion_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- below_ma_3pct=true (5일 평균 대비 -3% 이하 이탈): 평균 회귀 반등 기대 → 매수 고려.

[청산(SELL) 조건]
- above_ma_3pct=true (5일 평균 대비 +3% 이상 이탈): 평균 회귀 되돌림 기대 → 보유분 축소."""),
)


# ─────────────────────────────────────────────────────────────────────────────
# 10. 추세 필터 — 60일선 대비 위치 + 당일 방향
# ─────────────────────────────────────────────────────────────────────────────
def _trend_filter_signals(df: pd.DataFrame, price_info: dict) -> dict:
    period = 60
    if len(df) < period:
        return {"error": f"추세 필터 계산용 일봉 부족(>= {period}개 필요)"}
    ma = indicators.calc_ma(df, period)
    ma_value = ma.iloc[-1]
    if pd.isna(ma_value):
        return {"error": "이동평균 계산 실패"}
    curr_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    above_ma = curr_close > float(ma_value)
    daily_up = curr_close > prev_close
    return {
        "ma60": round(float(ma_value), 1),
        "above_ma60": above_ma,
        "daily_up": daily_up,
        "uptrend": bool(above_ma and daily_up),          # 매수 후보
        "downtrend": bool((not above_ma) and (not daily_up)),  # 매도 후보
    }


TREND_FILTER = StrategyPreset(
    key="trend_filter",
    name="추세 필터",
    min_days=70,
    signals=_trend_filter_signals,
    spec=_spec("""\
[진입(BUY) 조건]
- uptrend=true (종가가 MA60 위 + 당일 상승): 상승 추세 확인 → 매수 고려.

[청산(SELL) 조건]
- downtrend=true (종가가 MA60 아래 + 당일 하락): 하락 추세 확인 → 보유분 축소."""),
)


# ── 레지스트리 ──────────────────────────────────────────────────────────────
_ALL = [
    GOLDEN_CROSS, MOMENTUM, WEEK52_HIGH, CONSECUTIVE, DISPARITY,
    BREAKOUT_FAIL, STRONG_CLOSE, VOLATILITY, MEAN_REVERSION, TREND_FILTER,
]
PRESETS: dict[str, StrategyPreset] = {p.key: p for p in _ALL}

DEFAULT_STRATEGY = "golden_cross"


def get_preset(key: Optional[str]) -> StrategyPreset:
    """key에 해당하는 프리셋 반환. 비었거나 미등록이면 기본(golden_cross)."""
    if not key:
        return PRESETS[DEFAULT_STRATEGY]
    preset = PRESETS.get(key.strip().lower())
    if preset is None:
        raise KeyError(
            f"알 수 없는 STRATEGY_NAME='{key}'. 사용 가능: {', '.join(PRESETS)}"
        )
    return preset
