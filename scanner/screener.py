"""대량 종목 스크리너 — 가벼운 정렬/필터 표(index) + 종목별 상세 차트 페이지.

전 종목 차트를 한 페이지에 그리면 무거우므로 분리한다:
  - index.html          : 점수·RS·신고가·판정 표(한 종목=한 줄) → 수백~수천 종목도 가벼움
  - stocks/{code}.html  : 클릭 시 열리는 상세(일/주/월 차트·지표 토글·신호 카드)
"""
from __future__ import annotations

import html
import os

from scanner import card, chart
from scanner.dashboard import _BUCKETS, _bucket

PLOTLY = "https://cdn.plot.ly/plotly-2.35.2.min.js"
_TFS = [("D", "일봉"), ("W", "주봉"), ("M", "월봉")]
_BUCKET_KO = {"transition": "🟢전환", "watch": "⚪관망", "avoid": "🔴회피"}


def _card_html(result: dict) -> str:
    text = card.render(result).split("\n용어:")[0]
    return f'<pre class="card">{html.escape(text)}</pre>'


def _detail(result: dict, frames: dict) -> str:
    """종목별 상세 페이지(일/주/월 토글 + 지표 토글 + 카드)."""
    code = result["code"]
    charts, tfbtns = [], []
    for tf, label in _TFS:
        fig = chart.build_figure(result, frames, tf=tf)
        fig.update_layout(autosize=True, width=None, height=640)
        div = fig.to_html(include_plotlyjs=False, full_html=False,
                          div_id=f"plot-{code}-{tf}",
                          config={"displaylogo": False, "responsive": True})
        disp = "block" if tf == "D" else "none"
        charts.append(f'<div class="tfchart" id="tf-{code}-{tf}" '
                      f'style="display:{disp}">{div}</div>')
        on = " active" if tf == "D" else ""
        tfbtns.append(f'<button class="tfbtn{on}" id="tfb-{tf}" '
                      f'onclick="switchTf(\'{tf}\')">{label}</button>')
    toggles = "".join(
        f'<button class="toggle{" on" if on else ""}" data-group="{k}" '
        f'onclick="tg(this)">{html.escape(lab)}</button>'
        for k, lab, on in chart.GROUPS)
    default_on = "{" + ",".join(
        f'"{k}":{"true" if on else "false"}' for k, _l, on in chart.GROUPS) + "}"
    title = (f'{html.escape(result["name"])} {html.escape(code)} · '
             f'{html.escape(result["gauge"])} 정규화 {result["norm"]:+.0f}')
    return _DETAIL_TMPL.format(
        title=title, code=code, plotly=PLOTLY, toggles=toggles,
        tfbtns="".join(tfbtns), charts="".join(charts),
        card=_card_html(result), default_on=default_on,
        verdict=html.escape(result.get("verdict", "")))


def _rows(results: list[dict]) -> str:
    out = []
    for r in results:
        b = _bucket(r)
        code = r["code"]
        rs = r.get("rs", {}).get("rel")
        rs_txt = f"{rs*100:+.0f}%" if rs is not None else "-"
        nh = r.get("newhigh", {}).get("pct_from_high")
        nh_txt = f"{nh:+.0f}%" if nh is not None else "-"
        mk = r.get("market", {}).get("direction", "-")
        tone = html.escape(r.get("trend_oneline", ""))
        stg = r.get("transition_stage", 0)
        stg_lab = html.escape(r.get("transition_label", ""))
        vd = html.escape(r.get("verdict", ""))
        if r.get("chase"):
            vd = "🔺추격주의 · " + vd
        out.append(
            f'<tr class="b-{b}" data-bucket="{b}" data-stage="{stg}">'
            f'<td>{html.escape(r["gauge"])}</td>'
            f'<td class="nm"><a href="stocks/{code}.html">'
            f'{html.escape(r["name"])}</a><span class="cd">{html.escape(code)}</span></td>'
            f'<td data-v="{stg}" class="num stg">{stg_lab}</td>'
            f'<td>{tone}</td>'
            f'<td data-v="{r["norm"]:.1f}" class="num sc">{r["norm"]:+.0f}</td>'
            f'<td>{html.escape(mk)}</td>'
            f'<td data-v="{rs if rs is not None else -999}" class="num">{rs_txt}</td>'
            f'<td data-v="{nh if nh is not None else -999}" class="num">{nh_txt}</td>'
            f'<td class="vd">{vd}</td></tr>')
    return "".join(out)


