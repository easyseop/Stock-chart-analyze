"""이미 판 종목의 costbook 잔재 lot을 그 매도에 귀속해 닫는다(읽기 전용 plan / apply).

실측 2026-09-28: 눌림 2차 lot(`…:pb`)과 복구 lot이 부모 키와 다른 키에 쌓여
매도가 닫지 못했다. WLDN 20주 2,252,174원 · CHKP 4주 749,450원 · OMCL 1주가
'열린 원가'로 남아 ① 그 매도의 실현손익이 원가만큼 부풀고(+96%·+127%·+95%)
② A 슬리브 열린 원가가 과대해 매수 여력을 좁혔다.

보정은 append-only다. 잔재 lot마다 close 이벤트 하나를 추가한다 —
대금 0 · 원가 전액 · 실현일은 그 종목의 마지막 매도일 · event_id는 그 매도의
spill id. 그러면 costbook 합계(대금 불변, 원가 인식), 일별 실현, 거래이력 행
(같은 매도 행에 합산)이 한 번에 맞는다. 판매 대금은 이미 부모 close에 전액
실려 있으므로 여기서 다시 싣지 않는다.

대상은 '포지션이 닫혔는데(kis_positions qty 0) lot이 열린' 것뿐이다. 아직
보유 중인 종목의 갈라진 lot(G·ULTA)은 손대지 않는다 — 코드 수정 뒤 매도가
스스로 이어서 닫는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import costbook, kis_positions  # noqa: E402


class Refused(RuntimeError):
    pass


def _cost_events() -> list[dict]:
    path = costbook._path()
    out = []
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except FileNotFoundError:
        pass
    return out


def collect() -> dict:
    folded = costbook._fold()
    if not folded.get("healthy"):
        raise Refused("costbook 손상 — 보정 중단")
    events = _cost_events()
    held = kis_positions.load() or {}
    symbol_of = {k: v.get("symbol") for k, v in folded["lots"].items()}
    # 종목별 마지막 '기본' 매도 close(spill 아님) — 잔재를 귀속할 매도.
    last_close: dict[str, dict] = {}
    for ev in events:
        if ev.get("ev") != "close":
            continue
        eid = str(ev.get("event_id") or "")
        if not eid or costbook.SPILL_SEP in eid:
            continue
        sym = symbol_of.get(str(ev.get("key") or ""))
        if not sym:
            continue
        last_close[sym] = ev
    items, skipped = [], []
    for key, lot in sorted(folded["lots"].items()):
        qty = int(lot.get("qty") or 0)
        if qty <= 0:
            continue
        sym = str(lot.get("symbol") or "")
        if int((held.get(sym) or {}).get("qty") or 0) > 0:
            continue                                   # 아직 보유 — 대상 아님
        base = last_close.get(sym)
        if base is None:
            skipped.append({"key": key, "symbol": sym, "qty": qty,
                            "cost_krw": round(float(lot.get("cost_krw") or 0)),
                            "why": "귀속할 매도 close가 없다 — 수동 검토"})
            continue
        items.append({
            "key": key, "symbol": sym, "qty": qty,
            "sleeve": str(lot.get("sleeve") or "A"),
            "cost_krw": round(float(lot.get("cost_krw") or 0), 2),
            "attach_to": str(base.get("event_id")),
            "day_kst": str(base.get("day_kst") or ""),
            "recorded_pnl_krw": round(float(base.get("realized_pnl_krw") or 0), 2),
            "corrected_pnl_krw": round(float(base.get("realized_pnl_krw") or 0)
                                       - float(lot.get("cost_krw") or 0), 2),
        })
    by_month: dict[str, float] = {}
    for it in items:
        by_month[it["day_kst"][:7]] = by_month.get(it["day_kst"][:7], 0.0) - it["cost_krw"]
    return {"ok": True, "items": items, "skipped": skipped,
            "total_cost_krw": round(sum(i["cost_krw"] for i in items), 2),
            "pnl_delta_by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
            "open_cost_A_before": round(costbook.open_cost_total("A"), 2),
            "open_cost_B_before": round(costbook.open_cost_total("B"), 2)}


def apply(ack: str) -> dict:
    if not ack.strip():
        raise Refused("--ack 사유가 필요하다")
    plan = collect()
    done = []
    for it in plan["items"]:
        eid = costbook.spill_event_id(it["attach_to"], it["key"])
        if eid in costbook._fold().get("event_results", {}):
            done.append({**it, "state": "already"})
            continue
        costbook._append({"ev": "close", "key": it["key"], "qty": it["qty"],
                          "proceeds_krw": 0.0, "cost_closed_krw": it["cost_krw"],
                          "realized_pnl_krw": -it["cost_krw"],
                          "sleeve": it["sleeve"], "day_kst": it["day_kst"],
                          "event_id": eid, "operator_ack": ack.strip()[:200],
                          "reason": "stale-lot-repair"})
        done.append({**it, "state": "closed", "event_id": eid})
    return {**plan, "applied": done,
            "open_cost_A_after": round(costbook.open_cost_total("A"), 2),
            "open_cost_B_after": round(costbook.open_cost_total("B"), 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="이미 판 종목의 costbook 잔재 lot 보정")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="읽기 전용 미리보기(기본)")
    mode.add_argument("--apply", action="store_true", help="costbook에 보정 close 추가")
    ap.add_argument("--ack", default="", help="apply 운영자 승인 사유")
    args = ap.parse_args(argv)
    try:
        out = apply(args.ack) if args.apply else collect()
    except Refused as exc:
        print(json.dumps({"ok": False, "why": str(exc)}, ensure_ascii=False, indent=1))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
