"""단일 페이지 대시보드 (HTML) — v1.6.

5종목을 한 페이지에 모아:
  - 하락→상승 '전환 후보' 기준으로 그룹 분류(전환 후보 / 회피 / 관망)
  - 종목 탭 전환
  - 지표(이평선·핵심선·매매선·추세선·지지저항·피보·매물대) ON/OFF 토글 버튼
  - 종목별 신호 카드(손절·진입·목표 포함) + 인터랙티브 차트
호버는 'closest'라 마우스 근처 한 요소만 떠 정보가 쏟아지지 않는다.
"""
from __future__ import annotations

import html
import os

from scanner import card, chart


# 분류: 추세선 state 기준 (사용자 핵심 관심 = 하락→상승 전환 후보)
_BUCKETS = [
    ("transition", "🟢 전환 후보 (하락추세선 돌파·임박)"),
    ("watch",      "⚪ 관망 (방향 미정)"),
    ("avoid",      "🔴 회피 (하락추세 지속·상승추세 이탈)"),
]


def _bucket(result: dict) -> str:
    state = result["trendline"]["state"]
    if state in ("하락추세선 상향돌파", "하락추세선 임박"):
        return "transition"
    if state in ("하락추세 지속", "상승추세선 이탈"):
        return "avoid"
    return "watch"


def _card_html(result: dict) -> str:
    """텍스트 카드에서 용어 각주만 떼어내 <pre>로."""
    text = card.render(result)
    text = text.split("\n용어:")[0]            # 각주 제거(차트 호버로 대체)
    return f'<pre class="card">{html.escape(text)}</pre>'


def build(results: list[dict], frames_map: dict[str, dict],
          out_path: str = "dashboard.html") -> str:
    """결과들 → 단일 대시보드 HTML 저장. 경로 반환.

    frames_map: code -> frames(dict) (차트 그리기에 필요한 OHLCV)
    """
    # 버킷별로 정렬(전환 후보 → 관망 → 회피), 버킷 내 점수 내림차순
    ordered = []
    for key, _label in _BUCKETS:
        grp = [r for r in results if _bucket(r) == key]
        grp.sort(key=lambda r: r["norm"], reverse=True)
        ordered.extend((key, r) for r in grp)

    tabs, panels = [], []
    first_code = ordered[0][1]["code"] if ordered else None
    for bucket, r in ordered:
        code = r["code"]
        fig = chart.build_figure(r, frames_map[code])
        fig.update_layout(autosize=True, width=None, height=680)
        div = fig.to_html(include_plotlyjs=False, full_html=False,
                          div_id=f"plot-{code}",
                          config={"displaylogo": False, "responsive": True})
        active = " active" if code == first_code else ""
        gauge = html.escape(r["gauge"])
        tabs.append(
            f'<button class="tab b-{bucket}{active}" data-code="{code}" '
            f'onclick="showStock(\'{code}\')">'
            f'<span class="g">{gauge}</span> {html.escape(r["name"])} '
            f'<span class="sub">{html.escape(code)} · {r["norm"]:+.0f}</span></button>')
        panels.append(
            f'<section class="panel{active}" id="panel-{code}">'
            f'{_card_html(r)}<div class="chart">{div}</div></section>')

    # 버킷 헤더를 탭 목록 사이에 끼워 재구성
    tab_html = []
    seen = set()
    for i, (bucket, r) in enumerate(ordered):
        if bucket not in seen:
            seen.add(bucket)
            tab_html.append(
                f'<div class="bucket-h">{html.escape(dict(_BUCKETS)[bucket])}</div>')
        tab_html.append(tabs[i])

    # 토글 버튼 (chart.GROUPS의 기본 ON/OFF 반영)
    btns = []
    for key, label, on in chart.GROUPS:
        cls = "toggle on" if on else "toggle"
        btns.append(f'<button class="{cls}" data-group="{key}" '
                    f'onclick="toggleGroup(this)">{html.escape(label)}</button>')

    default_on = "{" + ",".join(
        f'"{k}":{"true" if on else "false"}' for k, _l, on in chart.GROUPS) + "}"

    doc = _TEMPLATE.format(
        tabs="\n".join(tab_html),
        toggles="\n".join(btns),
        panels="\n".join(panels),
        default_on=default_on,
        first=first_code or "")
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(doc)
    return out_path


