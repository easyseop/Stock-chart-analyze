"""매매 전략 생성기 — 분석 결과(result)를 '진입·손절·목표 + If-Then 규칙'으로.

상세 페이지(lwc) 맨 위에 붙는 '📋 매매 전략' 카드를 만든다. raw 신호 카드와 달리
"그래서 어디서 사고/자르고/판다"를 한국어 규칙으로 잡아준다.
진입 추천 점수(rec_n)도 여기서 공유한다(screener·lwc 공용, 순환 import 방지).
"""
from __future__ import annotations

import html

import config

REC_MIN = 6   # 6개 조건 '전부' 충족해야 진입 추천(엄선 — 추격/과열은 별도 제외)


def _overextended(r: dict) -> bool:
    """이미 너무 올라 '타점이 멀다'(추격)인가 — 추천에서 제외하는 기준.

    ① 추격주의 플래그, ② 20일선 대비 과대이격(+15%↑), ③ 손절폭이 너무 큼(진입의 12%↑
    = 손절을 멀리 둬야 함 = R:R 나쁨/이미 솟구침).
    """
    if r.get("chase"):
        return True
    nh = (r.get("newhigh") or {}).get("pct_from_high")
    if nh is not None and nh >= -8:          # 52주 신고가 −8% 이내 = 이미 다 옴(전환 초입 아님)
        return True
    ext = r.get("ext") or {}
    if ext.get("ma20_stretch", 0) >= 0.08:   # 20일선 +8%↑ 과대이격
        return True
    if ext.get("runup10", 0) >= 0.13:        # 최근 10봉 저점 대비 +13%↑ 급등(타점 멂)
        return True
    risk = r.get("risk") or {}
    price = (r.get("sr") or {}).get("price")
    entry = r.get("entry") or price
    stop = risk.get("stop")
    if entry and stop and entry > 0 and (entry - stop) / entry >= 0.12:
        return True
    return False


def _support_below(r: dict):
    """현재가 바로 아래의 가장 가까운 강한 지지(상승추세선/방어선/지지선) 가격."""
    sr = r.get("sr") or {}
    price = sr.get("price")
    cands = []
    up = (r.get("trendline") or {}).get("up")
    if up and up.get("slope", 0) > 0 and up.get("now"):
        cands.append(up["now"])               # 상승추세선(동적 지지)
    if sr.get("defense"):
        cands.append(sr["defense"])           # 방어선
    for lv in (r.get("levels") or {}).get("strong", []):
        cands.append(lv["price"])             # 강한 지지선
    cands = [c for c in cands if c and price and c < price]
    return max(cands) if cands else None


def timing(r: dict) -> str:
    """'지금이 타점인가/주시인가'를 한 줄로. 사용자 핵심: 지금 살 자리 vs 기다릴 자리."""
    if r.get("vetoed"):
        return ""
    if r.get("entry_kind") == "avoid":
        return "🚫 신규 매수 회피 — 방어선 이탈/하락추세 (반등은 추격 금지)"
    sr = r.get("sr") or {}
    price = sr.get("price")
    pos = sr.get("position")
    stage = r.get("transition_stage", 0)
    if stage <= 0 and r.get("norm", 0) < config.VERDICT_WEAK:
        return ""                              # 전환·강세 후보 아니면 타이밍 의미 없음
    if pos == "고점권" and sr.get("box_high") and price:
        g = (sr["box_high"] - price) / price * 100
        return f"⏳ 저항 돌파 대기 (저항까지 +{g:.1f}%)"
    if r.get("entry_kind") in ("pullback", "wait"):
        bz = (r.get("levels") or {}).get("bounce_zones") or []
        tgt, lab = (None, "")
        if bz and price:
            tgt, lab = bz[0]["center"], "1차 반등"
        else:
            sup = _support_below(r)
            if sup:
                tgt, lab = sup, "지지"
        if tgt and price:
            g = (price - tgt) / price * 100
            return f"👀 눌림 대기 — {lab} {tgt:,.2f}(−{g:.1f}%)까지 빠지면 진입"
        return "👀 진입 보류 — 받칠 지지 확인 후(추격 금지)"
    nh = (r.get("newhigh") or {}).get("pct_from_high")
    room = nh is not None and nh <= -12       # 신고가까지 12%+ 남음 = 올라갈 방 충분
    sup = _support_below(r)
    if sup and price:
        gap = (price - sup) / sup * 100
        if gap <= 4 and room:
            return f"🎯 지금 타점권 (지지 {sup:,.2f} 바로 위 +{gap:.1f}% · 신고가까지 {nh:.0f}%)"
        if gap <= 10:
            return f"👀 타점 근처 (지지까지 −{gap:.1f}%, 눌리면 진입) — 주시"
        return f"👀 타점 위 +{gap:.1f}% — 눌림 대기·주시"
    return ""


