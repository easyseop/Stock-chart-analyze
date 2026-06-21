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
    with open(os.path.join(out_dir, "lookup.html"), "w", encoding="utf-8") as fp:
        fp.write(_REPO and _trigger_page())   # 웹 즉석 조회(워크플로 트리거) 페이지
    return out_dir


# 저장소 정보(워크플로 트리거 대상). 다른 저장소면 여기만 바꾸면 됨.
_REPO = "easyseop/Stock-chart-analyze"


def _trigger_page() -> str:
    return _LOOKUP_TMPL.replace("__REPO__", _REPO)


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
  <a class="chip" href="lookup.html" style="margin-left:auto;background:#0f172a;color:#fff;border-color:#0f172a;text-decoration:none">➕ 티커 즉석 조회</a>
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


_LOOKUP_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>티커 즉석 조회</title>
<style>
  body{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}
  header{background:#0f172a;color:#fff;padding:14px 20px}
  header h1{margin:0;font-size:17px} header a{color:#7dd3fc;font-size:13px;text-decoration:none}
  .wrap{max-width:620px;margin:0 auto;padding:18px}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:#334155}
  input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cbd5e1;
    border-radius:8px;font-size:15px}
  button.go{width:100%;margin-top:14px;padding:13px;border:0;border-radius:10px;
    background:#2563eb;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
  button.go:disabled{background:#94a3b8}
  .hint{font-size:12px;color:#64748b;line-height:1.6;margin-top:6px}
  .stat{font-size:14px;margin-top:12px;padding:12px;border-radius:8px;background:#f8fafc;
    border:1px solid #e2e8f0;display:none}
  .stat.on{display:block}
  .pbar{height:12px;background:#e2e8f0;border-radius:999px;overflow:hidden;margin-top:12px;display:none}
  .pbar.on{display:block}
  .pfill{height:100%;width:0;background:#2563eb;transition:width .5s ease;border-radius:999px}
  .pfill.anim{background-image:linear-gradient(90deg,#2563eb,#60a5fa,#2563eb);background-size:200% 100%;animation:flow 1.2s linear infinite}
  @keyframes flow{0%{background-position:0 0}100%{background-position:-200% 0}}
  details summary{cursor:pointer;font-weight:600;color:#334155}
  code{background:#f1f5f9;padding:1px 5px;border-radius:4px}
</style></head><body>
<header><a href="index.html">&larr; 스크리너</a>
<h1>티커 즉석 조회 <span style="color:#38bdf8;font-size:13px">웹에서 바로 수집</span></h1></header>
<div class="wrap">
  <div class="card">
    <label>티커 / 코드</label>
    <input id="tk" placeholder="예: AAPL, NVDA, 005930" autocapitalize="characters">
    <button class="go" id="go" onclick="run()">수집하기 (워크플로 실행)</button>
    <div class="stat" id="st"></div>
    <div class="pbar" id="pb"><div class="pfill" id="pf"></div></div>
    <div class="hint">미국 티커 또는 한국 6자리 코드. 누르면 GitHub Actions의 lookup 워크플로가
      돌아 그 종목을 분석합니다(약 1~2분). 끝나면 결과 링크가 떠요.</div>
  </div>
  <div class="card">
    <details><summary>최초 1회: 내 GitHub 토큰 입력 (브라우저에만 저장)</summary>
    <label>GitHub 토큰 (PAT)</label>
    <input id="pat" type="password" placeholder="github_pat_...">
    <div class="hint">웹에서 워크플로를 켜려면 토큰이 필요합니다. <b>이 토큰은 당신 브라우저에만
      저장</b>(localStorage)되고 어디에도 전송/공개되지 않습니다(깃허브 API 호출에만 사용).<br>
      만드는 법: GitHub &rarr; Settings &rarr; Developer settings &rarr;
      <b>Fine-grained tokens</b> &rarr; 이 저장소만 선택 &rarr; 권한
      <code>Actions: Read and write</code> 부여 &rarr; 생성한 토큰을 여기 붙여넣기.</div>
    </details>
  </div>
</div>
<script>
  var REPO="__REPO__", WF="lookup.yml";
  var pat=document.getElementById('pat'), tk=document.getElementById('tk'),
      st=document.getElementById('st'), go=document.getElementById('go');
  pat.value=localStorage.getItem('ghpat')||"";
  function show(m){st.className='stat on';st.innerHTML=m;}
  function bar(p,c,anim){var pb=document.getElementById('pb'),pf=document.getElementById('pf');
    pb.className='pbar on';pf.style.width=p+'%';if(c)pf.style.background=c;
    pf.className='pfill'+(anim?' anim':'');}
  async function defaultBranch(){
    try{var r=await fetch('https://api.github.com/repos/'+REPO);
      return (await r.json()).default_branch||'main';}catch(e){return 'main';}
  }
  async function run(){
    var t=tk.value.trim().toUpperCase(), p=pat.value.trim();
    if(!t){show('티커를 입력하세요.');return;}
    if(!p){show('먼저 아래 토큰을 입력하세요(최초 1회).');return;}
    localStorage.setItem('ghpat',p);
    go.disabled=true; show('실행 요청 중...'); bar(12,null,true);
    var ref=await defaultBranch();
    var r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/dispatches',{
      method:'POST',
      headers:{'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28'},
      body:JSON.stringify({ref:ref,inputs:{ticker:t}})
    });
    go.disabled=false;
    if(r.status===204){
      show('✅ <b>'+t+'</b> 수집 시작! 1~2분 후 결과를 봅니다...'); bar(30,null,true);
      setTimeout(function(){track(p,t);},6000);
    }else{
      var e=await r.text();
      show('❌ 실패 ('+r.status+'). 토큰/권한 확인.<br><small>'+e.slice(0,160)+'</small>'); bar(100,'#dc2626',false);
    }
  }
  async function track(p,t){
    try{
      var r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/runs?per_page=1',
        {headers:{'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json'}});
      var run=(await r.json()).workflow_runs[0];
      if(!run){show('실행 대기 중...');bar(35,null,true);setTimeout(function(){track(p,t);},5000);return;}
      if(run.status!=='completed'){
        show('⏳ <b>'+t+'</b> 수집·분석 중... ('+run.status+') · <a href="'+run.html_url+'" target=_blank>로그</a>');
        bar(run.status==='queued'?45:75,null,true);
        setTimeout(function(){track(p,t);},5000);
      }else{
        var ok=run.conclusion==='success';
        bar(100,ok?'#16a34a':'#dc2626',false);
        show((ok?'✅ 완료!':'⚠️ '+run.conclusion)+' <b>'+t+'</b><br>'
          +'<a href="'+run.html_url+'" target=_blank>결과/차트(아티팩트) 보기 &rarr;</a>'
          +'<div class=hint>로그에 신호 카드, 하단 Artifacts에 상세차트 HTML.</div>');
      }
    }catch(e){show('상태 조회 오류: '+e);}
  }
  tk.addEventListener('keydown',function(e){if(e.key==='Enter')run();});
</script></body></html>"""
