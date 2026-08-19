"""브로커 체결은 존재하지만 회계가 유실된 BUY의 forensic plan/apply 복구.

주문을 전혀 내지 않는다. plan과 apply 모두 KIS 체결·잔고를 새로 읽고, apply는
서비스 runtime mask·stale heartbeat·SHA 승인·미존재 백업 디렉터리를 요구한다.
append-only 세 원장은 공통 event_id로 멱등 복구하므로 중간 크래시 뒤 새 백업
디렉터리로 같은 plan을 재실행할 수 있다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time

from bot import costbook, kis, kis_positions, kis_reconcile, ledger, ownership
from bot import legacy_migration

PLAN_VERSION = 1
PLAN_MAX_AGE_S = 300
US_EXCGS = ("NASD", "NYSE", "AMEX")


class RecoveryRefused(RuntimeError):
    """증거·원장·운영 전제 하나라도 불충분해 복구를 거부했다."""


def _canonical(payload: dict) -> bytes:
    clean = {key: value for key, value in payload.items()
             if key != "plan_sha256"}
    return json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def plan_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _positive(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused(f"{label} 숫자 변환 실패") from exc
    if not math.isfinite(number) or number <= 0:
        raise RecoveryRefused(f"{label} 유한한 양수 필요")
    return number


def _paths() -> dict[str, str]:
    return {
        "order_ledger": os.path.abspath(ledger.LEDGER_PATH),
        "kis_positions": os.path.abspath(kis_positions.PATH),
        "costbook": os.path.abspath(costbook._path()),
    }


def _broker_fill(*, trade_date: str, odno: str, symbol: str,
                 qty: int, fill_price: float) -> dict:
    matches: list[dict] = []
    for excg in US_EXCGS:
        response = kis.fills(excg=excg, start=trade_date, end=trade_date)
        if not isinstance(response, dict) or response.get("rt_cd") != "0":
            raise RecoveryRefused(f"KIS 체결조회 실패: {excg}")
        rows = kis_reconcile.normalize_rows(
            {"rt_cd": "0", "output": []}, response)
        for row in rows:
            if kis_reconcile.order_no_key(row.get("odno")) \
                    == kis_reconcile.order_no_key(odno):
                matches.append(row)
    if not matches:
        raise RecoveryRefused("KIS 체결조회에 지정 ODNO 부재")
    normalized = {
        (str(row.get("pdno") or "").upper(), str(row.get("side") or ""),
         int(row.get("filled") or 0), round(float(row.get("price") or 0), 6))
        for row in matches
    }
    if len(normalized) != 1:
        raise RecoveryRefused("거래소별 체결 증거 충돌")
    code, side, filled, price = next(iter(normalized))
    if code != symbol or side != "BUY" or filled != qty \
            or abs(price - fill_price) > 0.005:
        raise RecoveryRefused(
            f"KIS 체결 증거 불일치({code}/{side}/{filled}/{price})")
    return {"symbol": code, "side": side, "qty": filled,
            "fill_price": price, "odno": str(odno),
            "trade_date": trade_date}


def _broker_position(symbol: str, qty: int) -> dict:
    matches: list[tuple[int, float]] = []
    for excg in US_EXCGS:
        rows = kis.positions_detail("US", excg=excg)
        if rows is None:
            raise RecoveryRefused(f"KIS 잔고조회 실패: {excg}")
        for row in rows:
            if str(row.get("code") or "").upper() != symbol:
                continue
            try:
                matches.append((int(row.get("qty") or 0),
                                round(float(row.get("avg") or 0), 6)))
            except (TypeError, ValueError) as exc:
                raise RecoveryRefused("KIS 잔고 수량/평단 형식 오류") from exc
    unique = set(matches)
    if len(unique) != 1:
        raise RecoveryRefused("거래소별 잔고 증거 부재/충돌")
    actual_qty, average = next(iter(unique))
    if actual_qty != qty or average <= 0:
        raise RecoveryRefused(
            f"KIS 잔고 불일치({actual_qty}주, 평단 {average})")
    return {"symbol": symbol, "qty": actual_qty, "avg": average}


def _validate_order(order: dict, spec: dict) -> None:
    if (str(order.get("symbol") or "").upper() != spec["symbol"]
            or str(order.get("side") or "").upper() != "BUY"
            or int(order.get("intended") or 0) != spec["qty"]
            or kis_reconcile.order_no_key(order.get("odno"))
            != kis_reconcile.order_no_key(spec["odno"])):
        raise RecoveryRefused("주문 원장 정체성/수량 불일치")
    state = str(order.get("state") or "")
    filled = int(order.get("filled") or 0)
    accounted = int(order.get("accounted") or 0)
    initial = (
        state == "rejected" and filled == 0 and accounted == 0
        and str(order.get("reconcile_reason") or "")
        in {"broker-closed-zero-fill", "zero-fill-balance-proof"}
    )
    progressing = (
        state == "filled" and filled == spec["qty"]
        and accounted in (0, spec["qty"])
        and bool(order.get("accounting_recovery_pending")
                 or order.get("accounting_recovery_complete"))
    )
    if not (initial or progressing):
        raise RecoveryRefused(
            f"주문 원장 예상 상태 아님({state}/{filled}/{accounted})")


def _validate_local(spec: dict) -> dict:
    if _paths() != spec["journal_paths"]:
        raise RecoveryRefused("plan과 운영 원장 경로 불일치")
    if not ledger.ledger_healthy():
        raise RecoveryRefused("주문 원장 손상")
    order = ledger.state_of(spec["order_key"]) or {}
    _validate_order(order, spec)
    base = ownership.baseline()
    if base is None or spec["symbol"] in base:
        raise RecoveryRefused("ownership 미armed 또는 사용자 baseline 종목")
    rec = (kis_positions.load() or {}).get(spec["symbol"])
    if not rec or int(rec.get("qty") or 0) != spec["qty"]:
        raise RecoveryRefused("보호 포지션 수량 불일치")
    if abs(float(rec.get("entry") or 0) - spec["fill_price"]) > 0.005 \
            or float(rec.get("stop") or 0) <= 0:
        raise RecoveryRefused("보호 포지션 평단/손절선 불일치")
    book = costbook._fold()
    if not book.get("healthy"):
        raise RecoveryRefused("costbook 손상")
    event = (book.get("event_results") or {}).get(spec["event_id"])
    lot = (book.get("lots") or {}).get(spec["pos_key"])
    if event is None:
        if costbook.open_qty(spec["symbol"]) != 0:
            raise RecoveryRefused("기존 동종목 costbook lot 충돌")
    else:
        if abs(float(event.get("cost_krw") or 0) - spec["cost_krw"]) > 1e-6 \
                or not lot or int(lot.get("qty") or 0) != spec["qty"] \
                or abs(float(lot.get("cost_krw") or 0) - spec["cost_krw"]) > 1e-6:
            raise RecoveryRefused("복구 costbook event 충돌")
    complete = (
        int(order.get("filled") or 0) == spec["qty"]
        and int(order.get("accounted") or 0) == spec["qty"]
        and order.get("accounting_recovery_complete") is True
        and event is not None
        and rec.get("accounting_repaired") is True
        and str(rec.get("pos_key") or "") == spec["pos_key"]
    )
    return {"order": order, "position": rec, "book": book,
            "complete": complete}


def build_plan(*, order_key: str, odno: str, symbol: str, qty: int,
               fill_price: float, fx: float, cost_krw: float,
               trade_date: str, now: float | None = None) -> dict:
    stamp = time.time() if now is None else float(now)
    code = str(symbol or "").strip().upper()
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise RecoveryRefused("trade_date YYYYMMDD 필요")
    q = int(qty)
    if not code or q <= 0 or not str(order_key or "").strip() \
            or not str(odno or "").strip():
        raise RecoveryRefused("복구 주문 정체성 누락")
    px = _positive(fill_price, "fill_price")
    rate = _positive(fx, "fx")
    exact = _positive(cost_krw, "cost_krw")
    order = ledger.state_of(order_key) or {}
    pos_key = str(order.get("pos_key") or order_key)
    rec = (kis_positions.load() or {}).get(code) or {}
    spec = {
        "version": PLAN_VERSION, "created_at": stamp,
        "expires_at": stamp + PLAN_MAX_AGE_S,
        "journal_paths": _paths(), "order_key": str(order_key),
        "odno": str(odno), "symbol": code, "qty": q,
        "fill_price": px, "fx": rate, "cost_krw": exact,
        "trade_date": trade_date, "pos_key": pos_key,
        "event_id": f"fill:{order_key}:BUY:{q}",
        "position": {
            "entry": float(rec.get("entry") or 0),
            "stop": float(rec.get("stop") or 0),
            "stop0": float(rec.get("stop0") or rec.get("stop") or 0),
            "ccy": str(rec.get("ccy") or "USD"),
            "name": str(rec.get("name") or order.get("name") or code),
            "opened": str(rec.get("opened") or order.get("opened") or ""),
            "sleeve": str(rec.get("sleeve") or order.get("sleeve") or "A").upper(),
            "target": rec.get("target", order.get("target")),
        },
    }
    _validate_local(spec)
    spec["broker_fill"] = _broker_fill(
        trade_date=trade_date, odno=odno, symbol=code, qty=q, fill_price=px)
    spec["broker_position"] = _broker_position(code, q)
    spec["plan_sha256"] = plan_hash(spec)
    return spec


def _assert_plan(plan: dict, *, ack: str, services_stopped: bool,
                 now: float | None = None) -> None:
    stamp = time.time() if now is None else float(now)
    if int(plan.get("version") or 0) != PLAN_VERSION:
        raise RecoveryRefused("지원하지 않는 plan 버전")
    digest = plan_hash(plan)
    if digest != str(plan.get("plan_sha256") or ""):
        raise RecoveryRefused("plan SHA256 불일치")
    if str(ack or "") != f"APPLY {digest}":
        raise RecoveryRefused("정확한 plan SHA operator ack 필요")
    if not services_stopped:
        raise RecoveryRefused("--services-stopped 명시 필요")
    if stamp > float(plan.get("expires_at") or 0):
        raise RecoveryRefused("plan 만료 — 새 5분 plan 필요")
    required = {
        "order_key", "odno", "symbol", "qty", "fill_price", "fx",
        "cost_krw", "trade_date", "pos_key", "event_id", "position",
        "journal_paths", "broker_fill", "broker_position",
    }
    if not required.issubset(plan):
        raise RecoveryRefused("plan 필수 필드 누락")


def _broker_matches(plan: dict) -> None:
    fill = _broker_fill(
        trade_date=plan["trade_date"], odno=plan["odno"],
        symbol=plan["symbol"], qty=int(plan["qty"]),
        fill_price=float(plan["fill_price"]))
    position = _broker_position(plan["symbol"], int(plan["qty"]))
    if fill != plan["broker_fill"] or position != plan["broker_position"]:
        raise RecoveryRefused("plan 이후 브로커 증거 변경")


def apply_plan(plan: dict, *, ack: str, services_stopped: bool,
               backup_dir: str, now: float | None = None) -> dict:
    _assert_plan(plan, ack=ack, services_stopped=services_stopped, now=now)
    quiesced, why = legacy_migration._services_quiesced()
    if not quiesced:
        raise RecoveryRefused(f"주문 서비스 미정지: {why}")
    _broker_matches(plan)
    pre = _validate_local(plan)
    if pre["complete"]:
        return {"ok": True, "already_applied": True, "orders_sent": 0,
                "cost_krw": float(plan["cost_krw"])}
    backup = legacy_migration._backup_sources(backup_dir)
    quiesced, why = legacy_migration._services_quiesced()
    if not quiesced:
        raise RecoveryRefused(f"백업 후 주문 서비스 재등장: {why}")
    _broker_matches(plan)
    _validate_local(plan)

    q = int(plan["qty"])
    pos = plan["position"]
    with ledger._file_lock(True):
        fold, corrupt = ledger._fold_unlocked()
        if corrupt:
            raise RecoveryRefused("mutation 직전 주문 원장 손상")
        current = fold.get(plan["order_key"]) or {}
        _validate_order(current, plan)
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {
                "pos_key": plan["pos_key"], "fx": float(plan["fx"]),
                "ccy": pos["ccy"], "stop": float(pos["stop"]),
                "target": pos.get("target"), "name": pos["name"],
                "opened": pos["opened"], "sleeve": pos["sleeve"],
                "accounting_recovery_pending": True,
                "accounting_recovery_complete": False,
            },
        })
        if int(current.get("filled") or 0) != q:
            ledger._append_unlocked({
                "ev": "reconcile", "key": plan["order_key"],
                "state": "filled", "filled": q, "open": False,
                "fill_price": float(plan["fill_price"]),
                "fill_price_source": "broker-recovery-delayed-ccnl",
            })
            ledger._append_unlocked({
                "ev": "reconcile_meta", "key": plan["order_key"],
                "reason": "broker-recovery-delayed-ccnl",
                "meta": {"source": "broker-recovery-delayed-ccnl",
                         "side": "BUY", "intended": q},
            })
        costbook.add_recovery_lot(
            plan["pos_key"], plan["symbol"], q, plan["fill_price"],
            fx=plan["fx"], cost_krw=plan["cost_krw"],
            sleeve=pos["sleeve"], event_id=plan["event_id"])
        kis_positions.repair_buy_fill(
            plan["symbol"], qty=q, entry=plan["fill_price"],
            stop=pos["stop"], stop0=pos["stop0"], ccy=pos["ccy"],
            pos_key=plan["pos_key"], name=pos["name"], opened=pos["opened"],
            sleeve=pos["sleeve"], target=pos.get("target"),
            event_id=plan["event_id"])
        ledger._append_unlocked({
            "ev": "accounted", "key": plan["order_key"], "accounted": q})
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {"accounting_recovery_pending": False,
                     "accounting_recovery_complete": True},
        })
    final = _validate_local(plan)
    if not final["complete"]:
        raise RecoveryRefused("복구 후 세 원장 최종 검증 실패")
    return {"ok": True, "already_applied": False, "orders_sent": 0,
            "cost_krw": float(plan["cost_krw"]), "qty": q,
            "backup_manifest_sha256": backup["manifest_sha256"]}


def _write_plan(path: str, plan: dict) -> None:
    target = os.path.abspath(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".accounting-recovery-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fp:
            fd = -1
            json.dump(plan, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="유실 BUY forensic 회계 복구(주문 없음)")
    sub = ap.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("plan", help="KIS 체결·잔고를 재확인해 5분 plan 생성")
    for parser in (pp,):
        parser.add_argument("--order-key", required=True)
        parser.add_argument("--odno", required=True)
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--qty", required=True, type=int)
        parser.add_argument("--fill-price", required=True, type=float)
        parser.add_argument("--fx", required=True, type=float)
        parser.add_argument("--cost-krw", required=True, type=float)
        parser.add_argument("--trade-date", required=True)
    pp.add_argument("--output", required=True)
    pa = sub.add_parser("apply", help="검증 plan을 append-only 원장에 적용")
    pa.add_argument("--plan", required=True)
    pa.add_argument("--ack", required=True)
    pa.add_argument("--services-stopped", action="store_true")
    pa.add_argument("--backup-dir", required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_plan(
                order_key=args.order_key, odno=args.odno, symbol=args.symbol,
                qty=args.qty, fill_price=args.fill_price, fx=args.fx,
                cost_krw=args.cost_krw, trade_date=args.trade_date)
            _write_plan(args.output, plan)
            result = {"ok": True, "mode": "read-only-plan", "orders_sent": 0,
                      "plan": os.path.abspath(args.output),
                      "plan_sha256": plan["plan_sha256"],
                      "apply_ack": f"APPLY {plan['plan_sha256']}"}
        else:
            with open(args.plan, encoding="utf-8") as fp:
                plan = json.load(fp)
            result = apply_plan(
                plan, ack=args.ack, services_stopped=args.services_stopped,
                backup_dir=args.backup_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RecoveryRefused, legacy_migration.MigrationRefused,
            OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "refused": True, "why": str(exc),
                          "orders_sent": 0}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
