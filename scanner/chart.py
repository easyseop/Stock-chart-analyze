"""신호 차트 시각화 (plotly) — v1.6.

캔들 + 박스권/방어선/POC/이평선 + 진입·손절·목표 선 + 거래량.
모든 보조선을 '그룹(legendgroup)'으로 묶어 대시보드에서 켜고 끌 수 있다.
호버는 'closest'(가장 가까운 한 요소만)로 단순화 — 정보가 쏟아지지 않게.
HTML(인터랙티브) 저장이 기본, PNG 저장은 옵션(kaleido+chrome 필요).
"""
from __future__ import annotations

import os

import pandas as pd

import config
import glossary

# 색상 팔레트
C = {
    "ma20": "#f59e0b", "ma60": "#8b5cf6", "ma120": "#64748b",
    "box": "#3b82f6", "resist": "#ef4444", "defense": "#dc2626",
    "poc": "#0ea5e9", "entry": "#2563eb", "stop": "#ef4444",
    "target": "#16a34a", "up": "#16a34a", "down": "#ef4444",
    "sup": "#0d9488", "fib": "#a855f7", "avg": "#b45309", "prof": "#0ea5e9",
}

# 토글 그룹(대시보드 버튼과 1:1). (key, 라벨, 기본표시여부)
GROUPS = [
    ("ma",     "이평선",    True),
    ("level",  "핵심선",    True),   # 박스상단·방어선·POC
    ("trade",  "매매선",    True),   # 진입·손절·목표
    ("trend",  "추세선",    True),
    ("sr",     "지지/저항", False),  # 스윙 군집(강도선)
    ("fib",    "피보나치",  False),
    ("supply", "매물대",    False),  # 분포 + 추정평단
]
_DEFAULT_ON = {k for k, _, on in GROUPS if on}


def _hline(fig, x0, x1, y, label, color, group, term=None, dash="solid",
           width=1.4, show_label=True, hover=True, vfmt=",.2f"):
    """수평선을 '토글 가능한 trace'로 추가. (add_hline 대신 사용)

    라벨은 우측 끝점 텍스트로 붙여 선과 함께 켜고 꺼진다.
    hover 시 그 선의 '가격'을 같이 보여준다.
    """
    import plotly.graph_objects as go

    visible = True if group in _DEFAULT_ON else False
    desc = glossary.lookup(term) if term else ""
    htext = f"<b>{label}</b><br>가격 {format(y, vfmt)}" + (f"<br>{desc}" if desc else "")
    fig.add_trace(go.Scatter(
        x=[x0, x1], y=[y, y],
        mode="lines+text" if show_label else "lines",
        line=dict(color=color, dash=dash, width=width),
        text=[None, " " + label] if show_label else None,
        textposition="middle right",
        textfont=dict(color=color, size=10),
        cliponaxis=False,
        legendgroup=group, name=label, visible=visible,
        hovertext=htext if hover else None,
        hoverinfo="text" if hover else "skip",
        showlegend=False), row=1, col=1)


def _trendline(fig, tl: dict, which: str, color: str, avg_delta=None, project=0):
    """추세선(사선) 그리기 + 미래 연장(점선).

    실선=과거 피벗→현재, 점선=현재→미래 N봉(추세 지속 시 선이 갈 자리).
    트레이딩 단말처럼 '앞으로 추세선이 어디서 만날지'를 보여준다.
    """
    import plotly.graph_objects as go

    seg = tl.get(which)
    if not seg or "x0" not in seg:
        return
    label = "하락추세선" if which == "down" else "상승추세선"
    visible = True if "trend" in _DEFAULT_ON else False
    x_now, y_now = seg["x1"], seg["y1"]
    # 실선: 과거 피벗 → 현재
    fig.add_trace(go.Scatter(
        x=[seg["x0"], x_now], y=[seg["y0"], y_now], mode="lines",
        line=dict(color=color, width=2, dash="longdash"),
        legendgroup="trend", name=label, visible=visible,
        hovertext=f"{label}<br>{glossary.lookup('추세선')}",
        hoverinfo="text", showlegend=False), row=1, col=1)
    # 점선: 현재 → 미래 연장 (추세 지속 시 예상 경로)
    if project and avg_delta is not None and "slope" in seg:
        xf = x_now + avg_delta * project
        yf = y_now + seg["slope"] * project
        fig.add_trace(go.Scatter(
            x=[x_now, xf], y=[y_now, yf], mode="lines",
            line=dict(color=color, width=1.3, dash="dot"),
            legendgroup="trend", name=f"{label} 연장", visible=visible,
            hovertext=f"{label} 미래 연장<br>추세 지속 시 선이 갈 자리(예상)",
            hoverinfo="text", showlegend=False), row=1, col=1)


