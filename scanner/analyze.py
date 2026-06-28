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


def analyze(frames: dict[str, pd.DataFrame], meta: dict, bench=None) -> dict:
    d = frames["D"]

    regime = ind.regime(d)
    trend = ind.trend(frames)
    sr = ind.support_resistance(d, trend["ma"])
    volume = ind.volume_surge(d, sr)
    rsi = ind.momentum_rsi(d)
    rs = ind.relative_strength(d, bench)      # 지수 대비 상대강도(모멘텀)
    newhigh = ind.new_high(d)                 # 52주 신고가 근접도
    market = ind.market_trend(bench)          # 시장(지수) 방향
    trendline = tl.detect(d, frames)
    # 전환 확정 콤보 게이트: 거래량 동반 + RSI 과열 회피(둘 다)일 때만 '확정'(백테스트 검증)
    trendline = tl.apply_confirm_filter(trendline, volume.get("mult", 0.0),
                                        rsi.get("rsi", 50.0))
    levels = lv.analyze_levels(d)          # 차트용 지지/저항 레벨 + 피보/밸류영역
    supply = sp.analyze_supply(d)          # 기간분리 매물대 + 미실현손익 추정

    module_scores = {
        "trend": trend["score"], "rs": rs["score"], "newhigh": newhigh["score"],
        "market": market["score"], "volume": volume["score"], "sr": sr["score"],
        "rsi": rsi["score"], "trendline": trendline["score"],
    }
    norm = scoring.normalize(module_scores, regime["flag"])
    label, gauge = scoring.verdict(norm["score"])

    # 과대이격/급등 판정(진입 타점 결정에 사용) — 이미 많이 오른 종목은 '현재가'가 아니라
    # 1차 반등구간을 진입 타점으로(타이밍과 일치).
    price = float(d["Close"].iloc[-1])
    ma20 = trend.get("ma", {}).get(20)
    stretch = (price / ma20 - 1) if ma20 else 0.0
    low10 = float(d["Low"].iloc[-10:].min())
    runup10 = (price / low10 - 1) if low10 else 0.0
    nh_pct = newhigh.get("pct_from_high")
    # 추격(이미 많이 올라 타점이 멂)도 과대이격에 포함 → 진입타점 판정의 단일 기준.
    near_high = newhigh["score"] == 2
    chase = (near_high and (rsi.get("rsi", 50) >= 70 or stretch >= 0.08)
             or stretch >= 0.13 or runup10 >= 0.20)
    over_ext = (chase or stretch >= 0.08 or runup10 >= 0.13
                or (nh_pct is not None and nh_pct >= -8))
    bz = levels.get("bounce_zones") or []
    sup_below = _support_below(price, sr, levels, trend)   # 현재가 바로 아래 가까운 지지
    downtrend = bool(trendline.get("confirmed_down")
                     or trendline.get("state") == "하락추세 지속")

    if sr["position"] == "박스이탈" or (config.DOWNTREND_VETO and downtrend):
        entry = price                                # 방어선 이탈/하락추세 → 매수 자리 아님
        entry_kind = "avoid"
    elif sr["position"] == "고점권":
        entry = sr["box_high"]                       # 저항 돌파 시 매수
        entry_kind = "breakout"
    elif over_ext and bz:
        entry = bz[0]["center"]                      # 이미 급등 → 1차 반등구간에서 매수
        entry_kind = "pullback"
    elif over_ext and sup_below:
        entry = sup_below                            # 반등구간 없으면 가까운 지지를 눌림 목표로
        entry_kind = "pullback"
    elif over_ext:
        entry = price                                # 받칠 지지조차 없음 → 진입 보류(타점 미정)
        entry_kind = "wait"
    else:
        entry = price                                # 지지 근처 → 현재가 분할
        entry_kind = "now"
    # 눌림 매수(진입가가 지지 위)면 손절은 그 지지 아래 ATR 여유로(방어선 hug 방지)
    risk = ind.risk_levels(d, entry, sr["defense"], meta["ccy"],
                           prefer_atr=(entry_kind in ("pullback", "wait")))

    # ── 하락추세 veto: 하락추세 지속이면 매수 신호를 막는다(사용자 원칙) ──
    vetoed = False
    if config.DOWNTREND_VETO and trendline["confirmed_down"]:
        vetoed = True
        if not label.startswith("적극 매도") and not label.startswith("매도"):
            label, gauge = "회피(하락추세)", "🔴 하락추세"

    verdict_txt = _verdict_text(label, sr, entry, trendline, vetoed)
    trend_oneline = _one_line_trend(trend, regime, trendline)
    stage, stage_label = _transition_stage(trendline)

    # 추격 경고 문구(near_high·chase는 위에서 이미 계산 — 진입타점 단일 기준으로 사용)
    chase_note = (f"🔺 이미 많이 올라 타점이 멂(MA20 +{stretch*100:.0f}% · "
                  f"최근저점 대비 +{runup10*100:.0f}%) — 눌림 대기 권장" if chase else "")

    terms = []
    for blk in (regime, trend, rs, newhigh, market, rsi, sr, volume,
                trendline, supply, risk):
        terms += blk.get("terms", [])
    terms.append("정규화점수")

    return {
        "code": meta["code"], "name": meta["name"], "ccy": meta["ccy"],
        "regime": regime, "trend": trend, "rsi": rsi, "sr": sr,
        "rs": rs, "newhigh": newhigh, "market": market,
        "volume": volume, "trendline": trendline, "levels": levels,
        "supply": supply, "risk": risk,
        "module_scores": module_scores, "weights": norm["weights"],
        "norm": norm["score"], "verdict_label": label, "gauge": gauge,
        "verdict": verdict_txt, "entry": entry, "entry_kind": entry_kind,
        "vetoed": vetoed, "terms": terms,
        "ext": {"ma20_stretch": stretch, "runup10": runup10},
        "trend_oneline": trend_oneline, "chase": chase, "chase_note": chase_note,
        "transition_stage": stage, "transition_label": stage_label,
    }


