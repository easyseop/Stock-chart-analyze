"""CVNA H4 유실 BUY forensic 회계 복구의 정확원가·멱등·크래시 검증."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack
from unittest import mock

from bot import accounting_recovery as R
from bot import costbook as C
from bot import kis_positions as P
from bot import ledger as L
from bot import ownership as O

KEY = "kb:CVNA:CVNA-2026-08-18-now"
ODNO = "0000040445"


def _env(stack: ExitStack, tmp: str) -> None:
    stack.enter_context(mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")))
    stack.enter_context(mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")))
    stack.enter_context(mock.patch.dict(os.environ, {
        "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
        "USER_BASELINE_PATH": os.path.join(tmp, "baseline.json"),
        "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
    }))
    O.capture_baseline([])
    L.record_submit(KEY, "CVNA", 74, "pullback", meta={
        "side": "BUY", "market": "US", "excg": "NYSE",
        "price": 65.0332, "hldg_before": 0, "pos_key": KEY,
        "sleeve": "A", "fx": 1380.0, "ccy": "USD", "stop": 62.2689,
        "target": 85.7323, "name": "Carvana", "opened": "2026-08-18",
        "reservation_cost_krw": 6641190.384,
    })
    L.bind_broker_order(KEY, ODNO)
    L.on_result(KEY, "ack", 0, open_order=True)
    L.reconcile(KEY, 0, open_order=False)
    L.record_reconcile_meta(
        KEY, reason="broker-closed-zero-fill",
        meta={"source": "ccnl", "side": "BUY", "intended": 74})
    P.record("CVNA", qty=74, entry=65.03, stop=60.48, ccy="USD",
             name="Carvana", opened="2026-08-18", sleeve="A", pos_key="")


def _ccnl(*_args, **_kwargs):
    return {"rt_cd": "0", "output": [{
        "odno": ODNO, "pdno": "CVNA", "sll_buy_dvsn_cd": "02",
        "ft_ord_qty": "74", "ft_ccld_qty": "74",
        "ft_ccld_unpr3": "65.03", "ord_tmd": "223038",
    }]}


def _positions(*_args, **_kwargs):
    return [{"code": "CVNA", "qty": 74, "avg": 65.03}]


def _plan(now: float = 1000.0):
    with mock.patch.object(R.kis, "fills", side_effect=_ccnl), \
            mock.patch.object(R.kis, "positions_detail", side_effect=_positions):
        return R.build_plan(
            order_key=KEY, odno=ODNO, symbol="CVNA", qty=74,
            fill_price=65.03, fx=1380, cost_krw=6640863,
            trade_date="20260818", now=now)


def _apply(plan: dict, backup: str, *, now: float = 1001.0):
    with mock.patch.object(R.kis, "fills", side_effect=_ccnl), \
            mock.patch.object(R.kis, "positions_detail", side_effect=_positions), \
            mock.patch.object(R.legacy_migration, "_services_quiesced",
                              return_value=(True, "ok")):
        return R.apply_plan(
            plan, ack=f"APPLY {plan['plan_sha256']}", services_stopped=True,
            backup_dir=backup, now=now)


def test_exact_cost_and_absolute_position_are_idempotent():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        plan = _plan()
        result = _apply(plan, os.path.join(tmp, "backup-1"))
        assert result["ok"] and result["cost_krw"] == 6640863
        lot = C._fold()["lots"][KEY]
        assert lot["qty"] == 74 and lot["cost_krw"] == 6640863
        pos = P.load()["CVNA"]
        assert pos["qty"] == 74 and pos["pos_key"] == KEY
        assert pos["accounting_repaired"] is True and pos["stop"] == 60.48
        order = L.state_of(KEY)
        assert order["state"] == "filled" and order["accounted"] == 74
        before = {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        again = _apply(plan, os.path.join(tmp, "unused-backup"))
        assert again["already_applied"] is True
        assert before == {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
    print("[PASS] CVNA 6,640,863원·74주 절대복구 + 재실행 byte-idempotent")


def test_crash_after_costbook_recovers_without_duplicate_lot_or_qty():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        plan = _plan()
        original = P.repair_buy_fill
        with mock.patch.object(P, "repair_buy_fill", side_effect=OSError("crash")), \
                mock.patch.object(R.kis, "fills", side_effect=_ccnl), \
                mock.patch.object(R.kis, "positions_detail", side_effect=_positions), \
                mock.patch.object(R.legacy_migration, "_services_quiesced",
                                  return_value=(True, "ok")):
            try:
                R.apply_plan(
                    plan, ack=f"APPLY {plan['plan_sha256']}",
                    services_stopped=True,
                    backup_dir=os.path.join(tmp, "backup-crash"), now=1001)
                raise AssertionError("crash injection did not fire")
            except OSError:
                pass
        assert C.open_qty("CVNA") == 74
        assert P.load()["CVNA"]["qty"] == 74       # 기존 보호수량, 아직 증분 아님
        with mock.patch.object(P, "repair_buy_fill", side_effect=original):
            result = _apply(plan, os.path.join(tmp, "backup-retry"), now=1002)
        assert result["ok"] and C.open_qty("CVNA") == 74
        assert P.load()["CVNA"]["qty"] == 74
    print("[PASS] costbook 뒤 크래시 재실행도 lot·보호수량 중복 0")


def test_plan_requires_fresh_exact_broker_truth_and_unowned_symbol():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        with mock.patch.object(R.kis, "fills", return_value=None), \
                mock.patch.object(R.kis, "positions_detail", side_effect=_positions):
            try:
                R.build_plan(order_key=KEY, odno=ODNO, symbol="CVNA", qty=74,
                             fill_price=65.03, fx=1380, cost_krw=6640863,
                             trade_date="20260818", now=1000)
                raise AssertionError("broker 조회 실패를 plan으로 오독")
            except R.RecoveryRefused:
                pass
        O.adopt_manual_position("CVNA")
        with mock.patch.object(R.kis, "fills", side_effect=_ccnl), \
                mock.patch.object(R.kis, "positions_detail", side_effect=_positions):
            try:
                R.build_plan(order_key=KEY, odno=ODNO, symbol="CVNA", qty=74,
                             fill_price=65.03, fx=1380, cost_krw=6640863,
                             trade_date="20260818", now=1000)
                raise AssertionError("baseline 수동 보유를 봇 회계로 복구")
            except R.RecoveryRefused:
                pass
    print("[PASS] 조회 실패≠체결 · baseline 심볼은 forensic 복구 거부")


def main():
    test_exact_cost_and_absolute_position_are_idempotent()
    test_crash_after_costbook_recovers_without_duplicate_lot_or_qty()
    test_plan_requires_fresh_exact_broker_truth_and_unowned_symbol()
    print("\n모든 forensic 회계 복구 테스트 통과.")


if __name__ == "__main__":
    main()
