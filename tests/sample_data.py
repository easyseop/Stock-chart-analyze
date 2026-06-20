"""오프라인 검증용 합성 OHLCV 생성기.

네트워크가 막힌 환경에서 파이프라인 로직을 검증하기 위한 가짜 데이터.
실데이터 수집(scanner.data.fetch_daily)을 대체하는 용도일 뿐, 신호의 의미는 없다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make(scenario: str = "uptrend", n: int = 800, seed: int = 0,
         start_price: float = 100.0) -> pd.DataFrame:
    """시나리오별 일봉 생성.
    scenario: uptrend / downtrend / box(횡보) / breakdown(박스이탈)
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-01", periods=n)
    drift = {"uptrend": 0.0015, "downtrend": -0.0015, "box": 0.0,
             "breakdown": 0.0, "reversal": -0.0015}[scenario]
    vol = 0.008  # 노이즈를 낮춰 추세 시나리오에서 ADX가 추세장으로 잡히게
    rets = rng.normal(drift, vol, n)

    if scenario == "box":
        # 평균회귀로 박스권 형성
        price = [start_price]
        center = start_price
        for i in range(1, n):
            pull = (center - price[-1]) / center * 0.05
            price.append(price[-1] * (1 + rng.normal(pull, 0.01)))
        close = np.array(price)
    elif scenario == "breakdown":
        price = [start_price]
        center = start_price
        for i in range(1, n):
            if i < n - 5:
                pull = (center - price[-1]) / center * 0.05
                price.append(price[-1] * (1 + rng.normal(pull, 0.01)))
            else:  # 마지막 5봉 급락 → 박스 하단 이탈
                price.append(price[-1] * (1 - 0.03))
        close = np.array(price)
    elif scenario == "reversal":
        # 장기 하락 후 '최근 10봉'에서 하락추세선을 막 상향 돌파(추세 전환 순간)
        close = start_price * np.exp(np.cumsum(rets))
        turn = n - 10
        for i in range(turn, n):
            close[i] = close[i - 1] * (1 + abs(rng.normal(0.02, 0.006)))
    else:
        close = start_price * np.exp(np.cumsum(rets))

    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.integers(1_000_000, 3_000_000, n).astype(float)
    volume[-1] *= 2.5  # 마지막 봉 거래량 급증

    df = pd.DataFrame({"Open": open_, "High": np.maximum(high, close),
                       "Low": np.minimum(low, close), "Close": close,
                       "Volume": volume}, index=dates)
    return df