def _checklist(r: dict) -> int:
    stage = r.get("transition_stage", 0)
    rs = (r.get("rs") or {}).get("rel")
    mk = (r.get("market") or {}).get("direction", "")
    nh = (r.get("newhigh") or {}).get("pct_from_high")
    n = 0
    if stage >= 3:
        n += 1
    if "상승" in mk:
        n += 1
    if rs is not None and rs > 0:
        n += 1
    if nh is not None and -50 <= nh <= -15:  # 많이 빠졌다 도는 중 — 신고가까지 방 충분(초입)
        n += 1
    if not r.get("chase"):
        n += 1
    if r.get("norm", 0) >= config.VERDICT_WEAK:
        n += 1
    return n


def rec_n(r: dict) -> int:
    """진입 추천 점수(0~6). 하락추세 veto=0. 이미 솟구쳐 타점이 먼(추격) 종목은
    추천선(4) 아래로 강등 — '이미 상승해서 타점 애매한 건 추천 안 함'."""
    if r.get("vetoed"):
        return 0
    n = _checklist(r)
    if _overextended(r):
        return min(n, REC_MIN - 1)   # 과열/타점부적합 → 추천에서 제외(최대 3)
    return n


def _group(r: dict) -> tuple[str, str]:
    """카드용 '상태 그룹'(칩 축과 동일): 전환후보/상승추세/관망/회피 — 전환단계와 다른 축."""
    if r.get("vetoed") or r.get("entry_kind") == "avoid" or r.get("sr", {}).get("position") == "박스이탈":
        return "회피", "🔴"
    tone = r.get("trend_oneline", "")
    if r.get("transition_stage", 0) >= 1 or tone == "🔄 전환 시도":
        return "전환후보", "🟢"
    if tone == "📈 상승추세":
        return "상승추세", "📈"
    if tone == "📉 하락추세":
        return "회피", "🔴"
    return "관망", "⚪"


def _context_note(r: dict, group: str, mk: str):
    """'전환후보인데 시장 하락' 같은 헷갈리는 조합을 한 줄로 해석. (텍스트, 종류) 반환."""
    down = "하락" in mk
    up = "상승" in mk
    if group == "전환후보":
        if down:
            return ("⚠️ <b>전환후보 + 시장 하락</b> — 종목은 바닥에서 도는 중이지만 시장이 "
                    "안 받쳐주면 <b>전환 실패(되돌림) 위험</b>이 큼. <b>진입추천은 시장이 상승일 "
                    "때만</b> 나오므로 지금은 <b>관찰만</b>, 시장이 돌면 1순위 후보.", "warn")
        if up:
            return ("✅ <b>전환후보 + 시장 상승</b> — 개별 전환을 시장이 받쳐주는 좋은 조합. "
                    "단계 ③·④와 거래량 확인되면 진입 검토.", "good")
    if group == "상승추세" and down:
        return ("⚠️ <b>상승추세지만 시장 하락</b> — 종목은 강하나 시장 역풍. 비중·손절 보수적으로.",
                "warn")
    return None


def _fmt(v, ccy: str) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}원" if ccy == "KRW" else f"${v:,.2f}"


