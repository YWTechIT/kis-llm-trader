"""기술적 지표 계산 모듈 (pandas 기반).

reference/open-trading-api/strategy_builder/core/indicators.py 에서 현재 전략에
필요한 함수만 추려 왔다(그 디렉토리는 .gitignore 제외라 직접 import 불가).
모든 함수는 OHLCV DataFrame을 받아 지표 Series를 돌려주는 순수 함수로,
KIS API/인증에 의존하지 않는다. 기간 부족 시 예외 대신 빈 Series를 반환한다.

전략을 RSI·볼린저 등으로 바꿀 때는 reference의 calc_rsi / calc_bb_* 등을
같은 규칙으로 이 파일에 추가하면 된다.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def calc_ma(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """단순이동평균(SMA). 기간 부족 시 빈 Series."""
    if df.empty or len(df) < period:
        return pd.Series(dtype=float)
    return df[column].rolling(window=period).mean()


def calc_returns(df: pd.DataFrame, period: int) -> pd.Series:
    """기간 수익률(소수, 예 0.05=5%). 기간 부족 시 빈 Series."""
    if df.empty or len(df) < period:
        return pd.Series(dtype=float)
    return df["close"].pct_change(periods=period)


def calc_disparity(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """이격도 = 현재가/이동평균*100. 100 초과 과매수, 100 미만 과매도."""
    if df.empty or len(df) < period:
        return pd.Series(dtype=float)
    ma = calc_ma(df, period)
    return (df["close"] / ma) * 100


def calc_volatility(df: pd.DataFrame, period: int = 10) -> pd.Series:
    """변동성(일간 수익률의 이동표준편차). 기간 부족 시 빈 Series."""
    if df.empty or len(df) < period + 1:
        return pd.Series(dtype=float)
    return df["close"].pct_change().rolling(window=period).std()


def calc_consecutive_days(df: pd.DataFrame, direction: str = "up") -> int:
    """최근부터 역순으로 연속 상승/하락 일수. direction='up'|'down'."""
    if df.empty or len(df) < 2:
        return 0
    changes = df["close"].diff()
    condition = changes > 0 if direction == "up" else changes < 0
    count = 0
    for i in range(len(condition) - 1, 0, -1):
        if condition.iloc[i]:
            count += 1
        else:
            break
    return count


def calc_daily_change(df: pd.DataFrame) -> Optional[float]:
    """전일 대비 등락률(%, 예 5.0=5%). 데이터 부족/0가 분모면 None."""
    if df.empty or len(df) < 2:
        return None
    prev_close = df["close"].iloc[-2]
    curr_close = df["close"].iloc[-1]
    if prev_close == 0:
        return None
    return (curr_close - prev_close) / prev_close * 100


def calc_strong_close_ratio(df: pd.DataFrame) -> Optional[float]:
    """당일 봉의 종가 위치 비율 (Close-Low)/(High-Low). 1에 가까울수록 강세."""
    if df.empty:
        return None
    high = df["high"].iloc[-1]
    low = df["low"].iloc[-1]
    close = df["close"].iloc[-1]
    if high == low:  # 변동 없는 봉
        return 0.5
    return (close - low) / (high - low)


def ma_cross_signals(
    df: pd.DataFrame, short_period: int = 5, long_period: int = 20
) -> dict:
    """골든/데드크로스 신호 산출.

    df는 과거→현재로 정렬된 OHLCV(close 컬럼 필수). 직전 봉 대비 단/장기
    이평선의 교차 상태를 비교한다:
      - golden_cross: 직전 단기<=장기 → 당일 단기>장기 (상향 돌파)
      - dead_cross  : 직전 단기>=장기 → 당일 단기<장기 (하향 돌파)
    데이터가 부족하면 ma_error만 담아 돌려준다(상위에서 HOLD 처리).
    """
    if df.empty or len(df) < long_period + 1:
        return {"ma_error": f"이평선 계산용 일봉 부족(>= {long_period + 1}개 필요)"}

    ma_short = calc_ma(df, short_period)
    ma_long = calc_ma(df, long_period)
    # rolling 결과의 마지막 두 값(직전/당일)이 모두 유효해야 교차 판정 가능.
    if ma_short.iloc[-2:].isna().any() or ma_long.iloc[-2:].isna().any():
        return {"ma_error": "이평선 계산 실패(결측)"}

    prev_short, curr_short = float(ma_short.iloc[-2]), float(ma_short.iloc[-1])
    prev_long, curr_long = float(ma_long.iloc[-2]), float(ma_long.iloc[-1])

    golden = prev_short <= prev_long and curr_short > curr_long
    dead = prev_short >= prev_long and curr_short < curr_long
    return {
        f"ma{short_period}": round(curr_short, 1),
        f"ma{long_period}": round(curr_long, 1),
        "ma_short_above_long": curr_short > curr_long,
        "golden_cross": golden,
        "dead_cross": dead,
    }
