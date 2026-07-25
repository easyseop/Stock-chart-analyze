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
from . import pattern_quality as pq


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
    # 유동성(20일 평균 거래대금) + 장기 이격(MA120 대비) — 추천 품질 필터에 사용
    turnover = float((d["Close"].iloc[-20:] * d["Volume"].iloc[-20:]).mean())
    ma120 = trend.get("ma", {}).get(120)
    stretch_lt = (price / ma120 - 1) if ma120 else 0.0
    # 최근 3개월(63거래일) 상승폭 — '이미 많이 올랐나'(고점 추격) 판정용
    price63 = float(d["Close"].iloc[-63]) if len(d) >= 63 else price
    runup63 = (price / price63 - 1) if price63 else 0.0
    # 52주 범위 내 위치(0=최저점, 1=최고점) — '저점권' 게이트용(사용자: 저점에서 잡기)
    lb = min(len(d), config.NEWHIGH_LOOKBACK)
    lo52 = float(d["Low"].iloc[-lb:].min())
    hi52 = float(d["High"].iloc[-lb:].max())
    range_pos = (price - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5
    # 전일 대비 등락률 — 리스트 화면(토스식 현재가+등락 표시)용
    prev_close = float(d["Close"].iloc[-2]) if len(d) >= 2 else price
    day_chg = (price / prev_close - 1) if prev_close else 0.0
    # 돌파 신선도: 하락추세선 대비 현재가 위치(%). 갓 깬 것(+5% 이내)이 신선한 전환,
    # 한참 위는 '돌파 후 이미 진행'(승률↓, 우선순위 강등). 음수 = 아직 추세선 아래(임박).
    down_now = ((trendline.get("down") or {}).get("now")
                if isinstance(trendline.get("down"), dict) else None)
    break_gap = (price / down_now - 1) if down_now else None
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

    # 현재가가 방어선 아래 = 지지 이탈. 추세선이 '불명확'이라 downtrend veto를
    #   못 잡는 falling knife(예: METC 저점권 0%·현재가<방어선인데 'now'로 오분류)를
    #   여기서 회피 처리. 실측: 진입 픽 20개 전부 현재가≥방어선이라 정상 픽엔 무영향.
    below_defense = bool(sr.get("defense") and price < sr["defense"])
    if sr["position"] == "박스이탈" or (config.DOWNTREND_VETO and downtrend) or below_defense:
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
    # 손절은 변동성(ATR) 기준으로 — 지지/박스에 '바짝' 붙여 잡으면 노이즈에 휩쓸림(휩쏘).
    #   모든 매수 타점(now/pullback/wait/breakout)에 적용, 회피만 제외.
    risk = ind.risk_levels(d, entry, sr["defense"], meta["ccy"],
                           prefer_atr=(entry_kind != "avoid"))
    # 패턴 품질(Phase 0 — 기록 전용): 점수·verdict·게이트에 불개입.
    #   백테스트 분위수 검증에서 단조 개선이 증명된 항목만 이후 정렬에 승격.
    pattern = pq.compute(d, entry=entry, stop=risk["stop"], sr=sr, levels=levels)

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

    # 매물대 반등 신호(슬리브 B) — 전환 미확정이어도 지지 반등 확인 시 매수 후보.
    shelf = _shelf_signal(d, supply, volume, range_pos)

    terms = []
    for blk in (regime, trend, rs, newhigh, market, rsi, sr, volume,
                trendline, supply, risk):
        terms += blk.get("terms", [])
    terms.append("정규화점수")

    return {
        "shelf": shelf,
        "code": meta["code"], "name": meta["name"], "ccy": meta["ccy"],
        "regime": regime, "trend": trend, "rsi": rsi, "sr": sr,
        "rs": rs, "newhigh": newhigh, "market": market,
        "volume": volume, "trendline": trendline, "levels": levels,
        "supply": supply, "risk": risk, "pattern": pattern,
        "module_scores": module_scores, "weights": norm["weights"],
        "norm": norm["score"], "verdict_label": label, "gauge": gauge,
        "verdict": verdict_txt, "entry": entry, "entry_kind": entry_kind,
        "vetoed": vetoed, "terms": terms,
        "ext": {"ma20_stretch": stretch, "runup10": runup10,
                "ma120_stretch": stretch_lt, "runup63": runup63},
        "turnover": turnover, "range_pos": range_pos, "break_gap": break_gap,
        "day_chg": day_chg,
        "trend_oneline": trend_oneline, "chase": chase, "chase_note": chase_note,
        "transition_stage": stage, "transition_label": stage_label,
    }


def _shelf_signal(d, sup: dict, volume: dict, range_pos: float) -> dict:
    """매물대 반등 신호(슬리브 B). 반환 {ok, watch, entry, stop, target, ...}.

    A(전환확정)와 달리 추세 전환을 기다리지 않는다 — 큰 매물대(장기 POC/밸류영역)
    지지 위에서 **되돌아 오른 캔들**을 확인해 매수. '터치 즉시'가 아니라 '반등'을
    봐서 지지 붕괴(떨어지는 칼)를 거른다. 반등 확인만 덜 된 구조는 ``watch=True``로
    공개 화면에만 전달하며 자동매수 그룹(``shelf``)에는 절대 들어가지 않는다.
    손절=밸류 하단(VAL) 아래(논리 무효점).
    """
    if not getattr(config, "SHELF_ENABLED", False):
        return {"ok": False, "watch": False, "reason": "비활성"}
    lng = sup.get("long") or {}
    price = float(sup.get("price") or 0)
    val = float(lng.get("val") or 0)
    poc = float(sup.get("long_poc") or 0)
    vah = float(lng.get("vah") or 0)
    overhead = float((sup.get("pnl") or {}).get("overhead") or 1.0)
    context = {
        "poc": round(poc, 4) if poc > 0 else None,
        "val": round(val, 4) if val > 0 else None,
        "vah": round(vah, 4) if vah > 0 else None,
        "overhead": round(overhead, 3),
    }
    if not (price > 0 and val > 0 and vah > val):
        return {"ok": False, "watch": False,
                "reason": "매물대 정보 없음", **context}
    if range_pos > config.SHELF_LOW_ZONE:
        return {"ok": False, "watch": False,
                "reason": f"저점권 아님(범위 {range_pos*100:.0f}%)", **context}
    if not (val <= price <= vah):
        return {"ok": False, "watch": False,
                "reason": "밸류영역 밖(지지대 아님)", **context}
    if overhead > config.SHELF_OVERHEAD_MAX:
        return {"ok": False, "watch": False,
                "reason": f"머리 위 물량 과다({overhead*100:.0f}%)", **context}
    # 반등 확인: ①최근 3봉 저가가 매물대 본체(POC)까지 눌림 ②종가가 밸류 바닥(VAL)
    #   위 유지(붕괴 아님) ③오늘 캔들 상단 마감(양봉성) ④거래대금 동반 ⑤신저가 아님.
    #   지지 기준을 VAL(최하단)→POC(최대 거래 노드)로: 사용자 원안('거래량 터진 곳')
    #   에 충실 + 매물대 본체 지지를 잡아 신호 빈도 정상화(2026-07-23 진단 반영).
    recent_low = float(d["Low"].iloc[-3:].min())
    hi = float(d["High"].iloc[-1]); lo = float(d["Low"].iloc[-1]); cl = price
    touched = recent_low <= poc * (1 + config.SHELF_NEAR_VAL)
    reclaimed = cl > val
    upper_close = ((cl - lo) / (hi - lo) >= 0.5) if hi > lo else False
    vol_ok = volume.get("mult", 0.0) >= config.SHELF_VOL_MULT
    not_fresh_low = lo > float(d["Low"].iloc[-20:].min())
    # pandas/numpy 비교식은 ``numpy.bool_``를 돌려줄 수 있다. 이 값이 signals.json
    # 경계까지 새면 표준 json 인코더가 실패해 Pages 전체 배포가 멈춘다. 화면용
    # 진단값은 여기서 일반 bool로 고정한다.
    checks = {"터치": bool(touched), "회복": bool(reclaimed),
              "상단마감": bool(upper_close), "거래량": bool(vol_ok),
              "신저가아님": bool(not_fresh_low)}
    missing = [k for k, v in checks.items() if not v]
    stop = recent_low * (1 - config.SHELF_STOP_BUF)   # 반등한 저점 아래(지지 무효점)
    target = vah if vah > price else (
        price + 2 * (price - stop) if price > stop else 0)
    rr = (target - price) / (price - stop) if price > stop else 0.0
    if price <= stop:
        return {"ok": False, "watch": False,
                "reason": "손절선 무효", "checks": checks, **context}
    if (price - stop) / price > config.SHELF_MAX_STOP:
        return {"ok": False, "watch": False,
                "reason": f"손절폭 과대({(price-stop)/price*100:.0f}%)",
                "checks": checks, **context}
    if rr < config.SHELF_MIN_RR:
        return {"ok": False, "watch": False,
                "reason": f"손익비 부족({rr:.1f})",
                "checks": checks, **context}
    if missing:
        return {
            "ok": False, "watch": True,
            "entry": round(price, 4),
            "stop": round(stop, 4),
            "target": round(target, 4),
            "rr": round(rr, 2),
            "checks": checks,
            "reason": "반등 미확인(" + "·".join(missing) + ")",
            **context,
        }
    return {"ok": True, "watch": False,
            "entry": round(price, 4), "stop": round(stop, 4),
            "target": round(target, 4), "rr": round(rr, 2),
            "checks": checks, "reason": "매물대 지지 반등", **context}


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
