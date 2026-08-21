#!/usr/bin/env python3
"""동결/ACK 주문의 운영자 승인 대사 — 조회 전용 plan, 증거 기반 apply.

원장만 임의로 닫는 도구가 아니다. 매 실행마다 KIS 미체결·체결·총보유를 새로
읽고, exact ODNO 양수 체결 또는 10분+ 완전 부재를 처리한다. hldg_before가
있으면 잔고불변까지 자동 검증하고, None인 보호 SELL은 운영자 ack가 그 비교를
명시적으로 대신한다. 자동 대사 경로는 before=None을 계속 보류한다.
조회 실패/부분 페이지/소유 경계 불명은 파일을 한 바이트도 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis, kis_reconcile, ledger, ownership  # noqa: E402

US_EXCGS = ("NASD", "NYSE", "AMEX")


def _day(order: dict, market: str) -> str:
    stamp = float(order.get("submitted_at") or time.time())
    zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
    return datetime.datetime.fromtimestamp(stamp, zone).strftime("%Y%m%d")


def _read_market(order: dict) -> dict:
    """완전한 미체결·체결·총보유를 한 묶음으로 읽는다. 실패는 예외."""
    market = str(order.get("market") or kis.market_of_symbol(
        str(order.get("symbol") or ""))).upper()
    day = _day(order, market)
    if market == "KR":
        n_raw = kis.domestic_open_orders()
        n_rows = kis_reconcile.trusted_response_rows(n_raw, domestic=True)
        if n_rows is None and kis.IS_MOCK:
            n_rows = kis_reconcile.trusted_response_rows(
                kis.domestic_unfilled_orders(), domestic=True)
        c_rows = kis_reconcile.trusted_response_rows(
            kis.domestic_fills(start=day, end=day), domestic=True)
        holdings = kis.holdings("KR")
        if n_rows is None or c_rows is None or holdings is None:
            raise RuntimeError("KR broker evidence untrusted")
        normalized = kis_reconcile.normalize_domestic_rows(
            {"rt_cd": "0", "output": n_rows},
            {"rt_cd": "0", "output": c_rows})
        return {"market": market, "nccs": n_rows, "ccnl": c_rows,
                "holdings": holdings, "rows": normalized}

    n_all: list[dict] = []
    c_all: list[dict] = []
    holdings: dict[str, int] = {}
    for excg in US_EXCGS:
        n_rows = kis_reconcile.trusted_response_rows(kis.open_orders(excg=excg))
        c_rows = kis_reconcile.trusted_response_rows(
            kis.fills(excg=excg, start=day, end=day))
        hmap = kis.holdings("US", excg=excg)
        if n_rows is None or c_rows is None or hmap is None:
            raise RuntimeError(f"US {excg} broker evidence untrusted")
        n_all.extend(n_rows)
        c_all.extend(c_rows)
        for symbol, qty in hmap.items():
            holdings[str(symbol).upper()] = holdings.get(str(symbol).upper(), 0) \
                + int(qty)
    normalized = kis_reconcile.normalize_rows(
        {"rt_cd": "0", "output": n_all},
        {"rt_cd": "0", "output": c_all})
    return {"market": market, "nccs": n_all, "ccnl": c_all,
            "holdings": holdings, "rows": normalized}


def collect_plan(key: str, *, now_ts: float | None = None) -> dict:
    """현재 브로커 증거로만 안전한 plan을 만든다(쓰기 0건)."""
    if not ledger.ledger_healthy():
        raise RuntimeError("order ledger corrupt")
    order = ledger.state_of(str(key))
    if not order or order.get("state") not in (
            "submitted", "ack", "unknown", "partial", "cancel_pending", "filled"):
        raise RuntimeError("대상 주문이 없거나 broker in-flight 상태가 아님")
    symbol = str(order.get("symbol") or "").upper()
    side = str(order.get("side") or "").upper()
    odno = kis_reconcile.order_no_key(order.get("odno"))
    intended = int(order.get("intended") or 0)
    if not symbol or side not in ("BUY", "SELL") or not odno or intended <= 0:
        raise RuntimeError("주문 identity/ODNO/수량 불완전")

    open_orders = ledger.open_orders()
    counts = kis_reconcile._broker_inflight_counts(open_orders)
    terminal_review = order.get("state") == "filled" and ownership.is_frozen(symbol)
    if terminal_review:
        base = ownership.baseline()
        if base is None or counts.get(symbol, 0) != 0:
            raise RuntimeError("terminal 동결해제 소유경계/열린주문 불일치")
        if symbol in base:
            try:
                before = int(order.get("legacy_hldg_before")
                             if order.get("legacy_hldg_before") is not None
                             else order.get("hldg_before"))
            except (TypeError, ValueError):
                raise RuntimeError("사용자 baseline 주문은 해제 불가")
            if not kis_reconcile._verified_migrated_baseline_sell(
                    order, symbol, before):
                raise RuntimeError("사용자 baseline 주문은 해제 불가")
    elif not kis_reconcile._direct_evidence_allowed(order, symbol, counts):
        raise RuntimeError("소유 경계 미무장/사용자 baseline/동일심볼 다중주문")

    evidence = _read_market(order)
    rows = evidence["rows"]
    exact = [row for row in rows
             if kis_reconcile.order_no_key(row.get("odno")) == odno
             and str(row.get("pdno") or "").upper() == symbol
             and (not row.get("side") or row.get("side") == side)]
    now_ts = time.time() if now_ts is None else float(now_ts)
    age_s = max(0.0, now_ts - float(order.get("submitted_at") or 0))
    kind = "hold"
    filled = 0
    before_unknown = False
    reason = "직접/부재 증거 불충분"
    if len(exact) == 1 and int(exact[0].get("filled") or 0) > 0:
        kind = "terminal-direct-unfreeze" if terminal_review else "direct-fill"
        filled = min(intended, int(exact[0].get("filled") or 0))
        reason = "ODNO exact 양수 체결행"
    else:
        n_has = any(kis_reconcile.order_no_key(row.get("odno") or row.get("ODNO"))
                    == odno for row in evidence["nccs"])
        c_has = any(kis_reconcile.order_no_key(row.get("odno") or row.get("ODNO"))
                    == odno for row in evidence["ccnl"])
        before_raw = order.get("hldg_before")
        try:
            before = int(before_raw) if before_raw is not None else None
            current = int(evidence["holdings"].get(symbol, 0))
        except (TypeError, ValueError):
            before, current = None, None
        state = str(order.get("state") or "")
        filled_so_far = int(order.get("filled") or 0)
        known_unchanged = before is not None and current == before
        operator_unknown_sell = (side == "SELL" and before_raw is None
                                 and current is not None)
        if (not n_has and not c_has
                and state in ("submitted", "ack") and filled_so_far == 0
                and (known_unchanged or operator_unknown_sell)
                and age_s >= max(kis_reconcile.REJECT_ABSENCE_MIN_S,
                                 kis_reconcile.ACK_AGE_MIN_S)):
            kind = "absence-reject"
            before_unknown = operator_unknown_sell
            reason = ("완전 미체결/체결 부재 + fresh 총보유 확인 + "
                      "운영자 before 미상 승인 + 10분 유예"
                      if before_unknown else
                      "완전 미체결/체결 부재 + 총보유 불변 + 10분 유예")

    return {
        "key": str(key), "symbol": symbol, "side": side,
        "market": evidence["market"], "state": str(order.get("state") or ""),
        "kind": kind, "resolvable": kind != "hold", "filled": filled,
        "before_unknown": before_unknown,
        "exact_odno_matches": len(exact), "open_count": counts.get(symbol, 0),
        "frozen": ownership.is_frozen(symbol), "reason": reason,
        "_rows": rows,  # apply 내부 전용; 출력 전에 제거
    }


def safe_plan(plan: dict) -> dict:
    """화면 출력용 — ODNO·계좌·가격·수량·원장키가 없다."""
    hidden = {"key", "filled"}
    return {key: value for key, value in plan.items()
            if not key.startswith("_") and key not in hidden}


def apply_plan(key: str, *, ack: str) -> dict:
    """fresh evidence를 재수집해 확정하고, terminal일 때만 동결을 해제한다."""
    if not str(ack or "").strip():
        raise PermissionError("--apply에는 --ack 문자열이 필요")
    plan = collect_plan(key)                       # plan 결과 재사용 금지
    if not plan["resolvable"]:
        raise RuntimeError("현재 증거로 자동/운영자 확정 불가")
    evidence = {"symbol": plan["symbol"], "side": plan["side"],
                "market": plan["market"], "kind": plan["kind"],
                "filled": int(plan.get("filled") or 0), "state": "intent",
                "before_unknown": bool(plan.get("before_unknown"))}
    # 실제 상태 전이보다 먼저 운영자 승인 의도를 durable하게 남겨, 그 사이
    # 프로세스가 죽어도 누가 어떤 fresh 증거로 시도했는지 사라지지 않게 한다.
    ledger.record_operator_action(
        str(key), action="ack-resolve-intent", ack=ack, evidence=evidence)
    if plan["kind"] == "direct-fill":
        results = kis_reconcile.resolve_acks_from_rows(
            plan["_rows"], only_keys={str(key)})
        if len(results) != 1:
            raise RuntimeError("fresh direct evidence 적용 경쟁/실패")
        result = results[0]
    elif plan["kind"] == "terminal-direct-unfreeze":
        current = ledger.state_of(str(key)) or {}
        if current.get("state") != "filled":
            raise RuntimeError("terminal 상태가 fresh plan 뒤 변경됨")
        result = {"key": str(key), "state": "filled",
                  "filled": int(current.get("filled") or 0)}
    elif plan["kind"] == "absence-reject":
        result = {"key": str(key), **ledger.reconcile(str(key), 0,
                                                        open_order=False)}
        ledger.record_reconcile_meta(
            str(key), reason="operator-absence-proof",
            meta={"source": "operator-absence-proof",
                  "before_unknown": bool(plan.get("before_unknown")),
                  "broker_reason": (
                      "운영자 확인: 완전 부재+fresh 총보유 확인(before 미상)"
                      if plan.get("before_unknown") else
                      "운영자 확인: 완전 부재+총보유 불변")})
    else:                                          # pragma: no cover - 위 gate 방어
        raise RuntimeError("unsupported plan kind")

    terminal = str(result.get("state") or "") in ("filled", "rejected")
    ledger.record_operator_action(
        str(key), action="ack-resolve", ack=ack,
        evidence={"symbol": plan["symbol"], "side": plan["side"],
                  "market": plan["market"], "kind": plan["kind"],
                  "filled": int(result.get("filled") or 0),
                  "state": str(result.get("state") or ""),
                  "before_unknown": bool(plan.get("before_unknown"))})
    if terminal and ownership.is_frozen(plan["symbol"]):
        ownership.unfreeze(plan["symbol"], ack=ack)
    return {**safe_plan(plan), "result": str(result.get("state") or ""),
            "unfrozen": terminal and not ownership.is_frozen(plan["symbol"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KIS ACK 증거 기반 운영자 대사")
    parser.add_argument("--key", required=True, help="정확한 원장 주문키")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="읽기 전용 미리보기")
    mode.add_argument("--apply", action="store_true", help="fresh 증거로 적용")
    parser.add_argument("--ack", default="", help="apply 운영자 승인 사유")
    args = parser.parse_args(argv)
    try:
        out = (apply_plan(args.key, ack=args.ack) if args.apply
               else safe_plan(collect_plan(args.key)))
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "why": ledger.sanitize_broker_text(exc)},
                         ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **out}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
