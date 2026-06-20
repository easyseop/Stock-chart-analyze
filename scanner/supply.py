"""매물대 심리 — 기간분리 평단가 + 미실현손익 추정.

거래량이 쌓인 가격 = 사람들이 산 가격(평단가)의 근사.
이를 이용해 단기/장기 평단가를 나누고, 현재가 기준 '머리 위 물량(물린 물량)'과
평균 보유자의 추정 손익을 계산한다. (실제 개인 평단가는 못 구하므로 OHLCV 근사)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


def volume_profile(df: pd.DataFrame, lookback: int, bins: int = None) -> dict:
    """가격대별 거래량 분포 + POC/VAH/VAL. 거래량은 봉의 [Low,High]에 분산."""
    bins = bins or config.PROFILE_BINS
    seg = df.iloc[-lookback:]
    lo, hi = float(seg["Low"].min()), float(seg["High"].max())
    if hi <= lo:
        p = float(seg["Close"].iloc[-1])
        return {"edges": np.array([p, p]), "centers": np.array([p]),
                "vol": np.array([seg["Volume"].sum()]),
                "poc": p, "vah": p, "val": p}
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    for low, high, v in zip(seg["Low"].values, seg["High"].values,
                            seg["Volume"].values):
        b0 = int(np.clip(np.digitize(low, edges) - 1, 0, bins - 1))
        b1 = int(np.clip(np.digitize(high, edges) - 1, 0, bins - 1))
        vol[b0:b1 + 1] += v / (b1 - b0 + 1)

    poc_b = int(vol.argmax())
    total = vol.sum()
    inc, acc, lo_b, hi_b = {poc_b}, vol[poc_b], poc_b, poc_b
    while acc < config.VALUE_AREA_PCT * total and (lo_b > 0 or hi_b < bins - 1):
        left = vol[lo_b - 1] if lo_b > 0 else -1
        right = vol[hi_b + 1] if hi_b < bins - 1 else -1
        if right >= left:
            hi_b += 1; acc += vol[hi_b]; inc.add(hi_b)
        else:
            lo_b -= 1; acc += vol[lo_b]; inc.add(lo_b)
    return {"edges": edges, "centers": centers, "vol": vol,
            "poc": float(centers[poc_b]),
            "vah": float(centers[max(inc)]), "val": float(centers[min(inc)])}


def unrealized(profile: dict, price: float) -> dict:
    """매물대 분포 → 추정 평단가·미실현손익·머리 위 물량 비율."""
    centers, vol = profile["centers"], profile["vol"]
    total = vol.sum()
    if total <= 0:
        return {"avg_cost": price, "pnl": 0.0, "overhead": 0.0,
                "head": "정보없음", "mood": "중립"}
    avg_cost = float((centers * vol).sum() / total)          # 거래량 가중 평단
    overhead = float(vol[centers > price].sum() / total)     # 현재가 위 물량(물린)
    pnl = (price - avg_cost) / avg_cost                       # 평균 보유자 손익률

    if overhead >= config.OVERHEAD_HEAVY:
        head = "무거움(위 물량 많음)"
    elif overhead <= config.OVERHEAD_LIGHT:
        head = "가벼움(저항 적음)"
    else:
        head = "보통"

    if pnl >= config.PNL_BAND:
        mood = "평균 이익(차익실현 압력)"
    elif pnl <= -config.PNL_BAND:
        mood = "평균 손실(본전 저항)"
    else:
        mood = "평단 부근(중립)"

    return {"avg_cost": avg_cost, "pnl": pnl, "overhead": overhead,
            "head": head, "mood": mood}


def analyze_supply(df: pd.DataFrame) -> dict:
    """기간분리 매물대(단기/장기) + 미실현손익(장기 기준) 통합."""
    price = float(df["Close"].iloc[-1])
    short = volume_profile(df, config.SHORT_PROFILE_DAYS)
    long = volume_profile(df, config.LONG_PROFILE_DAYS)
    pnl = unrealized(long, price)                            # 1년 보유자 기준
    # 단기 평단이 현재가 위면 최근 매수자가 물려 단기 저항, 아래면 단기 지지
    short_role = "저항" if short["poc"] > price else "지지"
    long_role = "저항" if long["poc"] > price else "지지"
    return {"price": price, "short": short, "long": long, "pnl": pnl,
            "short_poc": short["poc"], "long_poc": long["poc"],
            "short_role": short_role, "long_role": long_role,
            "terms": ["매물대", "미실현손익", "평단가"]}