def thesis(r: dict) -> dict:
    """'클로드라면 이렇게 진입한다' — 진입 기준(가격·시점)과 그게 타당한지 판정.

    반환: {now: 지금진입타당?, verdict: 한줄판정, thesis: 진입논리, rr, stop_pct}.
    """
    ccy = r["ccy"]
    f = lambda v: _fmt(v, ccy)
    sr = r.get("sr") or {}
    risk = r.get("risk") or {}
    price = sr.get("price")
    entry = r.get("entry")
    kind = r.get("entry_kind", "now")
    stop = risk.get("stop")
    target = risk.get("target")
    rr = risk.get("rr", 2)
    stp = (entry - stop) / entry * 100 if (entry and stop) else 0

    if r.get("vetoed") or kind == "avoid":
        return {"now": False, "verdict": "🚫 회피",
                "thesis": "하락추세/방어선 이탈 — 신규 진입 부적합. 반등 나와도 추격 금지."}
    if kind == "breakout":
        return {"now": False, "verdict": "⏳ 돌파 대기",
                "thesis": (f"저항 {f(sr.get('box_high'))} 돌파+종가 안착 확인되면 진입. "
                           f"지금은 미돌파라 부적합 → 대기. 진입 시 손절 {f(stop)}·"
                           f"목표 {f(target)}(손익비 1:{rr:.0f}).")}
    if kind == "pullback":
        return {"now": False, "verdict": "⏳ 눌림 대기",
                "thesis": (f"이미 올라 지금 추격은 부적합. <b>{f(entry)}까지 눌리면</b> 진입 타당 "
                           f"— 손절 {f(stop)}(−{stp:.0f}%)·목표 {f(target)}(손익비 1:{rr:.0f}).")}
    if kind == "wait":
        return {"now": False, "verdict": "👀 관찰",
                "thesis": "이미 올랐는데 받칠 지지가 불명확 — 지지 형성될 때까지 관찰."}
    # kind == "now"
    rec = rec_n(r)
    tm = timing(r)
    strong = rec >= REC_MIN or (tm and "🎯" in tm)
    if strong:
        size = "적정" if stp <= 12 else "다소 큼(비중 축소)"
        return {"now": True, "verdict": "✅ 지금 진입 타당",
                "thesis": (f"현재가 {f(price)}가 지지 바로 위 타점권 → <b>지금 분할 진입 타당</b>. "
                           f"손절 {f(stop)}(−{stp:.0f}%)·목표 {f(target)}(손익비 1:{rr:.0f}). "
                           f"손절폭 {stp:.0f}% — {size}.")}
    return {"now": True, "verdict": "△ 조건부(신호 약함)",
            "thesis": (f"지지 근처라 현재가 {f(price)} 분할은 가능하나 신호가 약함 — "
                       f"소액·분할로만. 손절 {f(stop)}·목표 {f(target)}.")}


def _headline(r: dict, rec: int) -> tuple[str, str]:
    """(색, 한 줄 결론)."""
    stage = r.get("transition_stage", 0)
    pos = r["sr"]["position"]
    if r.get("vetoed"):
        return "#dc2626", "🔴 하락추세 — 신규 매수 회피. 보유 중이면 손절 점검."
    if r.get("entry_kind") == "avoid" or pos == "박스이탈":
        return "#dc2626", "🔴 방어선 이탈(하락) — 신규 매수 회피. 반등 나와도 추격 금지."
    if rec >= REC_MIN:
        return "#16a34a", f"⭐ 진입 추천 (체크리스트 {rec}/6) — 조건 양호, 분할 진입 검토."
    if r.get("entry_kind") in ("pullback", "wait"):
        ext = r.get("ext") or {}
        strong = (r.get("chase") or ext.get("ma20_stretch", 0) >= 0.10
                  or ext.get("runup10", 0) >= 0.18)
        mild = (ext.get("ma20_stretch", 0) >= 0.08 or ext.get("runup10", 0) >= 0.13)
        if strong:
            return "#d97706", "🔺 이미 급등 — 지금 진입은 추격. 눌림(반등구간) 대기."
        if mild:
            return "#d97706", "🔺 단기 반등 후 — 지금 추격은 주의. 눌림 대기."
        return "#d97706", "🔺 고점권(52주 고가 근처) — 신규 추격 주의, 눌림 대기."
    if stage >= 3:
        return "#16a34a", "🟢 전환 후보 — 돌파·거래량 확인되면 진입."
    if pos == "고점권":
        return "#0284c7", "저항 돌파 확인 후 매수 / 미돌파 시 관망."
    if stage == 2:
        return "#0284c7", "갓 돌파(미확인) — 되밀림 위험, 안착 확인 후."
    return "#64748b", f"관망 — 적극 진입 근거 부족 (체크리스트 {rec}/6)."