def _index(results: list[dict]) -> str:
    # 기본 정렬: 전환 단계 높은 순(하락→상승 전환 임박/확정) → 그다음 점수
    results = sorted(results,
                     key=lambda r: (r.get("transition_stage", 0), r["norm"]),
                     reverse=True)
    counts = {k: sum(1 for r in results if _bucket(r) == k) for k, _ in _BUCKETS}
    tcount = sum(1 for r in results if r.get("transition_stage", 0) > 0)
    chips = "".join(
        f'<button class="chip" onclick="flt(\'{k}\')">{_BUCKET_KO[k]} {counts[k]}</button>'
        for k, _ in _BUCKETS)
    return _INDEX_TMPL.format(
        n=len(results), rows=_rows(results), chips=chips, tcount=tcount)


def build(results: list[dict], frames_map: dict[str, dict],
          out_dir: str = "public", metas: dict | None = None) -> str:
    """스크리너(index.html) + 종목별 상세(stocks/*.html) 생성. out_dir 반환."""
    os.makedirs(os.path.join(out_dir, "stocks"), exist_ok=True)
    for r in results:
        code = r["code"]
        path = os.path.join(out_dir, "stocks", f"{code}.html")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(_detail(r, frames_map[code]))
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(_index(results))
    return out_dir


