#!/usr/bin/env python3
"""브로커 보유 ↔ 원장 대조 — 고아·유령·수량불일치·무보호 포지션 적발(읽기 전용).

왜(실측 2026-08-18): CVNA가 KIS에는 74주 있는데 `kis_positions` 원장에는
아예 없었다. 손절선이 없으니 파수꾼 감시 대상에서 빠져 **무보호**로 방치됐고,
평가액과 원장 매입원가가 어긋나 성과 계산이 한 틱에 +24% 튀어 격리됐다.
거래 이력에 buy·sell 어느 쪽도 없어 원장 이전 과정의 유실로 추정된다.
같은 유실이 다른 종목에도 있는지 사람이 눈으로 확인할 방법이 없었다.

**부재 증명 계약**: 브로커 조회가 한 건이라도 실패하면 "원장에만 있음(유령)"은
보고하지 않는다. 조회 실패를 부재로 오독하면 멀쩡한 포지션을 지우게 된다.
반대로 "브로커에 있음"은 성공한 응답으로 증명되므로 그대로 보고한다.

주문·kill·원장 쓰기를 하지 않는다. 종료코드: 0=이상 없음, 1=조치 필요, 2=조회 실패.
실행: python3 scripts/kis_orphan_audit.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

US_EXCGS = ("NASD", "NYSE", "AMEX")


def collect_broker() -> tuple[dict, list[str]]:
    """{code: {qty, avg, price, market}}, 실패한 조회 목록. 코드 기준 중복 제거."""
    from bot import kis
    rows: dict[str, dict] = {}
    failed: list[str] = []
    kr = kis.positions_detail("KR")
    if kr is None:
        failed.append("KR")
    else:
        for p in kr:
            rows.setdefault(str(p.get("code", "")).upper(),
                            {"qty": int(p.get("qty") or 0),
                             "avg": float(p.get("avg") or 0),
                             "price": float(p.get("price") or 0),
                             "market": "KR"})
    for excg in US_EXCGS:
        us = kis.positions_detail("US", excg=excg)
        if us is None:
            failed.append(excg)
            continue
        for p in us:
            rows.setdefault(str(p.get("code", "")).upper(),
                            {"qty": int(p.get("qty") or 0),
                             "avg": float(p.get("avg") or 0),
                             "price": float(p.get("price") or 0),
                             "market": excg})
    return rows, failed


def audit() -> dict:
    from bot import kis_positions, ownership
    broker, failed = collect_broker()
    ledger = kis_positions.load() or {}
    try:
        baseline = ownership.baseline() or set()
    except Exception:
        baseline = set()
    try:
        frozen = set(ownership.frozen_state() or {})
    except Exception:
        frozen = set()

    orphans, unprotected, mismatched, ghosts = [], [], [], []
    for code, b in sorted(broker.items()):
        if int(b["qty"]) <= 0:
            continue
        row = ledger.get(code)
        entry = {"code": code, "market": b["market"], "broker_qty": b["qty"],
                 "avg": b["avg"], "price": b["price"],
                 "baseline": code in baseline, "frozen": code in frozen}
        if row is None:
            orphans.append(entry)                      # 원장에 없음 = 무보호
            continue
        stop = float(row.get("stop") or 0)
        if stop <= 0:
            unprotected.append({**entry, "stop": stop})
        lq = int(row.get("qty") or 0)
        if lq and lq != int(b["qty"]):
            mismatched.append({**entry, "ledger_qty": lq})
    if not failed:                                     # 부재는 전 조회 성공에서만
        for code, row in sorted(ledger.items()):
            if code not in broker and int(row.get("qty") or 0) > 0:
                ghosts.append({"code": code, "ledger_qty": int(row.get("qty") or 0),
                               "stop": float(row.get("stop") or 0)})

    return {"broker_count": len(broker), "ledger_count": len(ledger),
            "query_failed": failed, "orphans": orphans,
            "unprotected": unprotected, "mismatched": mismatched,
            "ghosts": ghosts,
            "ghosts_suppressed": bool(failed)}


def render(rep: dict) -> int:
    print(f"브로커 보유 {rep['broker_count']}종목 · 원장 {rep['ledger_count']}종목")
    if rep["query_failed"]:
        print(f"⚠️ 조회 실패: {', '.join(rep['query_failed'])} "
              f"— '원장에만 있음' 판정은 생략(실패≠부재)")
    need = 0
    if rep["orphans"]:
        need = 1
        print(f"\n🚨 고아 {len(rep['orphans'])}건 — 브로커에 있고 원장에 없음(무보호):")
        for e in rep["orphans"]:
            tag = " · baseline(기보유)" if e["baseline"] else ""
            print(f"   {e['code']:8} {e['market']:5} {e['broker_qty']:>6}주 "
                  f"평단 {e['avg']}{tag}")
    if rep["unprotected"]:
        need = 1
        print(f"\n🚨 손절선 없음 {len(rep['unprotected'])}건 — 원장엔 있으나 stop<=0:")
        for e in rep["unprotected"]:
            print(f"   {e['code']:8} {e['broker_qty']:>6}주 stop={e['stop']}")
    if rep["mismatched"]:
        need = 1
        print(f"\n⚠️ 수량 불일치 {len(rep['mismatched'])}건:")
        for e in rep["mismatched"]:
            print(f"   {e['code']:8} 브로커 {e['broker_qty']} vs 원장 {e['ledger_qty']}")
    if rep["ghosts"]:
        need = 1
        print(f"\n⚠️ 유령 {len(rep['ghosts'])}건 — 원장엔 있고 브로커엔 없음:")
        for e in rep["ghosts"]:
            print(f"   {e['code']:8} 원장 {e['ledger_qty']}주")
    if not need:
        print("\n✅ 이상 없음 — 브로커·원장 정합")
    if rep["query_failed"]:
        return 2
    return 1 if need else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="브로커↔원장 고아 포지션 점검(읽기 전용)")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = ap.parse_args(argv)
    rep = audit()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 2 if rep["query_failed"] else (
            1 if (rep["orphans"] or rep["unprotected"]
                  or rep["mismatched"] or rep["ghosts"]) else 0)
    return render(rep)


if __name__ == "__main__":
    raise SystemExit(main())