def plan_html(r: dict) -> str:
    """상세 페이지용 '매매 전략' 카드 HTML."""
    ccy = r["ccy"]
    sr, risk = r["sr"], r["risk"]
    pos = sr["position"]
    price = sr["price"]
    entry = r["entry"]
    rec = rec_n(r)
    color, head = _headline(r, rec)
    f = lambda v: _fmt(v, ccy)

    # 진입 설명 — 타이밍과 일치(지금 vs 눌림 대기 vs 돌파 대기). 한 줄로 핵심만.
    kind = r.get("entry_kind", "now")
    has_bz = bool((r.get("levels") or {}).get("bounce_zones"))
    if kind == "avoid":
        entry_desc = "<b>신규 매수 회피</b> — 방어선 아래(하락추세). 표시값은 참고용 현재가"
        entry_src = ("방어선 이탈/하락추세 = 매수 자리 아님. '여기서 사라'가 아니라 "
                     "방어선 회복 전까지 관망. 보유 중이면 반등에 비중 축소.")
    elif kind == "breakout":
        entry_desc = f"저항 {f(sr['box_high'])} 돌파+안착 시 <b>지금 아님</b>"
        entry_src = "고점권이라 저항 돌파 자리에서 매수(돌파 확인 후)."
    elif kind == "pullback":
        tgt = "1차 반등구간" if has_bz else "아래 지지"
        entry_desc = f"{tgt} {f(entry)}까지 눌릴 때 <b>지금 아님</b>"
        entry_src = ("이미 올라 타점이 멂 → "
                     + ("1차 반등(지지 겹침) 구간에서." if has_bz else "현재가 아래 가까운 지지에서."))
    elif kind == "wait":
        # 이미 올랐는데 받칠 지지가 안 잡힘 → 진입가 미정. '현재가=진입' 오해 방지.
        entry_desc = "받칠 지지 불명확 — <b>진입 보류</b>(관망)"
        entry_src = ("표시된 값은 현재가일 뿐 '지금 사라'가 아님. 눌림 받칠 지지가 "
                     "잡힐 때까지 신규 진입 보류.")
    else:
        entry_desc = f"= 현재가 — <b>지금 이 가격에 분할 매수</b> (지지 바로 위 타점권)"
        entry_src = "진입가가 현재가와 같은 건 '지금 여기서 사라'는 뜻(타점권이라 일부러 동일)."

    # 손절 설명 — 핵심만(실제 손절가 기준). 방어선 vs 손절 구분은 '자세히'로.
    ds = sr.get("defense_strength", "")
    stop_pct = (entry - risk["stop"]) / entry * 100 if entry else 0
    if kind == "avoid":
        stop_desc = "참고용 — 하락추세/방어선 이탈이라 신규 매매 대상 아님"
    elif stop_pct >= 18:
        stop_desc = (f"종가 이탈 시 전량 (진입 −{stop_pct:.0f}%) "
                     f"<b>※손절폭 과대 — 변동성 큼, 매매 신중</b>")
    else:
        stop_desc = f"종가 이탈 시 전량 (진입 −{stop_pct:.0f}%)"
    stop_more = (
        f"<b>손절</b> {f(risk['stop'])} = 실제 파는 가격(방어선 약간 아래 또는 "
        f"ATR손절 {f(risk['atr_stop'])} 중 가까운 쪽).<br>"
        f"<b>방어선</b> {f(sr['defense'])}({ds}) = 추세가 살아있는 마지노선(지지 구조). "
        f"종가로 깨지면 추세 훼손 → 그때 손절 실행. <b>방어선=벽, 손절=빠져나오는 문.</b>")

    # 비중
    shares = risk.get("shares", 0)
    acct = risk.get("account", 0)
    pos_desc = (f"<b>{shares}주</b> "
                f"<span class='dim'>(계좌 {f(acct)}의 1% 손실 한도 기준)</span>")
    if risk.get("underfunded"):
        pos_desc += " <span style='color:#dc2626'>※손절폭이 1% 예산보다 큼 — 비중 축소</span>"

    # If-Then 규칙
    rules = []
    if kind == "avoid":
        rules.append("<b>회피</b>: 방어선 이탈/하락추세 — 신규 매수 금지. "
                     "보유 시 반등마다 비중 축소(추격 금지)")
    elif pos == "고점권":
        rules.append(f"<b>매수</b>: 저항 {f(sr['box_high'])}를 평소 1.5배↑ 거래량으로 "
                     f"돌파+종가 안착 → 진입")
    elif kind == "pullback":
        tgt = "1차 반등구간" if has_bz else "아래 지지"
        rules.append(f"<b>매수</b>: 이미 올라 추격 금지 — <b>{tgt} {f(entry)}까지 눌리면</b> 분할 진입")
    elif kind == "wait":
        rules.append("<b>매수</b>: 진입 보류 — 받칠 지지가 잡히고 거기서 지지 확인될 때까지 관망")
    elif r.get("transition_stage", 0) >= 2:
        rules.append("<b>매수</b>: 돌파선 위에서 되눌림 후 안착(상승추세선 형성) 확인 → 분할 진입")
    else:
        rules.append(f"<b>매수</b>: 현재가 부근 지지 확인 후 분할 (적극 진입은 신호 강화 시)")
    rules.append(f"<b>손절</b>: {f(risk['stop'])} 종가 이탈 → 전량 정리(무조건)")
    rules.append(f"<b>목표</b>: 1차 {f(risk['target'])}(손익비 1:{risk['rr']:.0f}) 도달 시 "
                 f"일부 익절 + 손절을 진입가로 올리기")
    if r.get("vetoed"):
        rules = [f"<b>회피</b>: 하락추세 — 신규 매수 금지. "
                 f"보유 시 방어선 {f(sr['defense'])} 이탈에서 정리"]
    rules_html = "".join(f"<li>{x}</li>" for x in rules)

    rs = (r.get("rs") or {}).get("rel")
    rs_txt = f"{rs*100:+.0f}%" if rs is not None else "-"
    rs_cls = "pos" if (rs is not None and rs > 0) else ("neg" if rs is not None else "")
    mk = (r.get("market") or {}).get("direction", "-")
    mk_cls = "pos" if "상승" in mk else ("neg" if "하락" in mk else "")
    rel = (r.get("trendline") or {}).get("reliability")
    rel_txt = f" · 추세선신뢰 {html.escape(rel)}" if rel else ""
    group, gem = _group(r)
    # 두 축을 분리해 표기: '상태 그룹'(전환후보/상승추세/관망/회피) vs '전환단계'(①~④)
    why = (f"상태 <b>{gem}{group}</b> · 전환단계 "
           f"{html.escape(r.get('transition_label') or '–')} · 신호 {html.escape(r['gauge'])} · "
           f"RS <span class='{rs_cls}'>{rs_txt}</span> · "
           f"시장 <span class='{mk_cls}'>{html.escape(mk)}</span>{rel_txt}")

    note = _context_note(r, group, mk)
    note_html = (f'<div class="plan-note {note[1]}">{note[0]}</div>' if note else "")

    tm = timing(r)
    tm_html = f'<div class="plan-timing">{html.escape(tm)}</div>' if tm else ""
    zones_html = _zones_html(r, f)
    return _TMPL.format(
        color=color, head=html.escape(head), why=why, timing=tm_html,
        note=note_html, zones=zones_html,
        entry=f(entry), entry_desc=entry_desc, entry_src=entry_src,
        stop=f(risk["stop"]), stop_desc=stop_desc, stop_more=stop_more,
        target=f(risk["target"]), rr=f"{risk['rr']:.0f}",
        pos_desc=pos_desc, rules=rules_html)


