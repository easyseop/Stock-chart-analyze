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


def _best_line(pts, kind: str, near: float, min_span: int, min_touches: int):
    """여러 스윙 피벗을 가장 잘 지나는 추세선(최소 터치·최소 길이·미관통).

    kind='H'(하락저항선: 기울기<0, 고점이 선 위로 안 뚫림),
    kind='L'(상승지지선: 기울기>0, 저점이 선 아래로 안 뚫림).
    반환: (slope, intercept, p0, p1, touches) 또는 None.
    """
    P = sorted([p for p in pts if p["kind"] == kind], key=lambda p: p["pos"])
    best = None
    for i in range(len(P)):
        for j in range(i + 1, len(P)):
            p0, p1 = P[i], P[j]
            span = p1["pos"] - p0["pos"]
            if span < min_span:
                continue
            slope = (p1["price"] - p0["price"]) / span
            if kind == "H" and slope >= 0:
                continue
            if kind == "L" and slope <= 0:
                continue
            b = p0["price"] - slope * p0["pos"]
            valid, touches = True, 0
            for p in P:
                lv = slope * p["pos"] + b
                dev = (p["price"] - lv) / lv if lv else 0.0
                if kind == "H" and dev > near:        # 고점이 저항선 위로 관통 → 무효
                    valid = False
                    break
                if kind == "L" and dev < -near:       # 저점이 지지선 아래로 관통 → 무효
                    valid = False
                    break
                if abs(dev) <= near:
                    touches += 1
            if not valid or touches < min_touches:
                continue
            cand = (touches, span)                    # 터치 많고 길수록 우선
            if best is None or cand > best[0]:
                best = (cand, (slope, b, p0, p1, touches))
    return best[1] if best else None


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

    mt, ms = config.TREND_MIN_TOUCHES, config.TREND_MIN_SPAN

    def _seg(fit):
        slope, b, p0, p1, touches = fit
        now = _line_value(slope, b, last_pos)
        return {"slope": slope, "now": float(now), "touches": touches,
                "p0": p0, "p1": p1,
                "x0": dates[p0["pos"]], "y0": p0["price"],
                "x1": dates[last_pos], "y1": float(now)}

    # ── 하락추세선(저항): 낮아지는 고점들을 최적합(3터치↑·길이↑·미관통) ──
    fit_d = _best_line(pts, "H", near, ms, mt)
    down = _seg(fit_d) if fit_d else None
    # ── 상승추세선(지지): 높아지는 저점들을 최적합 ──
    fit_u = _best_line(pts, "L", near, ms, mt)
    up = _seg(fit_u) if fit_u else None

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

    # 거리 필터: 현재가에서 너무 먼(낡은) 선은 매매에 무의미 → 참고없음으로 강등.
    # 단, 돌파/임박 등 '선 근처' 신호는 정의상 가까우니 제외.
    if (level is not None and abs(dist_pct) > config.TREND_MAX_DIST * 100
            and state in ("하락추세 지속", "상승추세 유지", "상승추세선 이탈")):
        state, score, confirmed_down = "추세선 불명확", 0, False
        note = f"추세선이 현재가에서 {dist_pct:+.0f}%로 멀어 참고 안 함(낡은 선)"
        level, dist_pct = None, None

    return {"state": state, "score": score, "note": note,
            "confirmed_down": confirmed_down, "down": down, "up": up,
            "level": level, "slope_sign": slope_sign, "dist_pct": dist_pct,
            "reason": f"{state}", "terms": ["추세선"]}


def apply_confirm_filter(tlres: dict, vol_mult: float, rsi_val: float) -> dict:
    """'추세 전환 확정'에 콤보 확인(거래량 동반 + RSI 과열 회피)을 적용한다.

    백테스트 검증: 거래량≥1.3배 & RSI<70 콤보가 전환 후보 기대값 최고(+0.23R, PF 1.40).
    둘 다 충족해야 '확정', 미충족이면 '전환 대기'로 격하(가짜 전환·추격 위험).
    """
    if tlres.get("state") != TRANSITION_CONFIRMED:
        tlres.setdefault("volume_confirmed", None)   # 대상 아님 → 해당없음
        return tlres
    vol_ok = vol_mult >= config.TRANSITION_VOL_MULT
    rsi_ok = rsi_val < config.TRANSITION_RSI_MAX
    if vol_ok and rsi_ok:
        tlres["volume_confirmed"] = True
        tlres["note"] += f" (거래량 {vol_mult:.1f}배·RSI {rsi_val:.0f} 확인 ✓)"
        return tlres
    miss = []
    if not vol_ok:
        miss.append(f"거래량 {vol_mult:.1f}배<{config.TRANSITION_VOL_MULT}")
    if not rsi_ok:
        miss.append(f"RSI {rsi_val:.0f}≥{config.TRANSITION_RSI_MAX}(과열)")
    tlres["state"] = TRANSITION_PENDING
    tlres["score"] = 1
    tlres["note"] = f"추세 전환 형태이나 확인 미충족({' · '.join(miss)}) → 전환 대기"
    tlres["reason"] = tlres["state"]
    tlres["volume_confirmed"] = False
    return tlres


def _empty():
    return {"state": "추세선 불명확", "score": 0, "note": "데이터 부족",
            "confirmed_down": False, "down": None, "up": None,
            "level": None, "slope_sign": "-", "dist_pct": None,
            "reason": "추세선 불명확", "terms": ["추세선"]}
