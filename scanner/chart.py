"""신호 차트 시각화 (plotly) — v1.5.

캔들 + 박스권/방어선/POC/이평선 + 진입·손절·목표 선 + 거래량.
핵심 선에는 마우스오버 시 용어 설명(glossary)이 뜬다.
HTML(인터랙티브) 저장이 기본, PNG 저장은 옵션(kaleido+chrome 필요).
"""
from __future__ import annotations

import os

import pandas as pd

import glossary

# 색상 팔레트
C = {
    "ma20": "#f59e0b", "ma60": "#8b5cf6", "ma120": "#64748b",
    "box": "#3b82f6", "resist": "#ef4444", "defense": "#dc2626",
    "poc": "#0ea5e9", "entry": "#2563eb", "stop": "#ef4444",
    "target": "#16a34a", "up": "#16a34a", "down": "#ef4444",
    "sup": "#0d9488", "fib": "#a855f7", "avg": "#b45309", "prof": "#0ea5e9",
}


def _level(fig, x_last, y, label, color, term=None, dash="solid", width=1.4):
    """수평선 + (옵션) 마우스오버 용어 설명 마커."""
    import plotly.graph_objects as go

    fig.add_hline(y=y, line=dict(color=color, dash=dash, width=width),
                  annotation_text=label, annotation_position="right",
                  annotation_font=dict(color=color, size=11), row=1, col=1)
    desc = glossary.lookup(term) if term else ""
    hover = f"<b>{label}</b>" + (f"<br>{desc}" if desc else "")
    fig.add_trace(go.Scatter(
        x=[x_last], y=[y], mode="markers",
        marker=dict(color=color, size=9, line=dict(color="white", width=1)),
        name=label, hovertext=hover, hoverinfo="text", showlegend=False),
        row=1, col=1)


def _trendline(fig, tl: dict, which: str, color: str):
    """추세선(사선) 그리기. which='down'|'up'."""
    import plotly.graph_objects as go

    seg = tl.get(which)
    if not seg or "x0" not in seg:
        return
    label = "하락추세선" if which == "down" else "상승추세선"
    fig.add_trace(go.Scatter(
        x=[seg["x0"], seg["x1"]], y=[seg["y0"], seg["y1"]], mode="lines",
        line=dict(color=color, width=2, dash="longdash"),
        name=label, hovertext=f"{label}<br>{glossary.lookup('추세선')}",
        hoverinfo="text"), row=1, col=1)