def _zone_li(tag: str, z: dict, f) -> str:
    rng = (f"{f(z['low'])}~{f(z['high'])}" if z["high"] - z["low"] > 1e-9
           else f(z["center"]))
    return (f'<li><b>{tag}</b> {rng} '
            f'<span class="zd">({z["dist_pct"]:+.1f}%)</span> · '
            f'{html.escape(z["label"])} <span class="zn">×{z["n_types"]}</span></li>')


def _zones_html(r: dict, f) -> str:
    """가격 지도 — 위=저항/목표, 가운데=현재가·손절, 아래=반등 예상(깨지면 다음)."""
    lv = r.get("levels") or {}
    bounce = lv.get("bounce_zones") or []
    res = lv.get("resist_zones") or []
    price = (r.get("sr") or {}).get("price")
    stop = (r.get("risk") or {}).get("stop")
    if not bounce and not res:
        return ""
    res_li = "".join(_zone_li(f"R{i+1}", z, f) for i, z in enumerate(res[:3]))
    bnc_li = "".join(_zone_li(["1차", "2차", "3차"][i], z, f)
                     for i, z in enumerate(bounce[:3]))
    res_html = (f'<div class="pm-up">⬆ 저항/목표<ul>{res_li}</ul></div>'
                if res_li else "")
    bnc_html = (f'<div class="pm-dn">⬇ 반등 예상 <span class="zhs">(깨지면 다음 구간)</span>'
                f'<ul>{bnc_li}</ul></div>' if bnc_li else "")
    now = (f'<div class="pm-now">── 현재가 <b>{f(price)}</b>'
           f'{" · 손절 " + f(stop) if stop else ""} ──</div>')
    return (f'<div class="pmap"><div class="pm-h">📈 가격 지도</div>'
            f'{res_html}{now}{bnc_html}</div>')


