"""한 종목 분석 파이프라인.

순서(데이터 의존성): 국면(ADX) → 추세(MA·다중TF) → 지지/저항·박스 →
거래대금(위치+캔들) → RSI → 추세선 → 점수 종합 → 진입/손절/목표(ATR).
"""
from __future__ import annotations

import pandas as pd

import config
from . import indicators as ind
from . import scoring
from . import levels as lv
from . import trendlines as tl
from . import supply as sp


def analyze(frames: dict[str, pd.DataFrame], meta: dict) -> dict:
    d = frames["D"]

    regime = ind.regime(d)
    trend = ind.trend(frames)
    sr = ind.support_resistance(d, trend["ma"])
    volume = ind.volume_surge(d, sr)
    rsi = ind.momentum_rsi(d)
    trendline = tl.detect(d, frames)
    levels = lv.analyze_levels(d)          # 차트용 지지/저항 레벨 + 피보/밸류영역
    supply = sp.analyze_supply(d)          # 기간분리 매물대 + 미실현손익 추정

    module_scores = {
        "trend": trend["score"], "rsi": rsi["score"], "sr": sr["score"],
        "volume": volume["score"], "trendline": trendline["score"],
    }
    norm = scoring.normalize(module_scores, regime["flag"])
    label, gauge = scoring.verdict(norm["score"])

    # 진입 기준가: 저항 임박(고점권)이면 '돌파 시 매수' → 박스 상단, 그 외 현재가
    price = float(d["Close"].iloc[-1])
    entry = sr["box_high"] if sr["position"] == "고점권" else price
    risk = ind.risk_levels(d, entry, sr["defense"], meta["ccy"])

    # ── 하락추세 veto: 하락추세 지속이면 매수 신호를 막는다(사용자 원칙) ──
    vetoed = False
    if config.DOWNTREND_VETO and trendline["confirmed_down"]:
        vetoed = True
        if not label.startswith("적극 매도") and not label.startswith("매도"):
            label, gauge = "회피(하락추세)", "🔴 하락추세"

    verdict_txt = _verdict_text(label, sr, entry, trendline, vetoed)

    terms = []
    for blk in (regime, trend, rsi, sr, volume, trendline, supply, risk):
        terms += blk.get("terms", [])
    terms.append("정규화점수")

    return {
        "code": meta["code"], "name": meta["name"], "ccy": meta["ccy"],
        "regime": regime, "trend": trend, "rsi": rsi, "sr": sr,
        "volume": volume, "trendline": trendline, "levels": levels,
        "supply": supply, "risk": risk,
        "module_scores": module_scores, "weights": norm["weights"],
        "norm": norm["score"], "verdict_label": label, "gauge": gauge,
        "verdict": verdict_txt, "entry": entry, "vetoed": vetoed, "terms": terms,
    }


def _verdict_text(label, sr, entry, trendline, vetoed) -> str:
    # 추세 전환 후보는 최우선으로 알림
    if trendline["state"] == "하락추세선 상향돌파":
        return "하락추세선 상향 돌파 → 추세 전환 후보 (확인 후 분할 진입)"
    if vetoed:
        return "하락추세선 아래 — 추세 전환 전까지 관망/회피"
    if trendline["state"] == "하락추세선 임박":
        return "하락추세선 임박 — 돌파 확인 시 전환 매수 후보"
    if sr["position"] == "고점권":
        return f"저항 {entry:,.2f} 돌파 시 매수 / 미돌파 시 관망"
    if sr["position"] == "박스이탈":
        return "방어선 이탈 — 보유 시 손절, 신규 회피"
    if label.startswith("적극 매수"):
        return "적극 매수 구간 (분할 진입)"
    if label.startswith("매수"):
        return "매수 관심 (지지 확인 후 진입)"
    if label.startswith("적극 매도"):
        return "적극 회피 / 청산"
    if label.startswith("매도"):
        return "매도 관심 / 비중 축소"
    return "관망 (신호 부족)"
