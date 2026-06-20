"""추세선 검출 — 하락추세선/상승추세선과 그 상태(지속·임박·돌파).

사용자 원칙: '하락추세엔 절대 안 건드린다'.
→ 하락추세 지속이면 매수 신호를 막고(veto), 하락추세선에 근접(끝나감)하거나
  상향 돌파(추세 전환)하는 종목을 별도로 포착한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from .levels import swing_points

# 전환 후보(매수 검토 대상) 상태 — 대시보드/백테스트가 공유(문자열 드리프트 방지)
TRANSITION_CONFIRMED = "추세 전환 확정"      # 돌파 + 안착 + 상승추세선 형성
TRANSITION_PENDING = "돌파 후 횡보·전환대기"  # 돌파 + 안착(상승추세선은 아직)
BREAKOUT_UNCONFIRMED = "하락추세선 돌파(미확인)"  # 갓 돌파 — 되밀림 위험
TRANSITION_STATES = {TRANSITION_CONFIRMED, TRANSITION_PENDING}


def _fit_through(p0: dict, p1: dict):
    """두 피벗을 잇는 직선 (slope, intercept) — x는 봉 위치(pos)."""
    slope = (p1["price"] - p0["price"]) / (p1["pos"] - p0["pos"])
    intercept = p0["price"] - slope * p0["pos"]
    return slope, intercept


def _line_value(slope, intercept, pos):
    return slope * pos + intercept


def _recent_pivots(pts, kind, n_take=4):
    """가장 최근 스윙 피벗들(kind='H' or 'L')을 시간순으로."""
    sel = [p for p in pts if p["kind"] == kind]
    return sorted(sel, key=lambda p: p["pos"])[-n_take:]


def detect(df: pd.DataFrame, frames: dict | None = None,
           lookback: int = None) -> dict:
    """추세선 상태 판정."""
    lookback = lookback or config.TREND_LOOKBACK
    win = df.iloc[-lookback:]
    dates = list(win.index.to_pydatetime())   # 차트 그리기용 실제 날짜
    seg = win.reset_index(drop=True)
    n = len(seg)
    if n < 2 * config.SWING_K + 5:
        return _empty()
    price = float(seg["Close"].iloc[-1])
    last_pos = n - 1
    near = config.TREND_NEAR
    pts = swing_points(seg)

    # ── 하락추세선: 최근 '낮아지는 고점'들을 잇는다 ──
    highs = _recent_pivots(pts, "H")
    down = None
    if len(highs) >= 2:
        h_prev, h_last = highs[-2], highs[-1]
        if h_last["price"] < h_prev["price"]:            # 낮아진 고점
            slope, b = _fit_through(h_prev, h_last)
            dn_now = _line_value(slope, b, last_pos)
            touches = sum(1 for p in highs
                          if abs(p["price"] - _line_value(slope, b, p["pos"]))
                          / p["price"] <= near)
            down = {"slope": slope, "now": float(dn_now), "touches": touches,
                    "p0": h_prev, "p1": h_last,
                    "x0": dates[h_prev["pos"]], "y0": h_prev["price"],
                    "x1": dates[last_pos], "y1": float(dn_now)}

    # ── 상승추세선: 최근 '높아지는 저점'들을 잇는다 ──
    lows = _recent_pivots(pts, "L")
    up = None
    if len(lows) >= 2:
        l_prev, l_last = lows[-2], lows[-1]
        if l_last["price"] > l_prev["price"]:            # 높아진 저점
            slope, b = _fit_through(l_prev, l_last)
            up_now = _line_value(slope, b, last_pos)
            touches = sum(1 for p in lows
                          if abs(p["price"] - _line_value(slope, b, p["pos"]))
                          / p["price"] <= near)
            up = {"slope": slope, "now": float(up_now), "touches": touches,
                  "p0": l_prev, "p1": l_last,
                  "x0": dates[l_prev["pos"]], "y0": l_prev["price"],
                  "x1": dates[last_pos], "y1": float(up_now)}

    closes = seg["Close"].values

    # ── 상태/점수 판정 (하락추세선 우선) ──
    state = "추세선 불명확"
    score = 0
    confirmed_down = False
    note = "뚜렷한 추세선 없음"

    if down and down["slope"] < 0:
        slope = down["slope"]
        b = down["p1"]["price"] - slope * down["p1"]["pos"]   # 절편
        dn = down["now"]
        # 최근 N봉 내에 '선 아래'였다가 현재 '선 위'면 갓 돌파한 것
        lookN = max(3, 2 * config.SWING_K + 4)
        recently_below = any(closes[i] < _line_value(slope, b, i)
                             for i in range(max(0, last_pos - lookN), last_pos))
        if price > dn and recently_below:
            # 돌파만으론 부족(되밀림 위험) → 안착(횡보)·상승추세선 형성까지 등급화
            held = 0                                  # 현재부터 거꾸로 '선 위' 연속 봉 수
            for i in range(last_pos, -1, -1):
                if closes[i] > _line_value(slope, b, i):
                    held += 1
                else:
                    break
            has_uptrend = bool(up and up["slope"] > 0
                               and price >= up["now"] * (1 - near))
            if held >= config.TRANSITION_MIN_HOLD and has_uptrend:
                state, score, note = (TRANSITION_CONFIRMED, 2,
                    f"하락추세선 돌파 후 {held}봉 안착 + 상승추세선(저점 높아짐) 형성 "
                    "→ 추세 전환 확정 ⭐")
            elif held >= config.TRANSITION_MIN_HOLD:
                state, score, note = (TRANSITION_PENDING, 1,
                    f"하락추세선 돌파 후 {held}봉 안착(횡보) "
                    "→ 상승추세 전환 대기(상승추세선 미형성)")
            else:
                state, score, note = (BREAKOUT_UNCONFIRMED, 0,
                    f"하락추세선 갓 돌파({held}봉) → 되밀림 위험, 안착 확인 필요")
        elif dn * (1 - near) <= price <= dn:
            state, score, note = ("하락추세선 임박", 1,
                                  "하락추세선 바로 아래 → 하락 추세가 끝나갈 가능성")
        elif price < dn:
            state, score, note = ("하락추세 지속", -2,
                                  "하락추세선 아래 (고점 낮아짐) → 매수 자제/회피")
            confirmed_down = True

    if state == "추세선 불명확" and up and up["slope"] > 0:
        un = up["now"]
        if price < un * (1 - near):
            state, score, note = ("상승추세선 이탈", -2,
                                  "상승추세선 하향 이탈 → 상승 추세 훼손")
        else:
            state, score, note = ("상승추세 유지", 1,
                                  "상승추세선 위 (저점 높아짐) → 추세 양호")

    # 현재 활성 추세선의 가격값(선이 '지금' 어디 있는지) + 현재가와의 거리
    if state == TRANSITION_CONFIRMED and up:        # 전환확정 → 새 상승추세선(지지)
        level, slope_sign = up["now"], "상승(↗)"
    elif (state in (TRANSITION_PENDING, BREAKOUT_UNCONFIRMED)
          or state.startswith("하락")) and down:    # 돌파/임박/지속 → 깬 하락선
        level, slope_sign = down["now"], "하락(↘)"
    elif state.startswith("상승") and up:
        level, slope_sign = up["now"], "상승(↗)"
    else:
        level, slope_sign = None, "-"
    dist_pct = (price - level) / level * 100 if level else None

    return {"state": state, "score": score, "note": note,
            "confirmed_down": confirmed_down, "down": down, "up": up,
            "level": level, "slope_sign": slope_sign, "dist_pct": dist_pct,
            "reason": f"{state}", "terms": ["추세선"]}


def apply_volume_filter(tlres: dict, vol_mult: float) -> dict:
    """하락추세선 '상향 돌파'에 거래량 동반 조건을 적용한다(백테스트 검증).

    거래량 미동반 돌파는 가짜 돌파가 많아(기대값 음) → '거래 미동반 돌파'로 격하해
    매수 점수를 빼고 관망으로 돌린다. 거래량을 동반하면 그대로 전환 후보로 인정.
    """
    # 거래량 확인은 '추세 전환 확정'(최상위 매수 신호)에만 적용
    if tlres.get("state") != TRANSITION_CONFIRMED:
        tlres.setdefault("volume_confirmed", None)   # 대상 아님 → 해당없음
        return tlres
    if vol_mult >= config.VOLUME_CONFIRM_MULT:
        tlres["volume_confirmed"] = True
        tlres["note"] += f" (거래량 {vol_mult:.1f}배 동반 ✓)"
        return tlres
    # 거래량 미동반 → '전환대기'로 격하(가짜 전환 위험)
    tlres["state"] = TRANSITION_PENDING
    tlres["score"] = 1
    tlres["note"] = (f"추세 전환 형태이나 거래량 미동반({vol_mult:.1f}배) "
                     f"→ 거래량 확인 후 판단(전환 대기)")
    tlres["reason"] = tlres["state"]
    tlres["volume_confirmed"] = False
    return tlres


def _empty():
    return {"state": "추세선 불명확", "score": 0, "note": "데이터 부족",
            "confirmed_down": False, "down": None, "up": None,
            "level": None, "slope_sign": "-", "dist_pct": None,
            "reason": "추세선 불명확", "terms": ["추세선"]}