# 타임프레임별 표시 봉 수 · 미래연장 봉 수 · MA 색
_LOOKBACK = {"D": 140, "W": 120, "M": 96}
_PROJECT = {"D": 12, "W": 10, "M": 6}
_TF_LABEL = {"D": "일봉", "W": "주봉", "M": "월봉"}
_MA_COLORS = ["#f59e0b", "#8b5cf6", "#64748b", "#0891b2"]


def build_figure(result: dict, frames: dict, tf: str = "D", lookback: int = None):
    """analyze() 결과 + 프레임 → plotly Figure (타임프레임 tf='D'|'W'|'M').

    캔들·이평선·추세선은 해당 타임프레임으로 다시 계산하고,
    수평 가격선(박스·방어선·POC·진입/손절/목표·매물대)은 일봉 분석값을
    그대로 겹쳐 그린다(절대 가격이라 모든 프레임에서 유효).
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from . import trendlines as tlmod

    tf = tf if tf in frames else "D"
    lookback = lookback or _LOOKBACK.get(tf, 140)
    project = _PROJECT.get(tf, 12)
    full = frames[tf]
    d = full.iloc[-lookback:]
    # pandas Timestamp는 kaleido(PNG) 직렬화가 안 되므로 파이썬 datetime으로 변환
    x = list(d.index.to_pydatetime())
    x0, x1 = x[0], x[-1]
    avg_delta = (x[-1] - x[0]) / max(1, len(x) - 1)   # 봉 간격(미래연장용)
    x_future = x1 + avg_delta * project
    sr, risk = result["sr"], result["risk"]
    f2 = ".0f" if result["ccy"] == "KRW" else ".2f"
    vfmt = ",.0f" if result["ccy"] == "KRW" else ",.2f"   # 호버 가격 표기

    # 추세선: 일봉은 분석결과(거래량필터 반영), 주/월봉은 그 프레임에서 재검출
    trendline = result["trendline"] if tf == "D" else tlmod.detect(full)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.76, 0.24], vertical_spacing=0.03,
                        subplot_titles=("", "거래량"))

    # 캔들
    fig.add_trace(go.Candlestick(
        x=x, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
        name="가격", increasing_line_color=C["up"], decreasing_line_color=C["down"],
        showlegend=False), row=1, col=1)

    # 이동평균선 (타임프레임별 기간, 가장 짧은 건 생략해 깔끔하게)
    for i, p in enumerate(config.MA_PERIODS[tf][1:]):
        ma = full["Close"].rolling(p).mean().iloc[-lookback:]
        fig.add_trace(go.Scatter(
            x=x, y=ma, name=f"MA{p}", mode="lines",
            legendgroup="ma", visible=("ma" in _DEFAULT_ON),
            line=dict(color=_MA_COLORS[i % len(_MA_COLORS)], width=1.1),
            hovertext=f"MA{p}", hoverinfo="text", showlegend=False), row=1, col=1)

    # 박스권 음영 (항상 표시, 옅음)
    fig.add_hrect(y0=sr["box_low"], y1=sr["box_high"], fillcolor=C["box"],
                  opacity=0.06, line_width=0, row=1, col=1)

    lv = result["levels"]
    va = lv["value_area"]
    # 보이는 가격 범위(여백 8%) — 화면 밖 선이 축을 늘이지 않게 필터
    y_lo = float(d["Low"].min()) * 0.96
    y_hi = float(d["High"].max()) * 1.04
    vis = lambda y: y_lo <= y <= y_hi

    # 매물대 밸류영역(VAL~VAH) 음영 (항상 표시, 옅음)
    fig.add_hrect(y0=va["val"], y1=va["vah"], fillcolor=C["poc"],
                  opacity=0.05, line_width=0, row=1, col=1)

    # 강도순 수평 지지/저항 (스윙 피벗 군집) — 강할수록 굵게. 그룹 'sr'
    for lvl in [l for l in lv["strong"] if vis(l["price"])][:6]:
        w = min(0.8 + lvl["strength"] * 0.5, 3.0)
        col = C["resist"] if lvl["price"] > lv["price"] else C["sup"]
        fig.add_trace(go.Scatter(
            x=[x0, x1], y=[lvl["price"], lvl["price"]], mode="lines",
            line=dict(color=col, width=w, dash="dot"), opacity=0.55,
            legendgroup="sr", name="지지/저항", visible=("sr" in _DEFAULT_ON),
            hovertext=f"지지/저항 {lvl['price']:,.2f}<br>터치 {lvl['touches']}회 "
                      f"· 강도 {lvl['strength']}<br>{glossary.lookup('강도점수')}",
            hoverinfo="text", showlegend=False), row=1, col=1)

    # 피보나치 되돌림 — 그룹 'fib'
    for fl in [f for f in lv["fib"]["levels"] if vis(f["price"])]:
        _hline(fig, x0, x1, fl["price"], f"피보 {fl['ratio']:.3f}", C["fib"],
               group="fib", dash="dot", width=0.8, vfmt=vfmt)

    # 핵심 수평선 (박스상단·방어선·POC) — 그룹 'level'
    conf = ("·".join(sr["confluence"]) if sr["confluence"] else "단독")
    _hline(fig, x0, x1, sr["box_high"], "저항(박스상단)", C["resist"],
           group="level", term="박스권", vfmt=vfmt)
    _hline(fig, x0, x1, sr["defense"],
           f"핵심방어선 ({sr['defense_strength']}·{conf})", C["defense"],
           group="level", term="방어선", width=2.0, vfmt=vfmt)
    _hline(fig, x0, x1, va["poc"], "POC(평단가 밀집)", C["poc"],
           group="level", term="POC", dash="dot", vfmt=vfmt)

    # 매매선 (진입·손절·목표) — 그룹 'trade'
    _hline(fig, x0, x1, result["entry"], "진입(기준가)", C["entry"],
           group="trade", dash="dash", vfmt=vfmt)
    _hline(fig, x0, x1, risk["stop"], "손절", C["stop"],
           group="trade", term="ATR손절", dash="dash", width=1.8, vfmt=vfmt)
    _hline(fig, x0, x1, risk["target"], "목표(1:2)", C["target"],
           group="trade", term="손익비", dash="dash", vfmt=vfmt)

    # 추세선 (하락=빨강, 상승=초록) — 그룹 'trend', 미래 연장 점선 포함
    _trendline(fig, trendline, "down", C["resist"], avg_delta, project)
    _trendline(fig, trendline, "up", C["target"], avg_delta, project)

    # 매물대 분포(장기) 가로 히스토그램 + 추정 평단가 — 그룹 'supply'
    sup = result["supply"]
    prof = sup["long"]
    centers, vols = prof["centers"], prof["vol"]
    mask = (centers >= y_lo) & (centers <= y_hi)
    maxv = float(vols.max()) or 1.0
    spacing = (centers[1] - centers[0]) if len(centers) > 1 else (y_hi - y_lo)
    fig.add_trace(go.Bar(
        x=list(vols[mask]), y=list(centers[mask]), orientation="h",
        xaxis="x3", yaxis="y", width=spacing * 0.85,
        legendgroup="supply", visible=("supply" in _DEFAULT_ON),
        marker=dict(color=C["prof"], opacity=0.16),
        hoverinfo="skip", showlegend=False, name="매물대"))
    pnl = sup["pnl"]
    _hline(fig, x0, x1, pnl["avg_cost"],
           f"추정평단 ({pnl['pnl']*100:+.1f}%)", C["avg"],
           group="supply", term="미실현손익", dash="dashdot", width=1.6, vfmt=vfmt)

    # 거래량 (양봉 초록/음봉 빨강)
    vcolors = [C["up"] if c >= o else C["down"]
               for c, o in zip(d["Close"], d["Open"])]
    fig.add_trace(go.Bar(x=x, y=d["Volume"], marker_color=vcolors,
                         name="거래량", hoverinfo="skip",
                         showlegend=False), row=2, col=1)

    tl_dir = trendline.get("reason", "")
    title = (f"{result['name']} {result['code']} · {_TF_LABEL.get(tf, tf)}"
             f"　|　{result['gauge']} 정규화 {result['norm']:+.0f}"
             f"　|　{result['verdict_label']}　({tl_dir})")
    fig.update_layout(
        title=dict(text=title, font=dict(size=15)),
        template="plotly_white", height=720, width=1100,
        xaxis_rangeslider_visible=False, hovermode="closest",
        showlegend=False,
        margin=dict(l=60, r=120, t=70, b=40),
        # 매물대 가로 히스토그램용 보조 x축 — 좌측 약 22% 폭만 차지하게 스케일
        xaxis3=dict(overlaying="x", anchor="y", side="top",
                    range=[0, maxv * 4.5], visible=False))
    fig.update_yaxes(tickformat=f2, row=1, col=1)
    # 일봉은 주말 빈칸을 제거해 캔들이 겹치지 않고 넓게 보이도록(거래일만 표시)
    if tf == "D":
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    # 우측에 미래연장 여백 확보(추세선 점선이 보이도록), 좌측은 표시구간으로 고정
    fig.update_xaxes(range=[x[0], x_future])
    return fig


def save(result: dict, frames: dict, out_dir: str = "charts",
         png: bool = False) -> str:
    """차트를 HTML(기본)·PNG(옵션)로 저장. 저장 경로(HTML) 반환."""
    os.makedirs(out_dir, exist_ok=True)
    fig = build_figure(result, frames)
    base = os.path.join(out_dir, result["code"])
    html = base + ".html"
    fig.write_html(html, include_plotlyjs="cdn")
    if png:
        fig.write_image(base + ".png", scale=2)
    return html
