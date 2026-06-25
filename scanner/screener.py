"""대량 종목 스크리너 — 가벼운 정렬/필터 표(index) + 종목별 상세 차트 페이지.

전 종목 차트를 한 페이지에 그리면 무거우므로 분리한다:
  - index.html          : 점수·RS·신고가·판정 표(한 종목=한 줄) → 수백~수천 종목도 가벼움
  - stocks/{code}.html  : 클릭 시 열리는 상세(일/주/월 차트·지표 토글·신호 카드)
"""
from __future__ import annotations

import html
import os

from scanner import card, earnings, intraday, lwc, names_ko
from scanner.dashboard import _BUCKETS, _bucket

_BUCKET_KO = {"transition": "🟢전환후보", "uptrend": "📈상승추세",
              "watch": "⚪관망", "avoid": "🔴회피"}

# 전환단계 칸 마우스오버 설명(뜻 + 판정 기준)
_STAGE_TIP = {
    4: ("④ 전환 확정 — 하락추세선을 거래량 동반 돌파 → 되눌림 후 안착 → "
        "위로 상승추세선까지 형성. 셋 다 충족된 가장 강한 하락→상승 전환 신호."),
    3: ("③ 돌파후 횡보(대기) — 하락추세선은 넘었고 그 위에서 다지는 중. "
        "상승추세선/거래량이 확인되면 ④ 전환 확정으로 올라감."),
    2: ("② 갓 돌파(미확인) — 하락추세선을 막 넘었지만 되밀릴 수 있어 안착 미확인. 관망."),
    1: ("① 임박 — 아직 하락추세선 아래지만 저항에 바짝 근접. 돌파하면 전환 시작."),
    0: "전환 신호 없음(추세 전환 단계에 해당하지 않음).",
}
# 신호 칸 마우스오버 설명(종합 점수 −100~+100 기준)
_GAUGE_TIP = {
    "🟢 강세": "종합점수 +50 이상 — 적극 매수 구간(여러 지표 강세 정렬).",
    "🟢 관심": "종합점수 +20~+50 — 매수 관심(지지 확인 후 진입).",
    "⚪ 중립": "종합점수 −20~+20 — 관망(방향성 신호 부족).",
    "🔴 주의": "종합점수 −50~−20 — 매도 관심/비중 축소.",
    "🔴 공포": "종합점수 −50 이하 — 적극 회피/청산.",
    "🔴 하락추세": "하락추세선 아래 — 추세 전환 전까지 회피.",
}


def _detail(result: dict, frames: dict) -> str:
    """종목별 상세 페이지 — lightweight-charts(모바일 핀치/팬 부드러움)."""
    return lwc.detail(result, frames)


from scanner.plan import rec_n as _rec_n, REC_MIN


def _rows(results: list[dict]) -> str:
    out = []
    for r in results:
        b = _bucket(r)
        code = r["code"]
        rec = _rec_n(r)
        recd = rec >= REC_MIN
        star = (f'<span class="star" title="진입 추천 — 체크리스트 {rec}/6 충족">'
                f'⭐{rec}</span>') if recd else ""
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
        edays = earnings.days_until(code)        # 네트워크 0(캐시만)
        if edays is not None and 0 <= edays <= earnings.NEAR_DAYS:
            vd = f"📅어닝 D-{edays}(갭주의) · " + vd
        gauge = r["gauge"]
        gtip = html.escape(_GAUGE_TIP.get(gauge, ""), quote=True)
        stip = html.escape(_STAGE_TIP.get(stg, ""), quote=True)
        ko = names_ko.ko(code)
        ko_html = (f'<span class="ko">{html.escape(ko)}</span>' if ko else "")
        out.append(
            f'<tr class="b-{b}{" rec" if recd else ""}" data-bucket="{b}" '
            f'data-stage="{stg}" data-rec="{rec}">'
            f'<td data-label="신호" title="{gtip}">{star}{html.escape(gauge)}</td>'
            f'<td class="nm"><a href="stocks/{code}.html">'
            f'{html.escape(r["name"])}</a>{ko_html}'
            f'<span class="cd">{html.escape(code)}</span></td>'
            f'<td data-label="전환단계" data-v="{stg}" class="num stg" '
            f'title="{stip}">{stg_lab}</td>'
            f'<td data-label="추세">{tone}</td>'
            f'<td data-label="점수" data-v="{r["norm"]:.1f}" class="num sc">{r["norm"]:+.0f}</td>'
            f'<td data-label="시장">{html.escape(mk)}</td>'
            f'<td data-label="RS" data-v="{rs if rs is not None else -999}" class="num">{rs_txt}</td>'
            f'<td data-label="신고가" data-v="{nh if nh is not None else -999}" class="num">{nh_txt}</td>'
            f'<td data-label="판정" class="vd">{vd}</td></tr>')
    return "".join(out)


