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

    # 핵심 수평선 (마우스오버 용어 포함)
    conf = ("·".join(sr["confluence"]) if sr["confluence"] else "단독")
    _level(fig, x_last, sr["box_high"], "저항(박스상단)", C["resist"], term="박스권")
    _level(fig, x_last, sr["defense"],
           f"핵심방어선 ({sr['defense_strength']}·{conf})", C["defense"],
           term="방어선", width=2.0)
    _level(fig, x_last, sr["poc"], "POC(매물대)", C["poc"], term="POC", dash="dot")
    _level(fig, x_last, result["entry"], "진입", C["entry"], dash="dash")
    _level(fig, x_last, risk["stop"], "손절", C["stop"], term="ATR손절", dash="dash")
    _level(fig, x_last, risk["target"], "목표(1:2)", C["target"], term="손익비", dash="dash")

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
        margin=dict(l=60, r=110, t=70, b=40))
    fig.update_yaxes(tickformat=f2, row=1, col=1)
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