_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>심리 신호 대시보드</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root {{ --bg:#0f172a; --panel:#fff; --ink:#1e293b; --mut:#64748b; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans KR',sans-serif;
         color:var(--ink); background:#f1f5f9; }}
  header {{ background:var(--bg); color:#fff; padding:14px 20px; }}
  header h1 {{ font-size:17px; margin:0; }}
  header p {{ margin:4px 0 0; font-size:12px; color:#94a3b8; }}
  .wrap {{ display:flex; gap:14px; padding:14px; align-items:flex-start; }}
  .side {{ flex:0 0 250px; }}
  .bucket-h {{ font-size:12px; font-weight:700; color:var(--mut);
              margin:12px 4px 6px; text-transform:none; }}
  .tab {{ display:block; width:100%; text-align:left; border:1px solid #e2e8f0;
         background:#fff; border-radius:10px; padding:9px 11px; margin:5px 0;
         cursor:pointer; font-size:13px; transition:.12s; }}
  .tab:hover {{ border-color:#94a3b8; }}
  .tab.active {{ border-color:#2563eb; box-shadow:0 0 0 2px #2563eb22; }}
  .tab .g {{ font-weight:700; }}
  .tab .sub {{ color:var(--mut); font-size:11px; float:right; }}
  .b-transition {{ border-left:4px solid #16a34a; }}
  .b-avoid {{ border-left:4px solid #ef4444; }}
  .b-watch {{ border-left:4px solid #94a3b8; }}
  .main {{ flex:1 1 auto; min-width:0; }}
  .toolbar {{ display:flex; flex-wrap:wrap; gap:7px; align-items:center;
             background:#fff; border:1px solid #e2e8f0; border-radius:10px;
             padding:9px 11px; margin-bottom:12px; }}
  .toolbar .lab {{ font-size:12px; color:var(--mut); margin-right:4px; }}
  .toggle {{ border:1px solid #cbd5e1; background:#f8fafc; color:#64748b;
            border-radius:999px; padding:5px 12px; cursor:pointer; font-size:12px; }}
  .toggle.on {{ background:#2563eb; border-color:#2563eb; color:#fff; }}
  .panel {{ display:none; }}
  .panel.active {{ display:block; }}
  .card {{ background:#0f172a; color:#e2e8f0; padding:14px 16px; border-radius:10px;
          font-size:12px; line-height:1.5; overflow:auto; white-space:pre;
          font-family:'D2Coding','Menlo','Consolas',monospace; }}
  .chart {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px;
           margin-top:12px; padding:4px; }}
  .hint {{ font-size:11px; color:var(--mut); margin:6px 2px 0; }}
</style></head><body>
<header>
  <h1>심리 신호 대시보드 <span style="color:#38bdf8;font-size:13px">차트만으로 읽는 군중심리</span></h1>
  <p>좌측에서 종목 선택 · 상단 버튼으로 지표 ON/OFF · 차트 위 요소에 마우스를 올리면 그 하나만 설명이 뜸</p>
</header>
<div class="wrap">
  <nav class="side">{tabs}</nav>
  <div class="main">
    <div class="toolbar">
      <span class="lab">지표</span>
      {toggles}
    </div>
    <div class="hint">손절/진입/목표선은 '매매선' 버튼으로 표시 — 손절가는 ATR손절·방어선손절 중 보수적인 값으로 자동 산출됩니다.</div>
    {panels}
  </div>
</div>
<script>
  var STATE = {default_on};
  var ACTIVE = "{first}";

  function applyAll(code) {{
    var gd = document.getElementById("plot-" + code);
    if (!gd || !gd.data) return;
    Object.keys(STATE).forEach(function(group) {{
      var idx = [];
      gd.data.forEach(function(t, i) {{ if (t.legendgroup === group) idx.push(i); }});
      if (idx.length) Plotly.restyle(gd, {{visible: STATE[group]}}, idx);
    }});
    Plotly.Plots.resize(gd);
  }}

  function showStock(code) {{
    document.querySelectorAll(".tab").forEach(function(t) {{
      t.classList.toggle("active", t.dataset.code === code);
    }});
    document.querySelectorAll(".panel").forEach(function(p) {{
      p.classList.toggle("active", p.id === "panel-" + code);
    }});
    ACTIVE = code;
    applyAll(code);
  }}

  function toggleGroup(btn) {{
    var group = btn.dataset.group;
    STATE[group] = !STATE[group];
    btn.classList.toggle("on", STATE[group]);
    var gd = document.getElementById("plot-" + ACTIVE);
    if (!gd || !gd.data) return;
    var idx = [];
    gd.data.forEach(function(t, i) {{ if (t.legendgroup === group) idx.push(i); }});
    if (idx.length) Plotly.restyle(gd, {{visible: STATE[group]}}, idx);
  }}

  window.addEventListener("load", function() {{ if (ACTIVE) applyAll(ACTIVE); }});
</script>
</body></html>"""
