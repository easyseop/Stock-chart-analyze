"""매매 전략 생성기 — 분석 결과(result)를 '진입·손절·목표 + If-Then 규칙'으로.

상세 페이지(lwc) 맨 위에 붙는 '📋 매매 전략' 카드를 만든다. raw 신호 카드와 달리
"그래서 어디서 사고/자르고/판다"를 한국어 규칙으로 잡아준다.
진입 추천 점수(rec_n)도 여기서 공유한다(screener·lwc 공용, 순환 import 방지).
"""
from __future__ import annotations

import html

import config

REC_MIN = 4   # 가이드 체크리스트 6개 중 4개 이상이면 '진입 추천'


def rec_n(r: dict) -> int:
    """진입 추천 점수 = 체크리스트 6개 중 충족 개수(0~6). 하락추세 veto면 0."""
    if r.get("vetoed"):
        return 0
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
    if nh is not None and -20 <= nh <= -3:
        n += 1
    if not r.get("chase"):
        n += 1
    if r.get("norm", 0) >= config.VERDICT_WEAK:
        n += 1
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
    if r.get("chase"):
        return "#d97706", "🔺 추격 주의 — 이미 과열/고점 확장. 눌림 대기."
    if rec >= REC_MIN:
        return "#16a34a", f"⭐ 진입 추천 (체크리스트 {rec}/6) — 조건 양호, 분할 진입 검토."
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
    why = (f"신호 {html.escape(r['gauge'])} · 전환단계 "
           f"{html.escape(r.get('transition_label') or '–')} · RS {rs_txt} · 시장 {html.escape(mk)}")

    return _TMPL.format(
        color=color, head=html.escape(head), why=why,
        entry=f(entry), entry_desc=entry_desc, entry_src=entry_src,
        stop=f(risk["stop"]), stop_desc=stop_desc,
        target=f(risk["target"]), rr=f"{risk['rr']:.0f}",
        pos_desc=pos_desc, rules=rules_html)


_TMPL = """<div class="plan">
  <div class="plan-h" style="border-color:{color}">
    <span class="plan-t">📋 매매 전략</span>
    <span class="plan-head" style="color:{color}">{head}</span>
  </div>
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
  .plan-why{padding:7px 14px;font-size:12px;color:#64748b;border-bottom:1px solid #f1f5f9}
  .plan-tb{width:100%;border-collapse:collapse}
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