def _support_below(price, sr, levels, trend):
    """현재가 바로 아래의 가장 가까운 강한 지지(상승추세선/방어선/강한 지지선)."""
    cands = []
    up = (trend or {}).get("trendline_up") or {}
    # 방어선(박스 하단 등)
    if sr.get("defense"):
        cands.append(sr["defense"])
    # 강한 지지 레벨
    for lv in (levels or {}).get("strong", []):
        p = lv.get("price") if isinstance(lv, dict) else lv
        if p:
            cands.append(p)
    cands = [c for c in cands if c and price and c < price]
    return max(cands) if cands else None


def _transition_stage(tl_res) -> tuple[int, str]:
    """하락→상승 '전환' 진행 단계(클수록 전환 확정에 가까움). 우선 정렬용.

    4 전환 확정 / 3 돌파후 횡보(대기) / 2 갓 돌파(미확인) / 1 임박 / 0 해당없음.
    """
    st = tl_res["state"]
    table = {
        tl.TRANSITION_CONFIRMED: (4, "④ 전환 확정 ⭐"),
        tl.TRANSITION_PENDING:   (3, "③ 돌파후 횡보(대기)"),
        tl.BREAKOUT_UNCONFIRMED: (2, "② 갓 돌파(미확인)"),
        "하락추세선 임박":        (1, "① 임박(저항 근접)"),
    }
    return table.get(st, (0, ""))


def _one_line_trend(trend, regime, tl_res) -> str:
    """추세선·국면·이평배열을 종합한 '한눈 추세' 한 줄."""
    arr = trend.get("arrangement", "")
    st = tl_res["state"]
    if st in tl.TRANSITION_STATES or st in (tl.BREAKOUT_UNCONFIRMED, "하락추세선 임박"):
        return "🔄 전환 시도"
    if tl_res.get("confirmed_down") or st == "하락추세 지속" or arr == "역배열":
        return "📉 하락추세"
    if st == "상승추세 유지" or arr == "정배열":
        return "📈 상승추세"
    if regime["flag"] == "횡보장":
        return "↔️ 횡보"
    return "↔️ 횡보/혼조"


def _verdict_text(label, sr, entry, trendline, vetoed) -> str:
    # 추세 전환 후보는 최우선으로 알림 (돌파+안착+상승추세선+거래량 동반까지 확인)
    st = trendline["state"]
    if st == tl.TRANSITION_CONFIRMED:
        return "추세 전환 확정(돌파+안착+상승추세선+거래량) → 전환 매수 후보 (분할 진입)"
    if st == tl.TRANSITION_PENDING:
        return "돌파 후 횡보 안착 — 상승추세선/거래량 확인 시 전환 매수"
    if st == tl.BREAKOUT_UNCONFIRMED:
        return "하락추세선 갓 돌파 — 되밀림 위험, 안착 확인 전 관망"
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
