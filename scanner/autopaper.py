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

import config

STATE_PATH = os.path.join("data_cache", "autopaper.json")
VERSION = 4                # 규칙 바뀌면 +1 → 재시작 (v4: 장중에만 매매 — 주말 진입 무효화)
START = 100_000_000        # 시드 1억(사용자 지정)
FX = 1380                  # 달러 환산(모의투자 페이지와 동일 가정)
RISK_PCT = 0.01            # 트레이드당 계좌 1% 리스크
MAX_POS = 12               # 동시 보유(대기 주문 포함) 상한
POS_CAP = 0.15             # 종목당 최대 평가 비중(타이트 손절 종목 과대 매수 방지)
MAX_INVEST = 0.85          # 총 투자 한도 — 15%는 현금 버퍼(눌림 체결·신규 신호용)
TIME_STOP_DAYS = 21        # 캘린더 21일 ≈ 15거래일(config.TIME_STOP_DAYS)
PENDING_DAYS = 21          # 지정가 대기 만료


def _today() -> str:
    return config.today_kst().isoformat()      # 날짜 스탬프는 KST(한국 사용자 기준)


def _market_open(ccy: str) -> bool:
    """해당 종목의 시장이 지금 장중인가 — 장 닫힌 가격(주말·야간 종가)으로
    진입/청산하는 비현실적 시뮬레이션 방지(사용자 지적: 주말 진입은 신뢰성↓).

    한국주: 평일 09:00~15:30 KST(=00:00~06:30 UTC)
    미국주: 평일 정규장(서머타임 포함 넉넉히 13:30~21:00 UTC)
    ※ 판정은 UTC로 한다(의도된 예외) — 미국장이 KST로는 밤 22:30~새벽 05:00로
      자정을 넘어 주말·요일 경계가 꼬이기 때문. 날짜 스탬프만 KST(_today).
    """
    now = datetime.datetime.utcnow()
    if now.weekday() >= 5:                # 토·일 — 어떤 시장도 안 열림
        return False
    hm = now.hour * 60 + now.minute
    if ccy == "KRW":
        return 0 <= hm <= 6 * 60 + 30
    return 13 * 60 + 30 <= hm <= 21 * 60


def _age(day: str) -> int:
    try:
        return (config.today_kst() - datetime.date.fromisoformat(day)).days
    except Exception:
        return 0


def _krw(v: float, ccy: str) -> float:
    return v * FX if ccy == "USD" else v


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fp:
            st = json.load(fp)
            if (st.get("start") == START and "pos" in st
                    and st.get("v") == VERSION):
                return st
    except Exception:
        pass
    return {"v": VERSION, "cash": START, "start": START,
            "pos": {}, "pending": {}, "log": []}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False)


def _log(st: dict, typ: str, code: str, name: str, q: int, price: float,
         ccy: str, note: str, pl: float | None = None,
         pl_pct: float | None = None, r: float | None = None) -> None:
    ent = {"d": _today(), "type": typ, "code": code, "name": name,
           "q": q, "price": round(price, 4), "ccy": ccy,
           "amt": round(_krw(price, ccy) * q), "note": note}
    if pl is not None:
        ent["pl"] = round(pl)
        ent["pl_pct"] = round(pl_pct or 0, 1)
    if r is not None:
        ent["r"] = r                       # 청산 완료 트레이드의 R 성과
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
    p["realized"] = p.get("realized", 0) + pl
    p.setdefault("exits", []).append({"d": _today(), "q": q,
                                      "price": round(price, 4), "note": note})
    p["q"] -= q
    final = p["q"] <= 0
    r = None
    if final and p.get("risk0"):
        r = round(p["realized"] / p["risk0"], 2)     # 트레이드 성과(R 단위)
    _log(st, "sell", code, p["name"], q, price, p["ccy"], note, pl, pct, r=r)
    if final:
        # 청산 완료 — 예측(계획) vs 실제를 통째로 기록(승률·기준 준수 분석용)
        st.setdefault("closed", []).insert(0, {
            "code": code, "name": p["name"], "ccy": p["ccy"],
            "ctx": p.get("ctx"),             # 매수 사유(신선도·단계·저점권·체크·전술)
            "plan": p.get("plan"),           # 진입 시점 우리 기준(진입/손절/목표)
            "fill": {"avg": round(p["avg"], 4), "q": p.get("q0", 0)},  # 실제 체결
            "exits": p["exits"],             # 실제 청산 내역(가격·사유)
            "opened": p["opened"], "closed": _today(),
            "pl": round(p["realized"]), "r": r if r is not None else 0,
        })
        st["closed"] = st["closed"][:400]
        del st["pos"][code]


