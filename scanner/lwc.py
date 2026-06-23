"""TradingView lightweight-charts 기반 상세 차트 (모바일 터치 최적).

plotly 대비 핀치/팬이 부드럽고 45KB로 가볍다. 일/주/월 캔들 + 거래량 + 이평선
+ 수평선(저항/방어선/POC/52주고가/진입/손절/목표/지지저항/피보) + 추세선(연장).
지표 토글·타임프레임 전환 포함.
"""
from __future__ import annotations

import html
import json

import pandas as pd

import config
from scanner import card, chart
from scanner import trendlines as tlmod

LWC_CDN = ("https://unpkg.com/lightweight-charts@4.2.0/dist/"
           "lightweight-charts.standalone.production.js")
_LOOKBACK = {"H": 320, "D": 180, "W": 160, "M": 120}
_PROJECT = {"H": 16, "D": 12, "W": 10, "M": 6}
_TF_LABELS = [("H", "시간봉"), ("D", "일봉"), ("W", "주봉"), ("M", "월봉")]
_MA_COLORS = ["#f59e0b", "#8b5cf6", "#64748b"]
# LWC에서 다루는 토글 그룹(매물대 히스토그램은 LWC 미지원 → 제외)
GROUPS = [("ma", "이평선", True), ("level", "핵심선", True),
          ("trade", "매매선", True), ("trend", "추세선", True),
          ("sr", "지지/저항", False), ("fib", "피보나치", False)]
_DEFAULT_ON = {k for k, _l, on in GROUPS if on}


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _tfn(tf):
    """타임프레임별 LWC 시간 인코더. 시간봉은 epoch초(시각 구분), 그 외는 날짜문자열."""
    if tf == "H":
        return lambda t: int(pd.Timestamp(t).timestamp())
    return _d


def _payload(result: dict, frames: dict, tf: str) -> dict:
    full = frames[tf]
    lb = _LOOKBACK.get(tf, 180)
    d = full.iloc[-lb:]
    T = _tfn(tf)
    up, dn = chart.C["up"], chart.C["down"]
    candles = [{"time": T(t), "open": float(o), "high": float(h),
                "low": float(l), "close": float(c)}
               for t, o, h, l, c in zip(d.index, d["Open"], d["High"],
                                        d["Low"], d["Close"])]
    vol = [{"time": T(t), "value": float(v), "color": (up if c >= o else dn)}
           for t, v, c, o in zip(d.index, d["Volume"], d["Close"], d["Open"])]
    mas = []
    for i, p in enumerate(config.MA_PERIODS[tf][1:]):
        s = full["Close"].rolling(p).mean().iloc[-lb:]
        pts = [{"time": T(t), "value": float(v)}
               for t, v in zip(s.index, s) if pd.notna(v)]
        mas.append({"color": _MA_COLORS[i % 3], "data": pts})

    sr, risk = result["sr"], result["risk"]
    va = result["levels"]["value_area"]
    price = sr["price"]
    lines = []

    def L(p, title, color, group, dash=0):
        lines.append({"price": round(float(p), 4), "title": title,
                      "color": color, "group": group, "dash": dash})

    L(sr["box_high"], "저항", chart.C["resist"], "level")
    L(sr["defense"], "방어선", chart.C["defense"], "level")
    L(va["poc"], "POC", chart.C["poc"], "level", 1)
    nh = result.get("newhigh", {}).get("high52")
    if nh:
        L(nh, "52주고가", chart.C["target"], "level", 1)
    L(result["entry"], "진입", chart.C["entry"], "trade", 2)
    L(risk["stop"], "손절", chart.C["stop"], "trade", 2)
    L(risk["target"], "목표", chart.C["target"], "trade", 2)
    for lv in result["levels"]["strong"][:6]:
        col = chart.C["resist"] if lv["price"] > price else chart.C["sup"]
        L(lv["price"], "", col, "sr", 1)
    for fl in result["levels"]["fib"]["levels"]:
        L(fl["price"], f"피보{fl['ratio']:.3f}", chart.C["fib"], "fib", 1)

    # 추세선(사선) + 미래 연장 — 라인 시리즈
    tl = result["trendline"] if tf == "D" else tlmod.detect(full)
    dates = list(d.index)
    avg_delta = (dates[-1] - dates[0]) / max(1, len(dates) - 1)
    proj = _PROJECT.get(tf, 12)
    trend = []
    for which, color in (("down", chart.C["resist"]), ("up", chart.C["target"])):
        seg = tl.get(which) if tl else None
        if not seg or "x0" not in seg:
            continue
        x1 = pd.Timestamp(seg["x1"])
        trend.append({"color": color, "dash": 0, "data": [
            {"time": T(seg["x0"]), "value": float(seg["y0"])},
            {"time": T(x1), "value": float(seg["y1"])}]})
        xf = x1 + avg_delta * proj
        yf = float(seg["y1"]) + float(seg.get("slope", 0)) * proj
        trend.append({"color": color, "dash": 2, "data": [
            {"time": T(x1), "value": float(seg["y1"])},
            {"time": T(xf), "value": yf}]})

    return {"candles": candles, "vol": vol, "mas": mas,
            "lines": lines, "trend": trend}


