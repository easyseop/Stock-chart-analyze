"""한 종목 분석 파이프라인.

순서(데이터 의존성): 국면(ADX) → 추세(MA·다중TF) → 지지/저항·박스 →
거래대금(위치+캔들) → RSI → 점수 종합 → 진입/손절/목표(ATR).
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind
from . import scoring


def analyze(frames: dict[str, pd.DataFrame], meta: dict) -> dict:
    d = frames["D"]

    regime = ind.regime(d)
    trend = ind.trend(frames)
    sr = ind.support_resistance(d, trend["ma"])
    volume = ind.volume_surge(d, sr)
    rsi = ind.momentum_rsi(d)

    module_scores = {
        "trend": trend["score"], "rsi": rsi["score"],
        "sr": sr["score"], "volume": volume["score"],
    }
    norm = scoring.normalize(module_scores, regime["flag"])
    label, gauge = scoring.verdict(norm["score"])

    # 진입 기준가: 저항 임박(고점권)이면 '돌파 시 매수' → 박스 상단, 그 외 현재가
    price = float(d["Close"].iloc[-1])
    entry = sr["box_high"] if sr["position"] == "고점권" else price

    risk = ind.risk_levels(d, entry, sr["defense"], meta["ccy"])

    # 판정 문구
    if sr["position"] == "고점권":
        verdict_txt = f"저항 {entry:,.2f} 돌파 시 매수 / 미돌파 시 관망"
    elif sr["position"] == "박스이탈":
        verdict_txt = "방어선 이탈 — 보유 시 손절, 신규 회피"
    elif label.startswith("적극 매수"):
        verdict_txt = "적극 매수 구간 (분할 진입)"
    elif label.startswith("매수"):
        verdict_txt = "매수 관심 (지지 확인 후 진입)"
    elif label.startswith("매도"):
        verdict_txt = "매도 관심 / 비중 축소"
    elif label.startswith("적극 매도"):
        verdict_txt = "적극 회피 / 청산"
    else:
        verdict_txt = "관망 (신호 부족)"

    terms = []
    for blk in (regime, trend, rsi, sr, volume, risk):
        terms += blk.get("terms", [])
    terms.append("정규화점수")

    return {
        "code": meta["code"], "name": meta["name"], "ccy": meta["ccy"],
        "regime": regime, "trend": trend, "rsi": rsi, "sr": sr,
        "volume": volume, "risk": risk,
        "module_scores": module_scores, "weights": norm["weights"],
        "norm": norm["score"], "verdict_label": label, "gauge": gauge,
        "verdict": verdict_txt, "entry": entry, "terms": terms,
    }