_TMPL = """<div class="plan">
  <div class="plan-h" style="border-color:{color}">
    <span class="plan-t">📋 매매 전략</span>
    <span class="plan-head" style="color:{color}">{head}</span>
  </div>
  {timing}
  {note}
  {zones}
  <table class="plan-tb">
    <tr><th>진입</th><td><b class="big">{entry}</b> <span class="d">{entry_desc}</span></td></tr>
    <tr><th>손절</th><td><b class="big">{stop}</b> <span class="d">{stop_desc}</span></td></tr>
    <tr><th>목표</th><td><b class="big">{target}</b> <span class="dim">손익비 1:{rr}</span></td></tr>
  </table>
  <details class="plan-more">
    <summary>자세히 — 근거·비중·규칙</summary>
    <div class="pm-body">
      <div class="plan-why">{why}</div>
      <div class="mrow"><span class="ml">진입 근거</span> {entry_src}</div>
      <div class="mrow"><span class="ml">손절·방어선</span> {stop_more}</div>
      <div class="mrow"><span class="ml">비중</span> {pos_desc}</div>
      <div class="plan-rules"><div class="rh">규칙 (If-Then)</div><ul>{rules}</ul></div>
    </div>
  </details>
  <div class="plan-warn">⚠️ 차트 기준 일반 가이드 · 투자권유 아님. 실적·뉴스·갭은 별도 확인.</div>
</div>"""


