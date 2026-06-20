"""신호 카드 렌더링 (텍스트) + CSV 행 생성."""
from __future__ import annotations

import glossary


def _fmt(x: float, ccy: str) -> str:
    if ccy == "KRW":
        return f"{x:,.0f}"
    return f"{x:,.2f}"


def render(result: dict) -> str:
    """analyze() 결과 dict → 텍스트 카드."""
    r = result
    ccy = r["ccy"]
    f = lambda v: _fmt(v, ccy)
    sr = r["sr"]
    risk = r["risk"]

    line = "─" * 60
    out = []
    out.append(f"[{r['name']} {r['code']}]"
               f"{' ' * max(1, 30 - len(r['name']))}"
               f"심리: {r['gauge']} (정규화 {r['norm']:+.0f}점)")
    out.append(line)
    out.append(f"국면      : {r['regime']['reason']}  방향 {r['regime']['direction']}")
    out.append(f"추세      : {r['trend']['reason']}  [{r['trend']['score']:+d}]")
    out.append(f"모멘텀    : {r['rsi']['reason']}  [{r['rsi']['score']:+d}]")
    out.append(f"지지/저항 : {sr['reason']}  [{sr['score']:+d}]")
    out.append(f"거래대금  : {r['volume']['reason']}  [{r['volume']['score']:+d}]")
    out.append(line)
    # 박스권 / 방어선 — 항상 노출
    conf = ("·".join(sr["confluence"]) + " 겹침" if sr["confluence"] else "단독")
    out.append(f"박스권    : {f(sr['box_low'])} ~ {f(sr['box_high'])}  "
               f"(현재 {f(sr['price'])}, 상단 −{sr['pct_to_high']:.1f}%)")
    out.append(f"핵심방어선 : {f(sr['defense'])} ⚠️ 이탈금지  "
               f"(신뢰도 {sr['defense_strength']}, {conf})")
    out.append(f"            └ 종가 이탈 시 추세 훼손 → 즉시 손절/회피")
    out.append(line)
    out.append(f"판정      : {r['verdict']}")
    out.append(f"진입(기준): {f(r['entry'])}")
    out.append(f"손절      : {f(risk['stop'])}  "
               f"(ATR손절 {f(risk['atr_stop'])} / 방어선 {f(sr['defense'])} 중 택)")
    out.append(f"1차 목표  : {f(risk['target'])}  (손익비 1:{risk['rr']:.0f})")
    out.append(f"비중      : {risk['shares']}주  "
               f"(계좌 {f(risk['account'])} {ccy}의 1% 손실 허용 기준)")

    # 각주
    notes = glossary.footnotes(r["terms"])
    if notes:
        out.append(line)
        out.append("용어:")
        out.append(notes)
    return "\n".join(out)


CSV_FIELDS = ["code", "name", "norm", "verdict", "gauge", "regime_flag",
              "trend", "rsi", "sr", "volume", "entry", "stop", "target",
              "box_low", "box_high", "defense", "defense_strength"]


def to_row(result: dict) -> dict:
    r = result
    return {
        "code": r["code"], "name": r["name"], "norm": r["norm"],
        "verdict": r["verdict"], "gauge": r["gauge"],
        "regime_flag": r["regime"]["flag"],
        "trend": r["trend"]["score"], "rsi": r["rsi"]["score"],
        "sr": r["sr"]["score"], "volume": r["volume"]["score"],
        "entry": round(r["entry"], 2), "stop": round(r["risk"]["stop"], 2),
        "target": round(r["risk"]["target"], 2),
        "box_low": round(r["sr"]["box_low"], 2),
        "box_high": round(r["sr"]["box_high"], 2),
        "defense": round(r["sr"]["defense"], 2),
        "defense_strength": r["sr"]["defense_strength"],
    }
