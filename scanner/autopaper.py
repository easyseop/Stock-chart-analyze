"""클로드 자동 모의투자 — 확정 전략(bot/STRATEGY.md)대로 스스로 진입·관리·청산.

사용자 요청: "타점 오면 알아서 들어가는 시뮬레이션, 시드 1억".
매 빌드(장중 15분)마다 이번 빌드의 현재가만 사용(추가 네트워크 0):

  ① 청산 관리 — 손절 터치(전량) → +1R(절반 익절+손절 본전) → 목표 2R(잔량) →
     타임 스탑(21일≈15거래일 내 +1R 미도달 시 정리)
  ② 지정가 체결 — 반반/눌림 전술의 대기 주문이 눌림가에 닿으면 체결,
     손절가까지 빠지면 취소(추세 훼손), 21일 지나면 만료
  ③ 신규 진입 — '지금 진입' 추천을 전술대로: ⚡즉시=전량 / ⚖반반=절반+절반 지정가 /
     ⏳눌림=지정가만. 수량은 계좌 1% 리스크, 종목당 평가 20% 상한

상태: data_cache/autopaper.json (actions/cache 영속) · 공개: public/api/paper_auto.json
track.py(추천 채점)와 다른 점: 여기는 '계좌'를 굴린다 — 현금·비중·복리까지 시뮬레이션.
"""
from __future__ import annotations

import datetime
import json
import os

STATE_PATH = os.path.join("data_cache", "autopaper.json")
START = 100_000_000        # 시드 1억(사용자 지정)
FX = 1380                  # 달러 환산(모의투자 페이지와 동일 가정)
RISK_PCT = 0.01            # 트레이드당 계좌 1% 리스크
MAX_POS = 12               # 동시 보유(대기 주문 포함) 상한
POS_CAP = 0.20             # 종목당 최대 평가 비중(타이트 손절 종목 과대 매수 방지)
TIME_STOP_DAYS = 21        # 캘린더 21일 ≈ 15거래일(config.TIME_STOP_DAYS)
PENDING_DAYS = 21          # 지정가 대기 만료


def _today() -> str:
    return datetime.date.today().isoformat()


def _age(day: str) -> int:
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(day)).days
    except Exception:
        return 0


def _krw(v: float, ccy: str) -> float:
    return v * FX if ccy == "USD" else v


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fp:
            st = json.load(fp)
            if st.get("start") == START and "pos" in st:
                return st
    except Exception:
        pass
    return {"cash": START, "start": START, "pos": {}, "pending": {}, "log": []}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False)


def _log(st: dict, typ: str, code: str, name: str, q: int, price: float,
         ccy: str, note: str, pl: float | None = None,
         pl_pct: float | None = None) -> None:
    ent = {"d": _today(), "type": typ, "code": code, "name": name,
           "q": q, "price": round(price, 4), "ccy": ccy,
           "amt": round(_krw(price, ccy) * q), "note": note}
    if pl is not None:
        ent["pl"] = round(pl)
        ent["pl_pct"] = round(pl_pct or 0, 1)
    st["log"].insert(0, ent)
    st["log"] = st["log"][:200]
    st.setdefault("_ev", []).append(ent)   # 이번 런 이벤트 — 텔레그램 보고용


def _sell(st: dict, code: str, q: int, price: float, note: str) -> None:
    p = st["pos"][code]
    q = min(q, p["q"])
    if q <= 0:
        return
    st["cash"] += _krw(price, p["ccy"]) * q
    pl = (_krw(price, p["ccy"]) - _krw(p["avg"], p["ccy"])) * q   # 실현 손익(원)
    pct = (price / p["avg"] - 1) * 100 if p["avg"] else 0
    _log(st, "sell", code, p["name"], q, price, p["ccy"], note, pl, pct)
    p["q"] -= q
    if p["q"] <= 0:
        del st["pos"][code]