def detail(result: dict, frames: dict) -> str:
    """lightweight-charts 상세 페이지 HTML. frames에 'H'(시간봉)가 있으면 탭 추가."""
    code = result["code"]
    # 표시할 타임프레임: 항상 일/주/월, 시간봉은 데이터 있을 때만(없으면 탭 미표시)
    default_tf = "D"
    labels = dict(_TF_LABELS)
    # 프레임별 페이로드 생성 — 한 프레임이 깨져도(예: 시간봉 데이터 이상) 그 탭만
    # 건너뛰고 나머지는 정상 렌더(빌드 전체가 죽지 않도록 방어).
    data = {}
    for tf, _lab in _TF_LABELS:
        if tf not in frames:
            continue
        try:
            data[tf] = _payload(result, frames, tf)
        except Exception:
            if tf == default_tf:
                raise            # 일봉은 필수 — 실패하면 상위에서 처리
    tfs = [tf for tf, _lab in _TF_LABELS if tf in data]
    tfbtns = "".join(
        f'<button class="tfbtn{" active" if tf == default_tf else ""}" '
        f'id="tfb-{tf}" onclick="sw(\'{tf}\')">{labels[tf]}</button>' for tf in tfs)
    _hide = ' style="display:none"'
    ccs = "".join(
        f'<div class="cc" id="c-{tf}"{"" if tf == default_tf else _hide}></div>'
        for tf in tfs)
    toggles = "".join(
        f'<button class="toggle{" on" if on else ""}" data-group="{k}" '
        f'onclick="tg(this)">{html.escape(lab)}</button>'
        for k, lab, on in GROUPS)
    default_on = json.dumps({k: (k in _DEFAULT_ON) for k, _l, _o in GROUPS})
    precision = 0 if result["ccy"] == "KRW" else 2
    title = (f'{html.escape(result["name"])} {html.escape(code)} · '
             f'{html.escape(result["gauge"])} 정규화 {result["norm"]:+.0f}')
    card_txt = card.render(result).split("\n용어:")[0]
    return _TMPL.format(
        title=title, cdn=LWC_CDN, toggles=toggles, tfbtns=tfbtns, ccs=ccs,
        tfs=json.dumps(tfs), first=default_tf,
        data=json.dumps(data), default_on=default_on, precision=precision,
        verdict=html.escape(result.get("verdict", "")),
        card=html.escape(card_txt))