PLAN_CSS = """
  .plan{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:0;
    overflow:hidden;margin-bottom:12px}
  .plan-h{display:flex;align-items:center;gap:10px;padding:11px 14px;
    border-left:5px solid #16a34a;background:#f8fafc}
  .plan-t{font-weight:800;font-size:15px;color:#0f172a}
  .plan-head{font-weight:700;font-size:13.5px}
  .plan-timing{margin:10px 14px 0;padding:9px 12px;border-radius:9px;font-size:14px;
    font-weight:700;background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
  .plan-note{margin:10px 14px 0;padding:9px 12px;border-radius:9px;font-size:12.5px;
    line-height:1.65;overflow-wrap:anywhere}
  .plan-note.warn{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
  .plan-note.good{background:#eff6ff;color:#1e40af;border:1px solid #bfdbfe}
  .plan-note b{font-weight:800}
  .plan .pos{color:#16a34a;font-weight:700} .plan .neg{color:#dc2626;font-weight:700}
  .pmap{margin:10px 14px 0;border:1px solid #e2e8f0;border-radius:11px;overflow:hidden}
  .pm-h{padding:8px 12px;font-size:13px;font-weight:800;color:#0f172a;background:#f8fafc;
    border-bottom:1px solid #eef2f7}
  .pm-up{padding:8px 12px;background:#fef2f2;color:#991b1b;font-size:12px;font-weight:700}
  .pm-dn{padding:8px 12px;background:#eff6ff;color:#1e40af;font-size:12px;font-weight:700}
  .pm-now{padding:7px 12px;text-align:center;font-size:13px;color:#334155;
    background:#fff;border-top:1px solid #eef2f7;border-bottom:1px solid #eef2f7}
  .pmap ul{margin:4px 0 0;padding-left:16px} .pmap li{font-size:13px;margin:3px 0;
    color:#1e293b;font-weight:600;overflow-wrap:anywhere}
  .pmap .zhs{font-weight:400;font-size:11px} .pmap .zd{color:#64748b;font-size:11px;font-weight:400}
  .pmap .zn{color:#2563eb;font-weight:700;font-size:11px}
  .plan-why{padding:7px 14px;font-size:12px;color:#64748b;border-bottom:1px solid #f1f5f9;
    overflow-wrap:anywhere}
  .plan-tb{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:4px}
  .plan-tb td,.plan-zones li{overflow-wrap:anywhere;word-break:break-word}
  .plan-tb th{width:50px;text-align:left;vertical-align:baseline;padding:11px 0 11px 14px;
    color:#64748b;font-size:13px;font-weight:700}
  .plan-tb td{padding:11px 14px 11px 6px;border-bottom:1px solid #f5f7fa;font-size:13.5px;
    vertical-align:baseline}
  .plan-tb .big{font-size:18px;color:#0f172a}
  .plan-tb .d{font-size:12.5px;color:#475569}
  .plan .dim{color:#94a3b8;font-weight:400;font-size:11.5px}
  .plan-more{border-top:1px solid #f1f5f9}
  .plan-more>summary{padding:9px 14px;font-size:12.5px;font-weight:700;color:#475569;
    cursor:pointer;list-style:none;background:#fbfcfe}
  .plan-more>summary::-webkit-details-marker{display:none}
  .plan-more>summary::before{content:"▸ ";color:#94a3b8}
  .plan-more[open]>summary::before{content:"▾ "}
  .pm-body{padding:2px 0 6px}
  .pm-body .plan-why{border-bottom:none}
  .mrow{padding:6px 14px;font-size:12px;color:#475569;line-height:1.6;overflow-wrap:anywhere}
  .mrow .ml{display:inline-block;font-weight:700;color:#334155;margin-right:4px}
  .plan-rules{padding:8px 14px}
  .plan-rules .rh{font-weight:700;font-size:13px;color:#334155;margin-bottom:4px}
  .plan-rules ul{margin:0;padding-left:18px} .plan-rules li{margin:4px 0;font-size:13px}
  .plan-warn{padding:8px 14px;background:#fffbeb;color:#92400e;font-size:11.5px}
"""