def _buy(st: dict, code: str, name: str, ccy: str, q: int, price: float,
         stop: float, target: float, note: str) -> bool:
    cost = _krw(price, ccy) * q
    if q < 1 or cost > st["cash"]:
        q = int(st["cash"] // _krw(price, ccy))
        cost = _krw(price, ccy) * q
        if q < 1:
            return False
    st["cash"] -= cost
    p = st["pos"].get(code)
    if p:                                  # 반반 2차 체결 — 평단 합산
        p["avg"] = (p["avg"] * p["q"] + price * q) / (p["q"] + q)
        p["q"] += q
    else:
        st["pos"][code] = {"name": name, "ccy": ccy, "q": q, "avg": price,
                           "entry": price, "stop": stop, "target": target,
                           "half_done": False, "opened": _today()}
    _log(st, "buy", code, name, q, price, ccy, note)
    return True


def _equity(st: dict, px: dict) -> float:
    total = st["cash"]
    for code, p in st["pos"].items():
        cur = px.get(code, (None, p["avg"], p["ccy"]))[1]
        total += _krw(cur, p["ccy"]) * p["q"]
    return total


def _fmt_native(v: float, ccy: str) -> str:
    return f"{v:,.0f}원" if ccy == "KRW" else f"${v:,.2f}"


def _report(st: dict, equity: float) -> None:
    """이번 런의 매수/매도/주문을 텔레그램으로 보고(체결 있을 때만).

    토큰 미설정/전송 실패는 조용히 무시 — 시뮬레이션(빌드)은 절대 안 죽인다.
    """
    ev = st.pop("_ev", [])
    if not any(x["type"] in ("buy", "sell") for x in ev):
        return
    icon = {"buy": "🟢 매수", "sell": "🔴 매도",
            "order": "📌 지정가 주문", "cancel": "✖ 주문 취소"}
    lines = []
    for x in ev[:12]:
        ln = (f'{icon.get(x["type"], x["type"])} <b>{x["name"]}</b> '
              f'{x["q"]}주 @ {_fmt_native(x["price"], x["ccy"])} — {x["note"]}')
        if x.get("pl") is not None:                 # 매도: 차익/손절 금액·수익률
            sg = "+" if x["pl"] >= 0 else ""
            ln += f' (<b>{sg}{x["pl"]:,}원 · {sg}{x["pl_pct"]}%</b>)'
        lines.append(ln)
    if len(ev) > 12:
        lines.append(f"…외 {len(ev) - 12}건")
    ret = (equity / st["start"] - 1) * 100
    sg = "+" if ret >= 0 else ""
    text = ("🤖 <b>자동 모의투자 체결 보고</b> (시드 1억)\n"
            + "\n".join(lines)
            + f"\n💰 평가 <b>{equity:,.0f}원</b> ({sg}{ret:.2f}%) · "
              f"현금 {st['cash']:,.0f}원 · 보유 {len(st['pos'])}종목")
    try:
        from bot import notify
        notify.send(text)
    except Exception:
        pass


def update(results: list[dict], picks: dict, out_dir: str = "public") -> dict:
    """빌드마다 호출 — 전략대로 시뮬레이션 한 스텝 진행하고 요약 반환."""
    st = _load()
    st["_ev"] = []
    px = {}
    for r in results:
        p = (r.get("sr") or {}).get("price")
        if p:
            px[r["code"]] = (r["name"], float(p), r.get("ccy", "USD"))

    # ① 보유 관리 — 손절 → +1R 절반 → 목표 → 타임 스탑 (한 스텝에 하나만)
    for code in list(st["pos"].keys()):
        p = st["pos"][code]
        cur = px.get(code)
        if not cur:
            continue
        price = cur[1]
        one_r = p["entry"] + (p["entry"] - p["stop"])
        if price <= p["stop"]:
            note = "손절" if not p["half_done"] else "본전 스탑"
            _sell(st, code, p["q"], price, note)
        elif not p["half_done"] and price >= one_r:
            _sell(st, code, max(1, p["q"] // 2), price, "+1R 절반 익절")
            if code in st["pos"]:          # 잔량 있으면 손절→본전(래칫)
                st["pos"][code]["stop"] = max(p["stop"], p["entry"])
                st["pos"][code]["half_done"] = True
        elif price >= p["target"]:
            _sell(st, code, p["q"], price, "목표(2R) 도달")
        elif _age(p["opened"]) >= TIME_STOP_DAYS and not p["half_done"]:
            _sell(st, code, p["q"], price, "타임 스탑(+1R 미도달)")

    # ② 지정가 대기 체결/취소
    for code in list(st["pending"].keys()):
        o = st["pending"][code]
        cur = px.get(code)
        if not cur:
            continue
        price = cur[1]
        if price <= o["stop"]:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 취소 — 손절선 이탈(추세 훼손)")
            del st["pending"][code]
        elif price <= o["limit"]:
            if _buy(st, code, o["name"], o["ccy"], o["q"], o["limit"],
                    o["stop"], o["target"], "눌림 지정가 체결"):
                del st["pending"][code]
        elif _age(o["created"]) >= PENDING_DAYS:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 만료 — 눌림 안 옴(놓침≠손실)")
            del st["pending"][code]

    # ③ 신규 진입 — '지금 진입' 추천을 전술대로
    equity = _equity(st, px)
    for item in picks.get("now", []):
        code = item["code"]
        if code in st["pos"] or code in st["pending"]:
            continue
        if len(st["pos"]) + len(st["pending"]) >= MAX_POS:
            break
        entry, stop, target = item.get("price"), item["stop"], item["target"]
        if not (entry and stop and target and stop < entry):
            continue
        ccy = item["ccy"]
        risk_share = _krw(entry - stop, ccy)          # 1주당 리스크(원)
        q = int(equity * RISK_PCT // risk_share) if risk_share > 0 else 0
        cap_q = int(equity * POS_CAP // _krw(entry, ccy))
        q = min(q, cap_q)
        if q < 1:
            continue
        t = item.get("tactic") or {}
        mode, pb = t.get("mode", "full"), t.get("pb_price")
        if mode == "pullback":
            if pb and stop < pb < entry:
                st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                       "limit": pb, "stop": stop, "target": target,
                                       "q": q, "created": _today()}
                _log(st, "order", code, item["name"], q, pb, ccy,
                     "눌림 지정가 주문(추격 금지)")
            continue                                   # 눌림가 없으면 관망
        if mode == "half":
            _buy(st, code, item["name"], ccy, max(1, q // 2), entry, stop, target,
                 "반반 — 1차 시장가")
            if pb and stop < pb < entry and q - q // 2 >= 1:
                st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                       "limit": pb, "stop": stop, "target": target,
                                       "q": q - q // 2, "created": _today()}
            continue
        _buy(st, code, item["name"], ccy, q, entry, stop, target, "즉시 분할 진입")

    equity = _equity(st, px)
    _report(st, equity)      # 이번 런 체결 내역 텔레그램 보고(_ev 소비)
    _save(st)
    positions = []
    for code, p in st["pos"].items():
        cur = px.get(code, (p["name"], p["avg"], p["ccy"]))[1]
        pl = (_krw(cur, p["ccy"]) - _krw(p["avg"], p["ccy"])) * p["q"]
        positions.append({"code": code, "name": p["name"], "ccy": p["ccy"],
                          "q": p["q"], "avg": p["avg"], "price": cur,
                          "stop": p["stop"], "target": p["target"],
                          "half_done": p["half_done"], "opened": p["opened"],
                          "pl_krw": round(pl),
                          "pl_pct": round((cur / p["avg"] - 1) * 100, 2)})
    sells = [x for x in st["log"] if x["type"] == "sell"]
    wins = sum(1 for x in sells if "익절" in x["note"] or "목표" in x["note"])
    out = {
        "updated": _today(), "start": st["start"], "cash": round(st["cash"]),
        "equity": round(equity),
        "ret_pct": round((equity / st["start"] - 1) * 100, 2),
        "positions": positions,
        "pending": [{"code": c, **o} for c, o in st["pending"].items()],
        "trades": len(sells), "win_trades": wins,
        "log": st["log"][:50],
    }
    os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
    with open(os.path.join(out_dir, "api", "paper_auto.json"), "w",
              encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    return out
