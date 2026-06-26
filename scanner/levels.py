"""수평 지지/저항 레벨 산출 — min/max를 넘어선 다중 기준.

  1) 스윙 피벗(전고점·전저점) 군집화 → '여러 번 부딪힌' 강한 선
  2) 선마다 강도 점수(터치 횟수 + 최근성) → 차트 굵기/우선순위
  3) 피보나치 되돌림 (직전 주요 파동 기준)
  4) 라운드 넘버 (심리적 가격대)
  5) 매물대 밸류영역 (POC + VAH/VAL, 거래량 70% 구간)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ────────────────────────────────────────────────────────────
# 1. 스윙 피벗
# ────────────────────────────────────────────────────────────
def swing_points(df: pd.DataFrame, k: int = None) -> list[dict]:
    """국소 고점/저점 + 그 봉의 거래량. High[i]가 좌우 k봉의 최대면 스윙고점, 최소면 저점.

    k는 지지/저항용 SR_SWING_K(기본 5) — 폭이 넓을수록 잔물결을 빼고 의미있는 변곡만.
    """
    k = k or config.SR_SWING_K
    highs, lows = df["High"].values, df["Low"].values
    vols = df["Volume"].values if "Volume" in df else [0.0] * len(df)
    n = len(df)
    pts = []
    for i in range(k, n - k):
        if highs[i] == highs[i - k:i + k + 1].max():
            pts.append({"pos": i, "price": float(highs[i]), "kind": "H",
                        "vol": float(vols[i])})
        if lows[i] == lows[i - k:i + k + 1].min():
            pts.append({"pos": i, "price": float(lows[i]), "kind": "L",
                        "vol": float(vols[i])})
    return pts


# ────────────────────────────────────────────────────────────
# 2. 군집화 + 강도
# ────────────────────────────────────────────────────────────
def cluster_levels(pts: list[dict], n_bars: int,
                   tol: float = None) -> list[dict]:
    """가까운(±tol) 스윙들을 한 레벨로 묶고 강도 점수를 매긴다."""
    tol = tol or config.SR_CLUSTER_TOL
    if not pts:
        return []
    pts = sorted(pts, key=lambda p: p["price"])
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        if abs(p["price"] - cur[-1]["price"]) / cur[-1]["price"] <= tol:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)

    # 거래량 가중 기준값(전체 스윙 거래량 중앙값) — 거래 몰린 변곡일수록 강한 벽
    all_vols = [m.get("vol", 0.0) for m in pts if m.get("vol", 0.0) > 0]
    med_vol = float(np.median(all_vols)) if all_vols else 0.0

    levels = []
    for c in clusters:
        prices = [m["price"] for m in c]
        last_pos = max(m["pos"] for m in c)
        touches = len(c)
        recency = last_pos / n_bars                     # 0~1, 최근일수록 큼
        avg_vol = float(np.mean([m.get("vol", 0.0) for m in c]))
        vol_factor = min(avg_vol / med_vol, 3.0) if med_vol > 0 else 1.0
        # 강도 = 터치(주) + 최근성 + 거래량가중(거래 몰린 가격일수록 가산)
        strength = touches + recency + (vol_factor - 1) * 0.6
        levels.append({
            "price": float(np.mean(prices)), "touches": touches,
            "last_pos": last_pos, "recency": round(recency, 2),
            "vol_factor": round(vol_factor, 2),
            "strength": round(strength, 2),
        })
    return sorted(levels, key=lambda l: l["strength"], reverse=True)


# ────────────────────────────────────────────────────────────
# 3. 피보나치 되돌림
# ────────────────────────────────────────────────────────────
def fibonacci(df: pd.DataFrame, lookback: int = None) -> dict:
    """직전 주요 파동(룩백 내 최저↔최고)의 되돌림 레벨."""
    lookback = lookback or config.SR_LOOKBACK
    seg = df.iloc[-lookback:]
    lo_i, hi_i = seg["Low"].idxmin(), seg["High"].idxmax()
    lo, hi = float(seg["Low"].min()), float(seg["High"].max())
    if hi <= lo:
        return {"levels": [], "direction": None}
    up = seg.index.get_loc(lo_i) < seg.index.get_loc(hi_i)  # 저점이 먼저면 상승파동
    rng = hi - lo
    levels = []
    for r in config.FIB_LEVELS:
        # 상승파동이면 고점에서 되돌림(지지), 하락파동이면 저점에서 되돌림(저항)
        price = hi - rng * r if up else lo + rng * r
        levels.append({"ratio": r, "price": round(price, 4)})
    return {"levels": levels, "direction": "상승" if up else "하락",
            "swing_low": lo, "swing_high": hi}


# ────────────────────────────────────────────────────────────
# 4. 라운드 넘버
# ────────────────────────────────────────────────────────────
def round_numbers(lo: float, hi: float, max_n: int = 6) -> list[float]:
    """[lo,hi] 범위의 라운드 가격대(10^n 및 5×10^(n-1) 배수)."""
    if lo <= 0 or hi <= lo:
        return []
    import math
    step = 10 ** math.floor(math.log10(hi)) / 2     # 절반 단위 간격
    start = math.ceil(lo / step) * step
    out, x = [], start
    while x <= hi and len(out) < max_n:
        out.append(round(x, 4))
        x += step
    return out


# ────────────────────────────────────────────────────────────
# 5. 매물대 밸류영역 (POC + VAH/VAL)
# ────────────────────────────────────────────────────────────
def value_area(df: pd.DataFrame, bins: int = None, lookback: int = None,
               va_pct: float = None) -> dict:
    """거래량 프로파일 → POC와 거래량 va_pct(기본 70%)가 몰린 구간(VAL~VAH)."""
    bins = bins or config.POC_BINS
    lookback = lookback or config.POC_LOOKBACK
    va_pct = va_pct or config.VALUE_AREA_PCT
    seg = df.iloc[-lookback:]
    lo, hi = float(seg["Low"].min()), float(seg["High"].max())
    if hi <= lo:
        p = float(seg["Close"].iloc[-1])
        return {"poc": p, "vah": p, "val": p}
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for low, high, v in zip(seg["Low"].values, seg["High"].values,
                            seg["Volume"].values):
        b0 = int(np.clip(np.digitize(low, edges) - 1, 0, bins - 1))
        b1 = int(np.clip(np.digitize(high, edges) - 1, 0, bins - 1))
        vol[b0:b1 + 1] += v / (b1 - b0 + 1)

    poc_b = int(vol.argmax())
    total = vol.sum()
    included = {poc_b}
    acc = vol[poc_b]
    lo_b = hi_b = poc_b
    # POC에서 양옆으로 거래량 많은 쪽을 더해가며 va_pct 도달까지 확장
    while acc < va_pct * total and (lo_b > 0 or hi_b < bins - 1):
        left = vol[lo_b - 1] if lo_b > 0 else -1
        right = vol[hi_b + 1] if hi_b < bins - 1 else -1
        if right >= left:
            hi_b += 1
            acc += vol[hi_b]; included.add(hi_b)
        else:
            lo_b -= 1
            acc += vol[lo_b]; included.add(lo_b)
    mid = lambda b: float((edges[b] + edges[b + 1]) / 2)
    return {"poc": mid(poc_b), "vah": mid(max(included)), "val": mid(min(included))}


# ────────────────────────────────────────────────────────────
# 통합
# ────────────────────────────────────────────────────────────
def confluence_zones(price: float, strong: list, va: dict, fib: dict,
                     rounds: list, recent_low: float | None,
                     below: bool = True, tol: float = 0.012) -> list[dict]:
    """여러 지지요소(지지선·매물대·라운드·피보·최근저점)가 겹치는 '반등 예상 구간'.

    각 요소를 가격으로 모은 뒤 ±tol 안에 든 것들을 한 구간으로 묶고, 서로 다른
    '종류'가 몇 개 겹치는지(컨플루언스)로 점수를 매긴다. 종류가 많이 겹칠수록 강함.
    below=True면 현재가 아래(반등 예상), False면 위(저항)만 본다.
    """
    items = []  # (price, type)
    for l in strong:
        items.append((l["price"], "지지선"))
    for key, lab in (("poc", "매물대(POC)"), ("val", "매물대(VAL)"),
                     ("vah", "매물대(VAH)")):
        if va.get(key):
            items.append((va[key], lab))
    for r in rounds:
        items.append((r, "라운드"))
    for fl in fib.get("levels", []):
        items.append((fl["price"], f"피보{fl['ratio']:.3f}"))
    if recent_low:
        items.append((recent_low, "최근저점"))

    # 방향 필터(아래/위)
    items = [(p, t) for p, t in items
             if p > 0 and (p < price if below else p > price)]
    items.sort(key=lambda x: x[0])

    # ±tol 군집화
    zones, cur = [], []
    for p, t in items:
        if cur and abs(p - cur[-1][0]) / cur[-1][0] <= tol:
            cur.append((p, t))
        else:
            if cur:
                zones.append(cur)
            cur = [(p, t)]
    if cur:
        zones.append(cur)

    out = []
    for z in zones:
        types = sorted({t for _p, t in z})           # 겹친 지지 '종류'
        prices = [p for p, _t in z]
        ctr = float(np.mean(prices))
        out.append({
            "center": ctr, "low": float(min(prices)), "high": float(max(prices)),
            "types": types, "n_types": len(types),
            "dist_pct": (ctr - price) / price * 100,
            "label": " + ".join(_short_type(t) for t in types),
        })
    # 컨플루언스(종류 수) 높고, 현재가에 가까운 순
    out.sort(key=lambda z: (-z["n_types"], abs(z["dist_pct"])))
    return out


def _short_type(t: str) -> str:
    return {"지지선": "지지선", "라운드": "라운드", "최근저점": "전저점"}.get(
        t, t.replace("매물대", "매물").replace("피보", "피보"))


def analyze_levels(df: pd.DataFrame) -> dict:
    """모든 기준을 합쳐 지지/저항 레벨 + 반등 예상 구간(컨플루언스) 반환."""
    lookback = config.SR_LOOKBACK
    seg = df.iloc[-lookback:]
    n = len(seg)
    price = float(df["Close"].iloc[-1])

    levels = cluster_levels(swing_points(seg), n)
    fib = fibonacci(df)
    va = value_area(df)
    rounds = round_numbers(float(seg["Low"].min()), float(seg["High"].max()))

    strong = [l for l in levels if l["touches"] >= config.SR_MIN_TOUCHES]
    supports = [l for l in strong if l["price"] < price]
    resists = [l for l in strong if l["price"] > price]
    nearest_sup = max(supports, key=lambda l: l["price"], default=None)
    nearest_res = min(resists, key=lambda l: l["price"], default=None)

    # 최근 횡보 박스 하단(단기 수요구간) — 최근 20봉 저점
    recent_low = float(df["Low"].iloc[-20:].min()) if len(df) >= 20 else None
    zones_below = confluence_zones(price, strong, va, fib, rounds, recent_low,
                                   below=True)
    # 종류 2개 이상 겹치는 구간만 '의미있는 반등 예상 구간'
    bounce = [z for z in zones_below if z["n_types"] >= 2][:3]

    return {
        "levels": levels, "strong": strong, "fib": fib, "value_area": va,
        "rounds": rounds, "nearest_support": nearest_sup,
        "nearest_resistance": nearest_res, "price": price,
        "bounce_zones": bounce,
    }
