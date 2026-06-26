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
    sr = r.get("sr") or {}
    price = sr.get("price")
    pos = sr.get("position")
    stage = r.get("transition_stage", 0)
    if stage <= 0 and r.get("norm", 0) < config.VERDICT_WEAK:
        return ""                              # 전환·강세 후보 아니면 타이밍 의미 없음
    if pos == "고점권" and sr.get("box_high") and price:
        g = (sr["box_high"] - price) / price * 100
        return f"⏳ 저항 돌파 대기 (저항까지 +{g:.1f}%)"
    if _overextended(r):
        return "👀 타점 위(이미 급등/고점) — 눌림 대기·주시"
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


def _fmt(v, ccy: str) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}원" if ccy == "KRW" else f"${v:,.2f}"


def _headline(r: dict, rec: int) -> tuple[str, str]:
    """(색, 한 줄 결론)."""
    stage = r.get("transition_stage", 0)
    pos = r["sr"]["position"]
    if r.get("vetoed"):
        return "#dc2626", "🔴 하락추세 — 신규 매수 회피. 보유 중이면 손절 점검."
    if rec >= REC_MIN:
        return "#16a34a", f"⭐ 진입 추천 (체크리스트 {rec}/6) — 조건 양호, 분할 진입 검토."
    if r.get("chase") or _overextended(r):
        ext = r.get("ext") or {}
        return "#d97706", (
            f"🔺 이미 많이 올라 타점이 멂(MA20 +{ext.get('ma20_stretch',0)*100:.0f}% · "
            f"최근저점 +{ext.get('runup10',0)*100:.0f}%) — 전환이어도 지금 진입은 추격. 눌림 대기.")
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

    # 진입 설명 — 진입가가 '어디서' 나오는지 명시
    if pos == "고점권":
        entry_desc = (f"박스 상단(저항) <b>{f(sr['box_high'])}</b> 기준 — "
                      f"여기를 <b>거래량 동반 돌파+종가 안착</b>하면 매수.")
    elif pos in ("저항돌파", "박스이탈"):
        entry_desc = f"돌파 직후 — 되눌림(눌림목) 확인하며 <b>현재가 {f(price)}</b> 분할."
    else:
        entry_desc = f"<b>현재가 {f(price)}</b> 분할 매수(지지 확인하며)."
    entry_src = ("진입가 = 고점권이면 ‘저항(박스 상단)’, 그 외엔 ‘현재가’에서 자동 산출.")

    # 손절 설명
    ds = sr.get("defense_strength", "")
    stop_desc = (f"방어선 <b>{f(sr['defense'])}</b>({ds}) 종가 이탈 시 정리. "
                 f"<span class='dim'>ATR손절 {f(risk['atr_stop'])} · "
                 f"방어선손절 {f(risk['defense_stop'])} 중 가까운 쪽 채택.</span>")

    # 비중
    shares = risk.get("shares", 0)
    acct = risk.get("account", 0)
    pos_desc = (f"<b>{shares}주</b> "
                f"<span class='dim'>(계좌 {f(acct)}의 1% 손실 한도 기준)</span>")
    if risk.get("underfunded"):
        pos_desc += " <span style='color:#dc2626'>※손절폭이 1% 예산보다 큼 — 비중 축소</span>"

    # If-Then 규칙
    rules = []
    if pos == "고점권":
        rules.append(f"<b>매수</b>: 저항 {f(sr['box_high'])}를 평소 1.5배↑ 거래량으로 "
                     f"돌파+종가 안착 → 진입")
    elif r.get("transition_stage", 0) >= 2:
        rules.append("<b>매수</b>: 돌파선 위에서 되눌림 후 안착(상승추세선 형성) 확인 → 분할 진입")
    else:
        rules.append(f"<b>매수</b>: 현재가 부근 지지 확인 후 분할 (적극 진입은 신호 강화 시)")
    rules.append(f"<b>손절</b>: 방어선 {f(sr['defense'])} 종가 이탈 → 전량 정리(무조건)")
    rules.append(f"<b>목표</b>: 1차 {f(risk['target'])}(손익비 1:{risk['rr']:.0f}) 도달 시 "
                 f"일부 익절 + 손절을 진입가로 올리기")
    if r.get("vetoed"):
        rules = [f"<b>회피</b>: 하락추세 — 신규 매수 금지. "
                 f"보유 시 방어선 {f(sr['defense'])} 이탈에서 정리"]
    rules_html = "".join(f"<li>{x}</li>" for x in rules)

    rs = (r.get("rs") or {}).get("rel")
    rs_txt = f"{rs*100:+.0f}%" if rs is not None else "-"
    mk = (r.get("market") or {}).get("direction", "-")
    rel = (r.get("trendline") or {}).get("reliability")
    rel_txt = f" · 추세선신뢰 {html.escape(rel)}" if rel else ""
    why = (f"신호 {html.escape(r['gauge'])} · 전환단계 "
           f"{html.escape(r.get('transition_label') or '–')} · RS {rs_txt} · 시장 {html.escape(mk)}"
           f"{rel_txt}")

    tm = timing(r)
    tm_html = f'<div class="plan-timing">{html.escape(tm)}</div>' if tm else ""
    zones_html = _zones_html(r, f)
    return _TMPL.format(
        color=color, head=html.escape(head), why=why, timing=tm_html,
        zones=zones_html,
        entry=f(entry), entry_desc=entry_desc, entry_src=entry_src,
        stop=f(risk["stop"]), stop_desc=stop_desc,
        target=f(risk["target"]), rr=f"{risk['rr']:.0f}",
        pos_desc=pos_desc, rules=rules_html)


