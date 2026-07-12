"""패턴 품질 지표 (Phase 0 — **기록 전용**, 매매 행동 불변).

GPT 제안서(docs/PATTERN_QUALITY_PLAN.md) 채택분 — "패턴 이름"이 아니라 사람이
차트에서 확인하는 가격 구조를 스칼라로 수치화한다. 점수·verdict·게이트에는
일절 개입하지 않는다(백테스트 분위수 검증에서 단조 개선이 증명될 때까지).

지표 5개:
  clear_space_R   진입 위 최근접 저항까지 거리를 R(손절폭)로 — "2R 갈 공간이 있나"
                  저항 후보: 강한 수평 스윙 군집(levels.strong) · 박스 상단 · POC.
                  위에 저항이 없으면 None + no_overhead=True(유리).
  close_location  당일 (종가-저가)/(고가-저가) — 돌파봉이 강하게 마감했나
  atr_contract    ATR14/ATR60 — 베이스가 조였나(<0.8 양호)
  wick ratios     윗/아랫꼬리·몸통 비율 — 흡수/거절 구분(기록용)
  extension_atr   (종가-MA20)/ATR14 — RSI보다 직접적인 추격 측정(기록용)

quality(0~3): clear_space(≥1.5 또는 no_overhead) + atr_contract(<0.8) +
close_location(≥0.6). — 표시/정렬 후보일 뿐 현 단계선 어디에도 안 쓰임.
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind

CS_GOOD = 1.5          # clear_space_R 양호 기준(제안서)
CONTRACT_GOOD = 0.8    # ATR14/ATR60 수축 양호
CLOSE_LOC_GOOD = 0.6   # 종가 위치 양호


def _nearest_resistance(entry: float, sr: dict | None,
                        levels: dict | None) -> tuple[float | None, str]:
    """진입가 위 최근접 저항 (가격, 종류). 없으면 (None, '')."""
    cands: list[tuple[float, str]] = []
    floor = entry * 1.001                      # entry 자체(=현 저항 돌파가)는 제외
    for l in (levels or {}).get("strong", []) or []:
        p = l.get("price") if isinstance(l, dict) else l
        if p and p > floor:
            cands.append((float(p), "swing_cluster"))
    if sr:
        bh = sr.get("box_high")
        if bh and bh > floor:
            cands.append((float(bh), "box_top"))
        pc = sr.get("poc")
        if pc and pc > floor:
            cands.append((float(pc), "poc"))
    if not cands:
        return None, ""
    return min(cands, key=lambda x: x[0])


def compute(df: pd.DataFrame, *, entry: float, stop: float,
            sr: dict | None = None, levels: dict | None = None) -> dict:
    """패턴 품질 스칼라 일괄 계산. 입력 부족 항목은 None(추측 금지)."""
    out: dict = {
        "clear_space_R": None, "nearest_resistance": None,
        "resistance_type": "", "no_overhead": False,
        "close_location": None, "upper_wick_ratio": None,
        "lower_wick_ratio": None, "body_ratio": None,
        "atr14": None, "atr60": None, "atr_contract": None,
        "extension_atr": None, "quality": 0,
    }
    n = len(df)
    if n < 2:
        return out

    # ── 캔들 품질(마지막 봉) ──────────────────────────────────
    o = float(df["Open"].iloc[-1])
    h = float(df["High"].iloc[-1])
    l = float(df["Low"].iloc[-1])
    c = float(df["Close"].iloc[-1])
    rng = h - l
    if rng > 0:
        out["close_location"] = (c - l) / rng
        out["upper_wick_ratio"] = (h - max(o, c)) / rng
        out["lower_wick_ratio"] = (min(o, c) - l) / rng
        out["body_ratio"] = abs(c - o) / rng
    else:                                       # 일자봉 — 중립 처리
        out["close_location"] = 0.5
        out["upper_wick_ratio"] = out["lower_wick_ratio"] = 0.0
        out["body_ratio"] = 0.0

    # ── 변동성 수축 + 확장(추격) ─────────────────────────────
    if n >= 15:
        a14 = float(ind.atr(df, 14).iloc[-1])
        out["atr14"] = a14
        if n >= 21 and a14 > 0:
            ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
            out["extension_atr"] = (c - ma20) / a14
        if n >= 61:
            a60 = float(ind.atr(df, 60).iloc[-1])
            out["atr60"] = a60
            if a60 > 0:
                out["atr_contract"] = a14 / a60

    # ── clear_space_R ────────────────────────────────────────
    risk = float(entry) - float(stop)
    if risk > 0:
        res_px, res_type = _nearest_resistance(float(entry), sr, levels)
        if res_px is None:
            out["no_overhead"] = True           # 위에 저항 없음 = 열린 공간(유리)
        else:
            out["nearest_resistance"] = res_px
            out["resistance_type"] = res_type
            out["clear_space_R"] = (res_px - float(entry)) / risk

    # ── 종합(기록·표시용 — 행동에 안 씀) ─────────────────────
    q = 0
    if out["no_overhead"] or (out["clear_space_R"] is not None
                              and out["clear_space_R"] >= CS_GOOD):
        q += 1
    if out["atr_contract"] is not None and out["atr_contract"] < CONTRACT_GOOD:
        q += 1
    if out["close_location"] is not None and out["close_location"] >= CLOSE_LOC_GOOD:
        q += 1
    out["quality"] = q
    return out


def badge(p: dict | None) -> str:
    """카드/대시보드 한 줄 배지 — 기록 전용임을 명시."""
    if not p:
        return ""
    parts = []
    if p.get("no_overhead"):
        parts.append("상방저항 없음")
    elif p.get("clear_space_R") is not None:
        parts.append(f"저항까지 {p['clear_space_R']:.1f}R")
    if p.get("atr_contract") is not None:
        parts.append(f"수축 {p['atr_contract']:.2f}")
    if p.get("close_location") is not None:
        parts.append(f"종가위치 {p['close_location']:.2f}")
    if not parts:
        return ""
    return f"📐 품질 {p.get('quality', 0)}/3 · " + " · ".join(parts) + " (기록용)"