def build_figure(result: dict, frames: dict, lookback: int = 140):
    """analyze() 결과 + 프레임 → plotly Figure."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    full = frames["D"]
    d = full.iloc[-lookback:]
    # pandas Timestamp는 kaleido(PNG) 직렬화가 안 되므로 파이썬 datetime으로 변환
    x = list(d.index.to_pydatetime())
    x_last = x[-1]
    sr, risk = result["sr"], result["risk"]
    f2 = ".0f" if result["ccy"] == "KRW" else ".2f"

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.76, 0.24], vertical_spacing=0.03,
                        subplot_titles=("", "거래량"))

    # 캔들
    fig.add_trace(go.Candlestick(
        x=x, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
        name="가격", increasing_line_color=C["up"], decreasing_line_color=C["down"],
        showlegend=False), row=1, col=1)

    # 이동평균선
    for p, key in [(20, "ma20"), (60, "ma60"), (120, "ma120")]:
        ma = full["Close"].rolling(p).mean().iloc[-lookback:]
        fig.add_trace(go.Scatter(x=x, y=ma, name=f"MA{p}", mode="lines",
                                 line=dict(color=C[key], width=1.1)), row=1, col=1)

    # 박스권 음영
    fig.add_hrect(y0=sr["box_low"], y1=sr["box_high"], fillcolor=C["box"],
                  opacity=0.06, line_width=0, row=1, col=1)

    lv = result["levels"]
    va = lv["value_area"]
    # 보이는 가격 범위(여백 8%) — 화면 밖 선이 축을 늘이지 않게 필터
    y_lo = float(d["Low"].min()) * 0.96
    y_hi = float(d["High"].max()) * 1.04
    vis = lambda y: y_lo <= y <= y_hi

    # 매물대 밸류영역(VAL~VAH) 음영 — 거래량 70%가 쌓인 '평단가 밀집' 구간
    fig.add_hrect(y0=va["val"], y1=va["vah"], fillcolor=C["poc"],
                  opacity=0.05, line_width=0, row=1, col=1)

    # 강도순 수평 지지/저항 (스윙 피벗 군집) — 강할수록 굵게
    for lvl in [l for l in lv["strong"] if vis(l["price"])][:6]:
        w = min(0.8 + lvl["strength"] * 0.5, 3.0)
        col = C["resist"] if lvl["price"] > lv["price"] else C["sup"]
        fig.add_hline(y=lvl["price"], line=dict(color=col, width=w, dash="dot"),
                      opacity=0.5, row=1, col=1)
        fig.add_trace(go.Scatter(
            x=[x_last], y=[lvl["price"]], mode="markers",
            marker=dict(color=col, size=6, opacity=0.6),
            hovertext=f"지지/저항 {lvl['price']:,.2f}<br>터치 {lvl['touches']}회 "
                      f"· 강도 {lvl['strength']}<br>{glossary.lookup('강도점수')}",
            hoverinfo="text", showlegend=False), row=1, col=1)

    # 피보나치 되돌림 (옅은 선)
    for fl in [f for f in lv["fib"]["levels"] if vis(f["price"])]:
        fig.add_hline(y=fl["price"], line=dict(color=C["fib"], width=0.8, dash="dot"),
                      opacity=0.45, annotation_text=f"피보 {fl['ratio']:.3f}",
                      annotation_position="left",
                      annotation_font=dict(color=C["fib"], size=9), row=1, col=1)

    # 핵심 수평선 (마우스오버 용어 포함)
    conf = ("·".join(sr["confluence"]) if sr["confluence"] else "단독")
    _level(fig, x_last, sr["box_high"], "저항(박스상단)", C["resist"], term="박스권")
    _level(fig, x_last, sr["defense"],
           f"핵심방어선 ({sr['defense_strength']}·{conf})", C["defense"],
           term="방어선", width=2.0)
    _level(fig, x_last, va["poc"], "POC(평단가 밀집)", C["poc"], term="POC", dash="dot")
    _level(fig, x_last, result["entry"], "진입", C["entry"], dash="dash")
    _level(fig, x_last, risk["stop"], "손절", C["stop"], term="ATR손절", dash="dash")
    _level(fig, x_last, risk["target"], "목표(1:2)", C["target"], term="손익비", dash="dash")

    # 추세선 (하락=빨강, 상승=초록) — 사선
    _trendline(fig, result["trendline"], "down", C["resist"])
    _trendline(fig, result["trendline"], "up", C["target"])

    # 매물대 분포(장기) 가로 히스토그램 + 추정 평단가
    sup = result["supply"]
    prof = sup["long"]
    centers, vols = prof["centers"], prof["vol"]
    mask = (centers >= y_lo) & (centers <= y_hi)
    maxv = float(vols.max()) or 1.0
    spacing = (centers[1] - centers[0]) if len(centers) > 1 else (y_hi - y_lo)
    fig.add_trace(go.Bar(
        x=list(vols[mask]), y=list(centers[mask]), orientation="h",
        xaxis="x3", yaxis="y", width=spacing * 0.85,
        marker=dict(color=C["prof"], opacity=0.16),
        hoverinfo="skip", showlegend=False, name="매물대"))
    pnl = sup["pnl"]
    _level(fig, x_last, pnl["avg_cost"],
           f"추정평단 ({pnl['pnl']*100:+.1f}%)", C["avg"],
           term="미실현손익", dash="dashdot", width=1.6)

    # 거래량 (양봉 초록/음봉 빨강)
    vcolors = [C["up"] if c >= o else C["down"]
               for c, o in zip(d["Close"], d["Open"])]
    fig.add_trace(go.Bar(x=x, y=d["Volume"], marker_color=vcolors,
                         name="거래량", showlegend=False), row=2, col=1)

    title = (f"{result['name']} {result['code']}　|　{result['gauge']} "
             f"정규화 {result['norm']:+.0f}　|　{result['verdict_label']}　"
             f"({result['regime']['reason']})")
    fig.update_layout(
        title=dict(text=title, font=dict(size=15)),
        template="plotly_white", height=720, width=1100,
        xaxis_rangeslider_visible=False, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=110, t=70, b=40),
        # 매물대 가로 히스토그램용 보조 x축 — 좌측 약 22% 폭만 차지하게 스케일
        xaxis3=dict(overlaying="x", anchor="y", side="top",
                    range=[0, maxv * 4.5], visible=False))
    fig.update_yaxes(tickformat=f2, row=1, col=1)
    fig.update_xaxes(range=[x[0], x[-1]])   # 추세선이 축을 좌측으로 늘이지 않게
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