def _zones_html(r: dict, f) -> str:
    """반등 예상 구간(컨플루언스) 카드 섹션."""
    zones = (r.get("levels") or {}).get("bounce_zones") or []
    if not zones:
        return ""
    ords = ["1차", "2차", "3차", "4차"]
    rows = []
    for i, z in enumerate(zones[:3]):
        rng = (f"{f(z['low'])}~{f(z['high'])}" if z["high"] - z["low"] > 1e-9
               else f(z["center"]))
        rows.append(
            f'<li><b>{ords[i]}</b> {rng} '
            f'<span class="zd">({z["dist_pct"]:+.1f}%)</span> · '
            f'{html.escape(z["label"])} <span class="zn">×{z["n_types"]}</span></li>')
    return ('<div class="plan-zones"><div class="zh">📍 반등 예상 구간 '
            '<span class="zhs">(지지 겹침 — 깨지면 다음 구간)</span></div>'
            f'<ul>{"".join(rows)}</ul></div>')


_TMPL = """<div class="plan">
  <div class="plan-h" style="border-color:{color}">
    <span class="plan-t">📋 매매 전략</span>
    <span class="plan-head" style="color:{color}">{head}</span>
  </div>
  {timing}
  {zones}
  <div class="plan-why">{why}</div>
  <table class="plan-tb">
    <tr><th>진입</th><td><b class="big">{entry}</b><div class="d">{entry_desc}</div>
      <div class="src">{entry_src}</div></td></tr>
    <tr><th>손절</th><td><b class="big">{stop}</b><div class="d">{stop_desc}</div></td></tr>
    <tr><th>목표</th><td><b class="big">{target}</b> <span class="dim">손익비 1:{rr}</span></td></tr>
    <tr><th>비중</th><td>{pos_desc}</td></tr>
  </table>
  <div class="plan-rules"><div class="rh">규칙 (If-Then)</div><ul>{rules}</ul></div>
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
  .plan-zones{margin:10px 14px 0;padding:9px 12px;border-radius:9px;background:#eff6ff;
    border:1px solid #bfdbfe}
  .plan-zones .zh{font-size:13px;font-weight:700;color:#1e40af;margin-bottom:4px}
  .plan-zones .zhs{font-weight:400;font-size:11px;color:#60a5fa}
  .plan-zones ul{margin:0;padding-left:16px} .plan-zones li{font-size:13px;margin:3px 0;color:#1e293b}
  .plan-zones .zd{color:#64748b;font-size:11px} .plan-zones .zn{color:#2563eb;font-weight:700;font-size:11px}
  .plan-why{padding:7px 14px;font-size:12px;color:#64748b;border-bottom:1px solid #f1f5f9;
    overflow-wrap:anywhere}
  .plan-tb{width:100%;border-collapse:collapse;table-layout:fixed}
  .plan-tb td,.plan-zones li{overflow-wrap:anywhere;word-break:break-word}
  .plan-tb th{width:54px;text-align:left;vertical-align:top;padding:10px 0 10px 14px;
    color:#64748b;font-size:13px;font-weight:700}
  .plan-tb td{padding:10px 14px 10px 6px;border-bottom:1px solid #f5f7fa;font-size:13.5px}
  .plan-tb .big{font-size:17px;color:#0f172a}
  .plan-tb .d{font-size:12.5px;color:#475569;margin-top:3px;line-height:1.6}
  .plan-tb .src{font-size:11px;color:#94a3b8;margin-top:3px}
  .plan .dim{color:#94a3b8;font-weight:400;font-size:11.5px}
  .plan-rules{padding:10px 14px}
  .plan-rules .rh{font-weight:700;font-size:13px;color:#334155;margin-bottom:4px}
  .plan-rules ul{margin:0;padding-left:18px} .plan-rules li{margin:4px 0;font-size:13px}
  .plan-warn{padding:8px 14px;background:#fffbeb;color:#92400e;font-size:11.5px}
"""