def _index(results: list[dict]) -> str:
    import datetime
    from scanner import cache, universe
    # 기본 정렬: 진입 추천 점수 → 전환 단계 → 종합점수 (추천 종목이 맨 위로)
    results = sorted(results,
                     key=lambda r: (_rec_n(r), r.get("transition_stage", 0), r["norm"]),
                     reverse=True)
    counts = {k: sum(1 for r in results if _bucket(r) == k) for k, _ in _BUCKETS}
    rcount = sum(1 for r in results if _rec_n(r) >= REC_MIN)
    chips = "".join(
        f'<button class="chip" onclick="flt(\'{k}\')">{_BUCKET_KO[k]} {counts[k]}</button>'
        for k, _ in _BUCKETS)
    # 수집 진행률(캐시된 종목 / 유니버스 전체)
    try:
        cached = len(cache.cached_codes())
        uni = len([s for s in universe.load() if s.get("code")]) or 1
    except Exception:
        cached, uni = len(results), max(len(results), 1)
    pct = min(100, round(cached / uni * 100))
    updated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return _INDEX_TMPL.format(
        n=len(results), rows=_rows(results), chips=chips, rcount=rcount,
        cached=cached, uni=uni, pct=pct, updated=updated)


def build(results: list[dict], frames_map: dict[str, dict],
          out_dir: str = "public", metas: dict | None = None) -> str:
    """스크리너(index.html) + 종목별 상세(stocks/*.html) 생성. out_dir 반환."""
    import config
    os.makedirs(os.path.join(out_dir, "stocks"), exist_ok=True)
    h_min = config.MA_PERIODS["H"][-1] + 5
    for r in results:
        code = r["code"]
        frames = frames_map[code]
        h = intraday.frame(code)                 # 시간봉 캐시(네트워크 0). 없으면 None
        if h is not None and len(h) >= h_min:
            frames = {**frames, "H": h}          # 충분하면 '시간봉' 탭 추가
        path = os.path.join(out_dir, "stocks", f"{code}.html")
        try:
            page = _detail(r, frames)
        except Exception:                        # 한 종목 실패가 전체 빌드를 막지 않도록
            page = _detail(r, {k: v for k, v in frames.items() if k != "H"})
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(page)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(_index(results))
    with open(os.path.join(out_dir, "lookup.html"), "w", encoding="utf-8") as fp:
        fp.write(_REPO and _trigger_page())   # 웹 즉석 조회(워크플로 트리거) 페이지
    with open(os.path.join(out_dir, "guide.html"), "w", encoding="utf-8") as fp:
        fp.write(_GUIDE_HTML)                  # 매매 가이드(읽기 전용 안내)
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
  .nm .ko{{color:#475569;font-size:12px;margin-left:6px}}
  .nm .cd{{color:#94a3b8;font-size:11px;margin-left:6px}}
  .legend{{margin:0 16px 4px;background:#fff;border:1px solid #e2e8f0;border-radius:10px}}
  .legend summary{{cursor:pointer;padding:9px 13px;font-size:13px;font-weight:600;color:#334155}}
  .legend .lc{{padding:4px 15px 13px;font-size:12.5px;color:#475569;line-height:1.75}}
  .legend b{{color:#0f172a}}
  tr.b-transition td.sc{{color:#16a34a}}
  tr.b-uptrend td.sc{{color:#0284c7}}
  tr.b-avoid td.sc{{color:#dc2626}}
  tr.rec{{background:#fffbeb}}
  .star{{font-size:11px;font-weight:700;color:#d97706;margin-right:4px}}
  .rec-chip{{background:#fef3c7;border-color:#f59e0b;color:#92400e;font-weight:700}}
  .rec-chip.on{{background:#f59e0b;border-color:#f59e0b;color:#fff}}
  .pos{{color:#16a34a}}.neg{{color:#dc2626}}
  .prog{{padding:8px 16px 0}}
  .ptxt{{font-size:12px;color:#475569;margin-bottom:4px}}
  .pbarw{{height:10px;background:#e2e8f0;border-radius:999px;overflow:hidden}}
  .pfillw{{height:100%;background:linear-gradient(90deg,#16a34a,#22c55e);border-radius:999px}}
  .search{{position:sticky;top:0;z-index:9;padding:8px 16px;display:flex;
    align-items:center;gap:8px;background:#f1f5f9;border-bottom:1px solid #e2e8f0}}
  .search input{{flex:1;box-sizing:border-box;padding:12px 14px;border:1px solid #cbd5e1;
    border-radius:10px;font-size:16px;background:#fff}}
  .search input:focus{{outline:none;border-color:#2563eb}}
  .qn{{font-size:12px;color:#64748b;white-space:nowrap}}
  .tw{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  /* 모바일: 표를 '카드 리스트'로 (한 종목 = 카드 한 장) */
  @media(max-width:640px){{
    header h1{{font-size:15px}} header p{{font-size:11px}}
    .chip{{padding:6px 11px;font-size:12px}}
    .tw{{overflow:visible}}
    thead{{display:none}}
    table,tbody,tr,td{{display:block;width:auto}}
    tr{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;
       margin:8px 12px;padding:6px 13px}}
    tr.b-transition{{border-left:4px solid #16a34a}}
    tr.b-uptrend{{border-left:4px solid #38bdf8}}
    tr.b-avoid{{border-left:4px solid #dc2626}}
    td{{display:flex;justify-content:space-between;gap:12px;align-items:center;
       text-align:right;border:0;padding:5px 0;font-size:13px}}
    td::before{{content:attr(data-label);color:#94a3b8;font-size:12px;
       font-weight:600;text-align:left}}
    td.nm{{display:block;text-align:left;border-bottom:1px solid #f1f5f9;
       padding-bottom:6px;margin-bottom:4px}}
    td.nm a{{font-size:16px}} td.nm .cd{{margin-left:6px}}
    td.nm .ko{{display:block;margin:2px 0 0;font-size:12px}}
    td.vd{{display:block;text-align:left;white-space:normal;font-size:12px;
       color:#475569;margin-top:5px;padding-top:6px;border-top:1px solid #f1f5f9}}
    td.vd::before{{display:block;margin-bottom:2px}}
    td[data-v="0"].stg,td[data-v=""].stg{{display:none}}
  }}
</style></head><body>
<header><h1>종목 스크리너 <span style="color:#38bdf8;font-size:13px">차트 신호 랭킹</span></h1>
<p>{n}종목 표시 · 헤더 클릭=정렬 · 칩=필터 · 종목명 클릭=상세 차트(일/주/월)</p></header>
<div class="prog">
  <div class="ptxt">📥 수집 진행 <b>{cached}/{uni}</b> ({pct}%) · 갱신 {updated}</div>
  <div class="pbarw"><div class="pfillw" style="width:{pct}%"></div></div>
</div>
<div class="search">
  <input id="q" type="search" inputmode="search" autocomplete="off"
    placeholder="🔍 종목 검색 (티커·영문명·한글명)" oninput="search(this.value)">
  <span id="qn" class="qn"></span>
</div>
<details class="legend"><summary>❔ 전환단계·신호가 무슨 뜻인가요? (판정 기준)</summary>
<div class="lc">
<b>전환단계</b>(하락→상승 전환이 얼마나 진행됐나, 클수록 강함):<br>
&nbsp;<b>④ 전환 확정 ⭐</b> — 하락추세선을 <b>거래량 동반 돌파</b> → 되눌림 후 <b>안착</b> →
 위로 <b>상승추세선</b>까지 형성. 셋 다 충족된 가장 강한 신호.<br>
&nbsp;<b>③ 돌파후 횡보(대기)</b> — 추세선은 넘었고 그 위에서 다지는 중. 상승추세선·거래량 확인되면 ④로.<br>
&nbsp;<b>② 갓 돌파(미확인)</b> — 막 넘었지만 되밀릴 수 있어 안착 미확인 → 관망.<br>
&nbsp;<b>① 임박</b> — 아직 추세선 아래지만 저항에 근접. 돌파하면 전환 시작.<br>
<b>신호</b>(종합점수 −100~+100): <b>🟢 강세</b> +50↑ 적극 · <b>🟢 관심</b> +20~50 ·
 <b>⚪ 중립</b> −20~+20 · <b>🔴 주의</b> −50~−20 · <b>🔴 공포</b> −50↓.<br>
<b>위쪽 칩(그룹)</b>은 <b>점수와 다른 '추세 상태' 축</b>이에요:
 <b>🟢전환후보</b>=하락→상승 전환 진행(①~④) · <b>📈상승추세</b>=전환은 끝났고 이미 강세 ·
 <b>⚪관망</b>=방향 미정(중립) · <b>🔴회피</b>=하락추세.<br>
<span style="color:#94a3b8">※ 그래서 "신호 🟢강세"인데 "전환후보"가 아닐 수 있어요(점수는 강한데 전환 셋업은 아닌 경우). 칩은 추세 상태, 신호는 점수 — 다른 축이에요.</span>
</div></details>
<div class="bar">
  <button class="chip rec-chip on" onclick="fltRec()">⭐ 진입 추천 {rcount}</button>
  <button class="chip" onclick="flt('all')">전체</button>
  {chips}
  <a class="chip" href="guide.html" style="margin-left:auto;text-decoration:none">📘 매매 가이드</a>
  <a class="chip" href="lookup.html" style="background:#0f172a;color:#fff;border-color:#0f172a;text-decoration:none">➕ 티커 즉석 조회</a>
</div>
<div class="tw"><table id="t"><thead><tr>
  <th onclick="srt(0,false)">신호</th>
  <th onclick="srt(1,false)">종목</th>
  <th onclick="srt(2,true)">전환단계▼</th>
  <th onclick="srt(3,false)">추세</th>
  <th onclick="srt(4,true)">점수</th>
  <th onclick="srt(5,false)">시장</th>
  <th onclick="srt(6,true)">RS</th>
  <th onclick="srt(7,true)">신고가</th>
  <th onclick="srt(8,false)">판정</th>
</tr></thead><tbody id="tb">{rows}</tbody></table></div>
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
  // ⭐ 진입 추천: 체크리스트 4/6 이상만 표시(가장 손쉬운 후보 추리기)
  function fltRec(){{
    setOn(typeof event!=='undefined'?event.target:document.querySelector('.rec-chip'));
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.rec||'0')>=4)?'':'none';
    }});
  }}
  // 검색: 티커·영문명·한글명으로 필터(칩 필터는 해제하고 전체에서 찾음)
  function search(v){{
    v=(v||'').trim().toLowerCase();
    var qn=document.getElementById('qn');
    if(!v){{ setOn(document.querySelectorAll('.chip')[0]);
      tb.querySelectorAll('tr').forEach(function(r){{r.style.display='';}});
      qn.textContent=''; return; }}
    setOn(null);
    var hit=0;
    tb.querySelectorAll('tr').forEach(function(r){{
      var t=r.querySelector('.nm').textContent.toLowerCase();
      var ok=t.indexOf(v)>=0; r.style.display=ok?'':'none'; if(ok)hit++;
    }});
    qn.textContent=hit+'개';
  }}
  // 첫 화면: ⭐진입 추천(체크리스트 4/6+)만 우선. 없으면 전환후보, 그래도 없으면 전체.
  window.addEventListener('load',function(){{
    var rec=0,trans=0;
    tb.querySelectorAll('tr').forEach(function(r){{
      if(parseInt(r.dataset.rec||'0')>=4)rec++;
      if(r.dataset.bucket==='transition')trans++;}});
    if(rec){{fltRec();}}
    else if(trans){{setOn(document.querySelector('.rec-chip'));
      document.querySelector('.rec-chip').classList.remove('on');
      var tc=Array.prototype.find.call(document.querySelectorAll('.chip'),
        function(c){{return c.textContent.indexOf('전환후보')>=0;}});
      setOn(tc);
      tb.querySelectorAll('tr').forEach(function(r){{
        r.style.display=(r.dataset.bucket==='transition')?'':'none';}});}}
    else{{setOn(document.querySelector('.chip:nth-child(2)'));
      tb.querySelectorAll('tr').forEach(function(r){{r.style.display='';}});}}
  }});
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
  .hist-item{display:flex;align-items:center;gap:10px;padding:9px 2px;
    border-bottom:1px solid #f1f5f9;font-size:14px}
  .hist-item:last-child{border-bottom:0}
  .hist-item a.t{font-weight:700;color:#1d4ed8;text-decoration:none}
  .hist-item .s{font-size:12px;color:#64748b;margin-left:auto}
  .hist-item .x{color:#cbd5e1;cursor:pointer;font-size:18px;line-height:1}
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
  <div class="card" id="histcard" style="display:none">
    <div style="font-weight:700;margin-bottom:6px">📋 즉석조회한 종목 <span id="histn" style="color:#94a3b8;font-weight:400;font-size:13px"></span></div>
    <div id="hist"></div>
    <div style="text-align:right;margin-top:6px"><span id="histclear" onclick="clearHist()" style="font-size:12px;color:#94a3b8;cursor:pointer">목록 비우기</span></div>
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
  // 즉석조회 누적 이력(브라우저 저장) — 조회한 종목이 목록으로 쌓이고, 수집중은 ⏳
  var HIST=[]; try{HIST=JSON.parse(localStorage.getItem('lookup_hist')||'[]');}catch(e){HIST=[];}
  function saveHist(){localStorage.setItem('lookup_hist',JSON.stringify(HIST.slice(0,40)));}
  function pushHist(t){HIST=HIST.filter(function(h){return h.t!==t;});
    HIST.unshift({t:t,done:false,ts:Date.now()});saveHist();renderHist();}
  function markDone(t,ok){for(var i=0;i<HIST.length;i++){if(HIST[i].t===t){HIST[i].done=true;HIST[i].ok=ok!==false;}}saveHist();renderHist();}
  function delHist(t){HIST=HIST.filter(function(h){return h.t!==t;});saveHist();renderHist();}
  function clearHist(){if(confirm('즉석조회 목록을 비울까요?')){HIST=[];saveHist();renderHist();}}
  function renderHist(){
    var c=document.getElementById('histcard'),h=document.getElementById('hist');
    document.getElementById('histn').textContent=HIST.length?'('+HIST.length+')':'';
    if(!HIST.length){c.style.display='none';return;} c.style.display='';
    h.innerHTML=HIST.map(function(x){
      var s=x.done?(x.ok===false?'⚠️ 실패':'✅ 완료'):'⏳ 수집중';
      return '<div class="hist-item"><a class="t" href="stocks/'+x.t+'.html">'+x.t+'</a>'
        +'<span class="s">'+s+' · <a href="stocks/'+x.t+'.html" style="color:#1d4ed8">상세</a></span>'
        +'<span class="x" title="삭제" onclick="delHist(\\''+x.t+'\\')">&times;</span></div>';
    }).join('');
  }
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
    // 이미 진행 중인 같은 종목이면 중복 실행하지 말고 이어서 추적(게이지 초기화 방지)
    var act=loadActive();
    if(act&&act.t===t){
      show('⏳ <b>'+t+'</b> 이미 수집 중 — 이어서 확인합니다.'); bar(45,null,true);
      track(p,t); return;
    }
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
      saveActive(t); pushHist(t);   // 진행 상태 저장 + 이력에 추가(수집중)
      show('✅ <b>'+t+'</b> 수집 시작! 1~2분 후 결과를 봅니다...'); bar(30,null,true);
      setTimeout(function(){startTrack(p,t);},6000);
    }else{
      var e=await r.text();
      show('❌ 실패 ('+r.status+'). 토큰/권한 확인.<br><small>'+e.slice(0,160)+'</small>'); bar(100,'#dc2626',false);
    }
  }
  // 추적 루프는 한 번에 하나만(모바일 백그라운드 복귀 시 중복 방지) — 세대 토큰으로 관리
  var TGEN=0;
  function startTrack(p,t){ TGEN++; track(p,t,TGEN); }
  async function track(p,t,gen){
    if(gen!==TGEN)return;            // 더 최신 추적이 시작됐으면 이 루프는 종료
    try{
      var r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/runs?per_page=1',
        {headers:{'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json'}});
      var run=(await r.json()).workflow_runs[0];
      if(!run){show('실행 대기 중...');bar(35,null,true);setTimeout(function(){track(p,t,gen);},5000);return;}
      if(run.status!=='completed'){
        show('⏳ <b>'+t+'</b> 수집·분석 중... ('+run.status+') · <a href="'+run.html_url+'" target=_blank>로그</a>');
        bar(run.status==='queued'?45:75,null,true);
        setTimeout(function(){track(p,t,gen);},5000);
      }else{
        TGEN++;          // 완료 → 진행 중인 루프 모두 무효화
        clearActive();   // 끝났으니 진행 상태 해제
        var ok=run.conclusion==='success';
        markDone(t,ok);  // 이력 상태 갱신(완료/실패)
        bar(100,ok?'#16a34a':'#dc2626',false);
        show((ok?'✅ 완료! <b>'+t+'</b> 스크리너에 추가됨':'⚠️ '+run.conclusion+' <b>'+t+'</b>')+'<br>'
          +'<a href="index.html">스크리너에서 보기 &rarr;</a> · '
          +'<a href="stocks/'+t+'.html">상세 차트 &rarr;</a> · '
          +'<a href="'+run.html_url+'" target=_blank>로그</a>'
          +'<div class=hint>안 보이면 새로고침(배포 반영 1~2분).</div>');
      }
    }catch(e){show('상태 조회 오류: '+e);}
  }
  // 진행 상태를 브라우저에 저장 → 새로고침/재방문해도 게이지·상태 유지(초기화 방지)
  function saveActive(t){localStorage.setItem('lookup_active',JSON.stringify({t:t,ts:Date.now()}));}
  function clearActive(){localStorage.removeItem('lookup_active');}
  function loadActive(){
    var a=localStorage.getItem('lookup_active'); if(!a)return null;
    try{a=JSON.parse(a);}catch(e){clearActive();return null;}
    if(Date.now()-a.ts>20*60*1000){clearActive();return null;}  // 20분 지나면 만료
    return a;
  }
  tk.addEventListener('keydown',function(e){if(e.key==='Enter')run();});
  // 진행 중이던 조회가 있으면 이어서 추적(모바일은 백그라운드에서 타이머가 멈추므로
  // 화면 복귀(visibilitychange/focus) 때마다 다시 살린다 → 게이지가 안 뜨던 문제 해결)
  function resume(){
    var a=loadActive(); if(!a)return;
    var p=localStorage.getItem('ghpat')||'';
    if(tk)tk.value=a.t;
    show('⏳ <b>'+a.t+'</b> 진행 확인 중...'); bar(40,null,true);
    if(p)startTrack(p,a.t);
    else show('토큰을 입력하면 <b>'+a.t+'</b> 진행 상태를 이어볼 수 있어요.');
  }
  window.addEventListener('load',function(){renderHist();resume();});
  window.addEventListener('focus',resume);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)resume();});
</script></body></html>"""


_GUIDE_HTML = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>매매 가이드</title>
<style>
  body{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b;line-height:1.7}
  header{background:#0f172a;color:#fff;padding:14px 20px}
  header a{color:#7dd3fc;font-size:13px;text-decoration:none}
  header h1{margin:5px 0 0;font-size:18px}
  .wrap{max-width:760px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px 18px;margin-bottom:14px}
  h2{font-size:16px;margin:2px 0 10px;color:#0f172a}
  h2 .em{color:#2563eb}
  table{width:100%;border-collapse:collapse;font-size:13.5px;margin:6px 0}
  th,td{border-bottom:1px solid #eef2f7;padding:7px 8px;text-align:left;vertical-align:top}
  th{color:#64748b;font-size:12px;background:#f8fafc}
  .good{color:#16a34a;font-weight:700} .bad{color:#dc2626;font-weight:700}
  .warn{color:#d97706;font-weight:700}
  ul{margin:6px 0;padding-left:20px} li{margin:3px 0}
  .pill{display:inline-block;background:#eff6ff;color:#1d4ed8;border-radius:999px;
    padding:1px 9px;font-size:12px;font-weight:600;margin-right:4px}
  .note{font-size:12.5px;color:#64748b}
  .big{background:#0f172a;color:#e2e8f0;border:0}
  .big b{color:#7dd3fc}
</style></head><body>
<header><a href="index.html">&larr; 스크리너</a>
<h1>📘 매매 가이드 — 이 차트를 어떻게 쓰나</h1></header>
<div class="wrap">

<div class="card big">
<b>핵심 철학:</b> 이건 <b>"강세 추격기"가 아니라 "하락→상승 전환 포착기"</b>입니다.
신호가 🟢강세(+50↑)일 때가 흔히 제일 안 좋은 자리(이미 다 오른 추격권)예요.
<br>한 줄: <b>남들이 무서워서 못 살 때(전환 초입) 사고, 모두가 강세라 외칠 때(추격) 사지 않는다.</b>
<br><span class="note">적중률은 절반 안팎. 돈은 "손익비"로 법니다 — 틀리면 −1R, 맞으면 +2~3R.
이 도구의 가장 큰 가치는 <b>크게 잃을 자리를 걸러주는 것</b>입니다.</span>
</div>

<div class="card">
<h2>컬럼별 <span class="em">좋은 값</span></h2>
<table>
<tr><th>컬럼</th><th>뭘 보나</th><th>좋은 신호</th></tr>
<tr><td>전환단계</td><td>하락→상승 전환 진행도</td><td class="good">③ 돌파후 횡보 · ④ 전환 확정 ⭐</td></tr>
<tr><td>신호</td><td>종합점수 −100~+100</td><td>🟢관심(+20~50)이 스윗스팟. 🟢강세는 추격 주의</td></tr>
<tr><td>RS</td><td>지수 대비 상대강도</td><td class="good">+(플러스) & 상승 중</td></tr>
<tr><td>신고가</td><td>52주 고가 대비 %</td><td>−20%~−3%(근접하되 안 붙음). <span class="bad">0%는 추격권</span></td></tr>
<tr><td>시장</td><td>지수 방향</td><td class="good">상승</td> </tr>
</table>
</div>

<div class="card">
<h2>전환단계의 <span class="em">뜻과 기준</span></h2>
<ul>
<li><b>④ 전환 확정 ⭐</b> — ①하락추세선을 <b>거래량 동반 돌파</b> → ②되눌림 후 <b>안착</b> → ③위로 <b>상승추세선</b>까지 형성. 셋 다 충족된 가장 강한 신호.</li>
<li><b>③ 돌파후 횡보(대기)</b> — 추세선은 넘었고 위에서 다지는 중. 상승추세선·거래량 확인되면 ④로.</li>
<li><b>② 갓 돌파(미확인)</b> — 막 넘었지만 되밀릴 수 있어 안착 미확인 → 관망.</li>
<li><b>① 임박</b> — 아직 추세선 아래지만 저항 근접. 돌파하면 전환 시작.</li>
</ul>
</div>

<div class="card">
<h2>✅ "내가 산다면" <span class="em">체크리스트</span></h2>
<ul>
<li><span class="pill">1</span>전환단계 <b>③ 또는 ④</b></li>
<li><span class="pill">2</span>시장 = <b>상승</b> (지수가 받쳐줄 때만)</li>
<li><span class="pill">3</span>RS <b>플러스</b> (시장보다 강함)</li>
<li><span class="pill">4</span>신고가 <b>−20%~−3%</b> (고가 근접, 아직 안 붙음)</li>
<li><span class="pill">5</span><b>🔺추격주의 없음</b> (과열·과대이격 아님)</li>
<li><span class="pill">6</span>신호 <b>🟢관심 이상</b></li>
</ul>
<span class="note">1·2·3 충족 = 진지하게 검토 · 1~6 다 충족 = 1순위 매수 후보.</span>
</div>

<div class="card">
<h2>"중립 vs 강세" <span class="em">어디서 사나</span></h2>
<ul>
<li><span class="good">⚪중립 + 전환단계 ③④</span> → 👍 가장 좋은 자리(선취매 구간).</li>
<li><span class="bad">🟢강세 + 전환 0 + 신고가 0% + 추격주의</span> → ⚠️ 추격. 눌림 대기.</li>
<li>⚪중립 + 전환 0 → 관망(살 이유 없음).</li>
<li><span class="bad">🔴주의/공포 · 🔴하락추세</span> → 매수 금지. 보유 중이면 손절 점검.</li>
</ul>
</div>

<div class="card">
<h2>진입 · 손절 · 목표 <span class="em">(리스크 관리가 본체)</span></h2>
<ul>
<li><b>진입</b>: 차트 "진입선". 고점권이면 <b>저항 돌파 확인 후</b>, 아니면 현재가 분할.</li>
<li><b>손절</b>: "손절선"(진입−2×ATR) <b class="bad">밑으로 종가 마감 시 무조건 정리.</b> ← 안 지키면 도구 의미 없음.</li>
<li><b>목표</b>: "목표선"(손익비 1:2). 절반 도달 시 일부 익절 + 손절 올리기.</li>
<li><b>비중</b>: 한 종목 손실이 전체의 <b>1~2%</b>를 넘지 않게.</li>
</ul>
</div>

<div class="card">
<h2>피해야 할 것 · <span class="em">한계</span></h2>
<ul>
<li class="bad">🔺추격주의 종목 신규 진입 / 🔴하락추세 "쌀 것 같아서" 받기 / 시장 하락에 매수.</li>
<li><span class="warn">📅어닝 D-3 이내</span> 종목은 갭 리스크로 신규 진입 회피.</li>
<li class="note">차트만 보므로 실적·뉴스·갭은 못 봅니다. 정밀한 분 단위 진입은 증권사 앱으로.
 백테스트 엣지는 작고(+0.1R 수준) 생존편향이 있어 "예측기"가 아닌 "리스크 도구"로 쓰세요.</li>
</ul>
</div>

<p class="note" style="text-align:center">이 페이지는 일반적 가이드이며 투자 권유가 아닙니다. 최종 판단·책임은 본인에게 있습니다.</p>
</div></body></html>"""