_INDEX_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>종목 스크리너</title>
<style>
  body{{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}}
  header{{background:#0f172a;color:#fff;padding:14px 20px}}
  header h1{{margin:0;font-size:17px}}
  header p{{margin:4px 0 0;font-size:12px;color:#94a3b8}}
  .bar{{padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  .chip{{border:1px solid #cbd5e1;background:#fff;border-radius:999px;padding:6px 14px;
    cursor:pointer;font-size:13px}}
  .chip.on{{background:#2563eb;border-color:#2563eb;color:#fff}}
  table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px}}
  th,td{{padding:8px 10px;border-bottom:1px solid #eef2f7;text-align:right}}
  th{{background:#f8fafc;color:#64748b;font-size:12px;position:sticky;top:0;cursor:pointer;
    user-select:none}}
  th:nth-child(2),td.nm{{text-align:left}}
  td.vd{{text-align:left;color:#475569;max-width:360px}}
  td.sc{{font-weight:700}}
  .num{{font-variant-numeric:tabular-nums}}
  td.stg{{text-align:left;color:#16a34a;font-weight:600;font-size:12px}}
  .nm a{{color:#1d4ed8;text-decoration:none;font-weight:600}}
  .nm .cd{{color:#94a3b8;font-size:11px;margin-left:6px}}
  tr.b-transition td.sc{{color:#16a34a}}
  tr.b-avoid td.sc{{color:#dc2626}}
  .pos{{color:#16a34a}}.neg{{color:#dc2626}}
</style></head><body>
<header><h1>종목 스크리너 <span style="color:#38bdf8;font-size:13px">차트 신호 랭킹</span></h1>
<p>{n}종목 · 헤더 클릭=정렬 · 칩=필터 · 종목명 클릭=상세 차트(일/주/월)</p></header>
<div class="bar">
  <button class="chip" onclick="flt('all')">전체</button>
  <button class="chip on" onclick="fltStage()">🔄 전환후보 {tcount}</button>
  {chips}
</div>
<table id="t"><thead><tr>
  <th onclick="srt(0,false)">신호</th>
  <th onclick="srt(1,false)">종목</th>
  <th onclick="srt(2,true)">전환단계▼</th>
  <th onclick="srt(3,false)">추세</th>
  <th onclick="srt(4,true)">점수</th>
  <th onclick="srt(5,false)">시장</th>
  <th onclick="srt(6,true)">RS</th>
  <th onclick="srt(7,true)">신고가</th>
  <th onclick="srt(8,false)">판정</th>
</tr></thead><tbody id="tb">{rows}</tbody></table>
<script>
  var tb=document.getElementById('tb');
  function srt(col,num){{
    var rows=[].slice.call(tb.querySelectorAll('tr'));
    rows.sort(function(a,b){{
      var x=a.children[col],y=b.children[col];
      if(num){{return parseFloat(y.dataset.v)-parseFloat(x.dataset.v);}}
      return (x.textContent>y.textContent?1:-1);
    }});
    rows.forEach(function(r){{tb.appendChild(r);}});
  }}
  function setOn(btn){{
    document.querySelectorAll('.chip').forEach(function(c){{c.classList.remove('on');}});
    if(btn) btn.classList.add('on');
  }}
  function flt(b){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(b==='all'||r.dataset.bucket===b)?'':'none';
    }});
  }}
  function fltStage(){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.stage||'0')>0)?'':'none';
    }});
  }}
  // 첫 화면: '전환 후보'(하락→상승 전환 단계)만 우선 표시
  window.addEventListener('load',function(){{
    var any=false;
    tb.querySelectorAll('tr').forEach(function(r){{if(parseInt(r.dataset.stage||'0')>0)any=true;}});
    if(any){{tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.stage||'0')>0)?'':'none';}});}}
    else{{setOn(document.querySelectorAll('.chip')[0]);}}
  }});
</script></body></html>"""


_DETAIL_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="{plotly}" charset="utf-8"></script>
<style>
  body{{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}}
  header{{background:#0f172a;color:#fff;padding:12px 18px}}
  header a{{color:#7dd3fc;font-size:13px;text-decoration:none}}
  header h1{{margin:4px 0 0;font-size:16px}}
  .vt{{font-size:12px;color:#94a3b8;margin-top:3px}}
  .wrap{{padding:14px}}
  .toolbar,.tfbar{{display:flex;flex-wrap:wrap;gap:7px;align-items:center;
    background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:9px 11px;
    margin-bottom:10px}}
  .toggle{{border:1px solid #cbd5e1;background:#f8fafc;color:#64748b;border-radius:999px;
    padding:5px 12px;cursor:pointer;font-size:12px}}
  .toggle.on{{background:#2563eb;border-color:#2563eb;color:#fff}}
  .tfbtn{{border:1px solid #cbd5e1;background:#fff;color:#475569;border-radius:8px;
    padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600}}
  .tfbtn.active{{background:#0f172a;border-color:#0f172a;color:#fff}}
  .chart{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:4px}}
  .card{{background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:10px;
    font-size:12px;line-height:1.5;white-space:pre;overflow:auto;margin-top:12px;
    font-family:'D2Coding','Menlo','Consolas',monospace}}
  details>summary{{cursor:pointer;font-size:13px;font-weight:600;color:#334155;
    padding:8px 12px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin-top:12px}}
</style></head><body>
<header><a href="../index.html">← 스크리너 목록</a><h1>{title}</h1>
<div class="vt">{verdict}</div></header>
<div class="wrap">
  <div class="toolbar"><span style="font-size:12px;color:#64748b">지표</span>{toggles}</div>
  <div class="tfbar">{tfbtns}<span style="font-size:11px;color:#64748b;margin-left:6px">
    ← 일/주/월 전환(추세는 프레임마다 다름)</span></div>
  <div class="chart">{charts}</div>
  <details><summary>📋 상세 신호 카드</summary>{card}</details>
</div>
<script>
  var STATE={default_on}, CODE="{code}", TF="D";
  function pid(){{return "plot-"+CODE+"-"+TF;}}
  function apply(){{
    var gd=document.getElementById(pid()); if(!gd||!gd.data)return;
    Object.keys(STATE).forEach(function(g){{
      var idx=[];gd.data.forEach(function(t,i){{if(t.legendgroup===g)idx.push(i);}});
      if(idx.length)Plotly.restyle(gd,{{visible:STATE[g]}},idx);
    }});
    Plotly.Plots.resize(gd);
  }}
  function switchTf(tf){{
    TF=tf;
    ["D","W","M"].forEach(function(t){{
      var c=document.getElementById("tf-"+CODE+"-"+t);if(c)c.style.display=(t===tf)?"block":"none";
      var b=document.getElementById("tfb-"+t);if(b)b.classList.toggle("active",t===tf);
    }});
    apply();
  }}
  function tg(btn){{
    var g=btn.dataset.group;STATE[g]=!STATE[g];btn.classList.toggle("on",STATE[g]);
    var gd=document.getElementById(pid());if(!gd||!gd.data)return;
    var idx=[];gd.data.forEach(function(t,i){{if(t.legendgroup===g)idx.push(i);}});
    if(idx.length)Plotly.restyle(gd,{{visible:STATE[g]}},idx);
  }}
  window.addEventListener("load",apply);
</script></body></html>"""