def _buy(st: dict, code: str, name: str, ccy: str, q: int, price: float,
         stop: float, target: float, note: str,
         plan: dict | None = None, ctx: dict | None = None) -> bool:
    cost = _krw(price, ccy) * q
    if q < 1 or cost > st["cash"]:
        q = int(st["cash"] // _krw(price, ccy))
        cost = _krw(price, ccy) * q
        if q < 1:
            return False
    st["cash"] -= cost
    p = st["pos"].get(code)
    if p:                                  # 반반 2차 체결 — 평단 합산 + 리스크 누적
        p["avg"] = (p["avg"] * p["q"] + price * q) / (p["q"] + q)
        p["q"] += q
        p["q0"] = p.get("q0", 0) + q
        p["risk0"] = p.get("risk0", 0) + _krw(price - stop, ccy) * q
    else:
        st["pos"][code] = {"name": name, "ccy": ccy, "q": q, "q0": q,
                           "avg": price, "entry": price, "stop": stop,
                           "target": target, "half_done": False,
                           "opened": _today(),
                           "plan": plan, "ctx": ctx,
                           "risk0": _krw(price - stop, ccy) * q,
                           "realized": 0, "exits": []}
    _log(st, "buy", code, name, q, price, ccy, note)
    return True


def _basis(item: dict) -> str:
    """매수 사유 한 줄 — '왜 샀는지'를 기록(전부 추천 기준에서 나온 값들)."""
    t = item.get("tactic") or {}
    parts = [item.get("freshness"), item.get("stage"),
             f'저점권{item.get("range_pos", "?")}%',
             f'체크{item.get("rec", "?")}/6',
             f'손절폭{t.get("stop_pct", "?")}%']
    return " · ".join(str(x) for x in parts if x)


def _ctx(item: dict) -> dict:
    t = item.get("tactic") or {}
    return {"mode": t.get("mode", "full"), "fresh": bool(item.get("fresh")),
            "stage": item.get("stage_n", 0), "rp": item.get("range_pos"),
            "rec": item.get("rec"), "basis": _basis(item)}


def _stats(st: dict) -> dict:
    """청산 완료 트레이드로 승률·평균R을 기준별(전술/신선도)로 집계 +
    예측(계획) 대비 실제 체결 편차."""
    cl = st.get("closed", [])

    def agg(rows):
        n = len(rows)
        w = sum(1 for x in rows if x["r"] > 0)
        return {"n": n, "win": w,
                "win_rate": round(w / n * 100) if n else 0,
                "avg_r": round(sum(x["r"] for x in rows) / n, 2) if n else 0}

    by_mode = {}
    for m in ("full", "half", "pullback"):
        rows = [x for x in cl if (x.get("ctx") or {}).get("mode") == m]
        if rows:
            by_mode[m] = agg(rows)
    fresh = agg([x for x in cl if (x.get("ctx") or {}).get("fresh")])
    stale = agg([x for x in cl if x.get("ctx") and not x["ctx"].get("fresh")])
    # 예측 vs 실제 — 계획 진입가 대비 실제 평단(+ = 비싸게 삼), 계획 손절가 대비 손절 체결가
    slips = [(x["fill"]["avg"] / x["plan"]["entry"] - 1) * 100
             for x in cl if x.get("plan") and x["plan"].get("entry")
             and x.get("fill", {}).get("avg")]
    stop_slips = []
    for x in cl:
        ps = (x.get("plan") or {}).get("stop")
        if not ps:
            continue
        for e in x.get("exits", []):
            if "손절" in e["note"]:
                stop_slips.append((e["price"] / ps - 1) * 100)
    return {"all": agg(cl), "by_mode": by_mode, "fresh": fresh, "stale": stale,
            "slip_entry_pct": round(sum(slips) / len(slips), 2) if slips else None,
            "slip_stop_pct": (round(sum(stop_slips) / len(stop_slips), 2)
                              if stop_slips else None)}


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
            ln += f' (<b>{sg}{x["pl"]:,}원 · {sg}{x["pl_pct"]}%</b>'
            if x.get("r") is not None:              # 청산 완료 — R 성과
                sr = "+" if x["r"] >= 0 else ""
                ln += f' · {sr}{x["r"]}R'
            ln += ")"
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
    #    장중인 시장의 종목만 — 닫힌 시장 가격은 움직이지도, 체결되지도 않는다
    for code in list(st["pos"].keys()):
        p = st["pos"][code]
        cur = px.get(code)
        if not cur or not _market_open(p["ccy"]):
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

    # ② 지정가 대기 체결/취소 (주문 금액은 ③에서 예약해둔 상태 — 체결 실패 시 취소)
    for code in list(st["pending"].keys()):
        o = st["pending"][code]
        cur = px.get(code)
        if not cur or not _market_open(o["ccy"]):
            continue
        price = cur[1]
        if price <= o["stop"]:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 취소 — 손절선 이탈(추세 훼손)")
            del st["pending"][code]
        elif price <= o["limit"]:
            filled = _buy(st, code, o["name"], o["ccy"], o["q"], o["limit"],
                          o["stop"], o["target"],
                          "눌림 지정가 체결 — " + (o.get("basis") or ""),
                          plan=o.get("plan"), ctx=o.get("ctx"))
            if not filled:
                _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                     "지정가 취소 — 현금 부족")
            del st["pending"][code]
        elif _age(o["created"]) >= PENDING_DAYS:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 만료 — 눌림 안 옴(놓침≠손실)")
            del st["pending"][code]

    # ③ 신규 진입 — '지금 진입' 추천을 전술대로.
    #    지정가 대기 주문 금액은 '예약'으로 계산해 현금을 다 써버리지 않는다
    #    (실제 증권사의 주문가능금액과 동일 개념 — 대기 주문이 체결 불능이 되는 것 방지)
    equity = _equity(st, px)
    reserved = sum(_krw(o["limit"], o["ccy"]) * o["q"]
                   for o in st["pending"].values())
    invested = equity - st["cash"]                 # 현재 보유 평가액
    budget = min(st["cash"] - reserved,
                 equity * MAX_INVEST - invested - reserved)
    for item in picks.get("now", []):
        code = item["code"]
        if code in st["pos"] or code in st["pending"]:
            continue
        if len(st["pos"]) + len(st["pending"]) >= MAX_POS:
            break
        if budget <= 0:
            break                                      # 주문가능금액 소진
        entry, stop, target = item.get("price"), item["stop"], item["target"]
        if not (entry and stop and target and stop < entry):
            continue
        ccy = item["ccy"]
        if not _market_open(ccy):
            continue                       # 장 닫힘 — 종가로 가짜 진입 금지
        ed = item.get("earnings_d")
        if ed is not None and 0 <= ed <= 3:
            continue                       # 어닝 D-3 이내 — 갭 리스크, 신규 진입 금지
        per = _krw(entry, ccy)                         # 1주 가격(원)
        risk_share = _krw(entry - stop, ccy)           # 1주당 리스크(원)
        q = int(equity * RISK_PCT // risk_share) if risk_share > 0 else 0
        q = min(q, int(equity * POS_CAP // per))       # 종목당 20% 상한
        if q < 1:
            continue
        t = item.get("tactic") or {}
        mode, pb = t.get("mode", "full"), t.get("pb_price")
        # 예측값(우리 기준) 스냅샷 + 매수 사유 — 추천 기준으로만 매수한다는 증빙
        plan = {"entry": entry, "stop": stop, "target": target,
                "pb": pb, "tactic": mode}
        ctx, basis = _ctx(item), _basis(item)
        if mode == "pullback":
            if pb and stop < pb < entry:
                oq = min(q, int(budget // _krw(pb, ccy)))
                if oq < 1:
                    continue
                st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                       "limit": pb, "stop": stop, "target": target,
                                       "q": oq, "created": _today(),
                                       "plan": plan, "ctx": ctx, "basis": basis}
                budget -= _krw(pb, ccy) * oq           # 주문 금액 예약
                _log(st, "order", code, item["name"], oq, pb, ccy,
                     "눌림 지정가 주문(추격 금지) — " + basis)
            continue                                   # 눌림가 없으면 관망
        if mode == "half":
            bq = min(max(1, q // 2), int(budget // per))
            if bq < 1:
                continue
            if _buy(st, code, item["name"], ccy, bq, entry, stop, target,
                    "반반 1차 — " + basis, plan=plan, ctx=ctx):
                budget -= per * bq
            oq = q - q // 2
            if pb and stop < pb < entry and oq >= 1:
                oq = min(oq, int(budget // _krw(pb, ccy)))
                if oq >= 1:
                    st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                           "limit": pb, "stop": stop,
                                           "target": target, "q": oq,
                                           "created": _today(),
                                           "plan": plan, "ctx": ctx, "basis": basis}
                    budget -= _krw(pb, ccy) * oq
            continue
        bq = min(q, int(budget // per))
        if bq >= 1 and _buy(st, code, item["name"], ccy, bq, entry, stop, target,
                            "즉시 분할 — " + basis, plan=plan, ctx=ctx):
            budget -= per * bq

    equity = _equity(st, px)
    _report(st, equity)      # 이번 런 체결 내역 텔레그램 보고(_ev 소비)
    # 수익 곡선용 일별 스냅샷 — 하루 1점(같은 날 재실행 시 최신값으로 갱신)
    hist = st.setdefault("hist", [])
    today = _today()
    if hist and hist[-1]["d"] == today:
        hist[-1]["v"] = round(equity)
    else:
        hist.append({"d": today, "v": round(equity)})
    st["hist"] = hist[-400:]
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
    stats = _stats(st)
    out = {
        "updated": _today(), "start": st["start"], "cash": round(st["cash"]),
        "equity": round(equity),
        "ret_pct": round((equity / st["start"] - 1) * 100, 2),
        "positions": positions,
        "pending": [{"code": c, **o} for c, o in st["pending"].items()],
        "trades": stats["all"]["n"], "win_trades": stats["all"]["win"],
        "stats": stats,                    # 승률·평균R(전술/신선도별) + 예측대비 편차
        "closed": st.get("closed", [])[:30],   # 청산 기록 — 계획 vs 실제 비교용
        "hist": st.get("hist", []),        # 수익 곡선용 일별 평가액 스냅샷
        "log": st["log"][:50],
    }
    os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
    with open(os.path.join(out_dir, "api", "paper_auto.json"), "w",
              encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    return out