_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="{cdn}"></script>
<style>
  body{{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}}
  header{{background:#0f172a;color:#fff;padding:12px 16px}}
  header a{{color:#7dd3fc;font-size:13px;text-decoration:none}}
  header h1{{margin:4px 0 0;font-size:16px}} .vt{{font-size:12px;color:#94a3b8;margin-top:3px}}
  .wrap{{padding:12px}}
  .bar{{display:flex;flex-wrap:wrap;gap:7px;align-items:center;background:#fff;
    border:1px solid #e2e8f0;border-radius:10px;padding:9px 10px;margin-bottom:10px}}
  .toggle{{border:1px solid #cbd5e1;background:#f8fafc;color:#64748b;border-radius:999px;
    padding:5px 12px;cursor:pointer;font-size:12px}}
  .toggle.on{{background:#2563eb;border-color:#2563eb;color:#fff}}
  .tfbtn{{border:1px solid #cbd5e1;background:#fff;color:#475569;border-radius:8px;
    padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600}}
  .tfbtn.active{{background:#0f172a;border-color:#0f172a;color:#fff}}
  .chart{{position:relative;background:#fff;border:1px solid #e2e8f0;border-radius:10px;
    padding:4px}}
  .cc{{width:100%;height:62vh;min-height:360px}}
  .card{{background:#0f172a;color:#e2e8f0;padding:14px 16px;border-radius:10px;
    font-size:12px;line-height:1.5;white-space:pre;overflow:auto;margin-top:12px;
    font-family:'D2Coding','Menlo','Consolas',monospace}}
  details>summary{{cursor:pointer;font-weight:600;color:#334155;padding:8px 12px;
    background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin-top:12px}}
  .lc{{background:#fff;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px;
    padding:10px 14px;font-size:12.5px;color:#475569;line-height:1.85}}
  .lc b{{color:#0f172a}}
  .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:2px;
    vertical-align:middle}}
  @media(max-width:640px){{.wrap{{padding:8px}}header h1{{font-size:14px}}
    .toggle,.tfbtn{{padding:5px 10px;font-size:12px}}.card{{font-size:11px}}.lc{{font-size:11.5px}}}}
</style></head><body>
<header><a href="../index.html">← 스크리너 목록</a><h1>{title}</h1>
<div class="vt">{verdict}</div></header>
<div class="wrap">
  <div class="bar"><span style="font-size:12px;color:#64748b">지표</span>{toggles}</div>
  <div class="bar">{tfbtns}
    <span style="font-size:11px;color:#64748b;margin-left:6px">핀치=확대/축소 · 드래그=이동</span></div>
  <div class="chart">{ccs}</div>
  <details class="lines"><summary>❔ 차트의 선들이 무슨 뜻인가요? (진입·지지/저항)</summary>
  <div class="lc">
  <b>매매선</b>(파란 토글):<br>
  &nbsp;<span class="dot" style="background:#2563eb"></span><b>진입</b> — <b>"여기서 사라"는 매수 기준가</b>.
   고점권이면 <b>저항 돌파가</b>(돌파해야 매수), 그 외엔 현재가. ※저항·지지가 진입 기준으로 잡힌 게
   아니라, 가격 구조로 저항·지지를 먼저 잡고 거기서 진입가를 끌어낸 것.<br>
  &nbsp;<span class="dot" style="background:#ef4444"></span><b>손절</b> — 진입−2×ATR. 종가로 이 아래면 정리.<br>
  &nbsp;<span class="dot" style="background:#16a34a"></span><b>목표</b> — 손익비 1:2 목표가.<br>
  <b>핵심선</b>(파란 토글):<br>
  &nbsp;<span class="dot" style="background:#ef4444"></span><b>저항</b> — 박스 상단·매도벽 ·
  &nbsp;<span class="dot" style="background:#f59e0b"></span><b>방어선</b> — 깨지면 안 되는 핵심 지지 ·
  &nbsp;<span class="dot" style="background:#8b5cf6"></span><b>POC</b> — 거래 최다 가격대 ·
  &nbsp;<span class="dot" style="background:#16a34a"></span><b>52주고가</b><br>
  <b>지지/저항</b>(기본 꺼짐): 과거 스윙 고·저점에서 만든 보조 레벨(가격 위는 저항=빨강, 아래는 지지=초록).
   핵심선보다 <b>얇은 점선</b>이라 구분돼요. 너무 많으면 토글로 끄세요.<br>
  <b>피보나치</b>(기본 꺼짐): 고점→저점 되돌림 비율(38.2·50·61.8%) 회색 선.<br>
  <b>추세선</b>(사선): 빨강=하락추세선(고점 잇기) · 초록=상승추세선(저점 잇기), 점선은 미래 연장.
  </div></details>
  <details><summary>📋 상세 신호 카드</summary><pre class="card">{card}</pre></details>
</div>
<script>
  var DATA={data}, STATE={default_on}, PREC={precision}, TFS={tfs}, CUR='{first}', CH={{}};
  var LS=LightweightCharts.LineStyle;
  var STY=[LS.Solid,LS.Dotted,LS.Dashed];
  function build(tf){{
    var el=document.getElementById('c-'+tf);
    var c=LightweightCharts.createChart(el,{{
      width:el.clientWidth,height:el.clientHeight,
      layout:{{background:{{color:'#fff'}},textColor:'#334155',fontSize:11}},
      grid:{{vertLines:{{color:'#f1f5f9'}},horzLines:{{color:'#f1f5f9'}}}},
      rightPriceScale:{{borderColor:'#e2e8f0'}},
      timeScale:{{borderColor:'#e2e8f0',timeVisible:tf==='H',secondsVisible:false}},
      crosshair:{{mode:LightweightCharts.CrosshairMode.Normal}},
      handleScroll:true,handleScale:true
    }});
    var pf={{type:'price',precision:PREC,minMove:PREC?0.01:1}};
    var candle=c.addCandlestickSeries({{upColor:'#16a34a',downColor:'#ef4444',
      borderVisible:false,wickUpColor:'#16a34a',wickDownColor:'#ef4444',priceFormat:pf}});
    candle.setData(DATA[tf].candles);
    var vol=c.addHistogramSeries({{priceFormat:{{type:'volume'}},priceScaleId:'v'}});
    vol.setData(DATA[tf].vol);
    c.priceScale('v').applyOptions({{scaleMargins:{{top:0.84,bottom:0}}}});
    var reg={{ma:[],trend:[],lines:{{level:[],trade:[],sr:[],fib:[]}}}};
    DATA[tf].mas.forEach(function(m){{var s=c.addLineSeries({{color:m.color,lineWidth:1,
      priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}});
      s.setData(m.data);reg.ma.push(s);}});
    DATA[tf].trend.forEach(function(t){{var s=c.addLineSeries({{color:t.color,lineWidth:2,
      lineStyle:STY[t.dash]||0,priceLineVisible:false,lastValueVisible:false,
      crosshairMarkerVisible:false}});s.setData(t.data);reg.trend.push(s);}});
    DATA[tf].lines.forEach(function(l){{
      var mk=function(){{return candle.createPriceLine({{price:l.price,color:l.color,
        lineWidth:1,lineStyle:STY[l.dash]||0,axisLabelVisible:true,title:l.title}});}};
      reg.lines[l.group].push({{mk:mk,pl:null}});
    }});
    CH[tf]={{chart:c,candle:candle,reg:reg}};
    apply(tf); c.timeScale().fitContent();
    new ResizeObserver(function(){{c.applyOptions({{width:el.clientWidth,height:el.clientHeight}});}}).observe(el);
  }}
  function apply(tf){{
    var o=CH[tf]; if(!o)return; var r=o.reg;
    r.ma.forEach(function(s){{s.applyOptions({{visible:STATE.ma}});}});
    r.trend.forEach(function(s){{s.applyOptions({{visible:STATE.trend}});}});
    ['level','trade','sr','fib'].forEach(function(g){{
      r.lines[g].forEach(function(x){{
        if(STATE[g]&&!x.pl){{x.pl=x.mk();}}
        else if(!STATE[g]&&x.pl){{o.candle.removePriceLine(x.pl);x.pl=null;}}
      }});
    }});
  }}
  function tg(btn){{var g=btn.dataset.group;STATE[g]=!STATE[g];
    btn.classList.toggle('on',STATE[g]);apply(CUR);}}
  function sw(tf){{CUR=tf;
    TFS.forEach(function(t){{
      document.getElementById('c-'+t).style.display=(t===tf)?'block':'none';
      document.getElementById('tfb-'+t).classList.toggle('active',t===tf);}});
    if(!CH[tf])build(tf); else {{CH[tf].chart.timeScale().fitContent();}}
  }}
  window.addEventListener('load',function(){{build(CUR);}});
</script></body></html>"""
