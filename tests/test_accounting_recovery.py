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
from bot import trade_history as H

KEY = "kb:CVNA:CVNA-2026-08-18-now"
ODNO = "0000040445"
SELL_KEY = "xe:CVNA:half:#1"
SELL_ODNO = "0000041614"
LEGACY_POS_KEY = "legacy:A:CVNA:?"


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


def _ccnl_partial(*_args, **_kwargs):
    return {"rt_cd": "0", "output": [
        {
            "odno": ODNO, "pdno": "CVNA", "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "74", "ft_ccld_qty": "74",
            "ft_ccld_unpr3": "65.03", "ord_tmd": "223038",
        },
        {
            "odno": SELL_ODNO, "pdno": "CVNA",
            "sll_buy_dvsn_cd": "01", "ft_ord_qty": "37",
            "ft_ccld_qty": "37", "ft_ccld_unpr3": "69.51",
            "ord_tmd": "001300",
        },
    ]}


def _positions_partial(*_args, **_kwargs):
    return [{"code": "CVNA", "qty": 37, "avg": 65.03}]


def _partial_exit_state() -> None:
    L.record_submit(SELL_KEY, "CVNA", 37, "half", meta={
        "side": "SELL", "market": "US", "excg": "NYSE",
        "price": 69.51, "hldg_before": 74, "pos_key": LEGACY_POS_KEY,
        "sleeve": "A", "fx": 1380.0, "ccy": "USD",
    })
    L.bind_broker_order(SELL_KEY, SELL_ODNO)
    L.on_result(SELL_KEY, "ack", 0, open_order=True)
    L.reconcile(SELL_KEY, 37, fill_price=69.51,
                fill_price_source="ccnl", open_order=False)
    L.record_reconcile_meta(
        SELL_KEY, reason="ccnl-filled",
        meta={"source": "ccnl", "side": "SELL", "intended": 37})
    sell_event = f"fill:{SELL_KEY}:SELL:37"
    C.add_lot(LEGACY_POS_KEY, "CVNA", 74, 65.03, fx=1380,
              sleeve="A", event_id=sell_event + ":legacy")
    C.close_lot(LEGACY_POS_KEY, 37, 69.51 * 37 * 1380,
                sleeve="A", day_kst="2026-08-20", event_id=sell_event)
    P.apply_sell_fill("CVNA", qty=37, price=69.51,
                      pos_key=LEGACY_POS_KEY, event_id=sell_event)
    P.mark_half_done("CVNA", event_id="half:CVNA:2026-08-20")
    P.raise_stop("CVNA", 65.03)
    L._append({"ev": "accounted", "key": SELL_KEY, "accounted": 37})


def _partial_plan(now: float = 1000.0, *, cost_krw: float = 6640863):
    with mock.patch.object(R.kis, "fills", side_effect=_ccnl_partial), \
            mock.patch.object(R.kis, "positions_detail",
                              side_effect=_positions_partial):
        return R.build_partial_exit_plan(
            order_key=KEY, odno=ODNO, sell_order_key=SELL_KEY,
            sell_odno=SELL_ODNO, symbol="CVNA", qty=74,
            fill_price=65.03, sell_qty=37, sell_fill_price=69.51,
            fx=1380, cost_krw=cost_krw, trade_date="20260818",
            sell_trade_date="20260820", sell_day_kst="2026-08-20",
            now=now)


def _apply_partial(plan: dict, backup: str, *, now: float = 1001.0):
    with mock.patch.object(R.kis, "fills", side_effect=_ccnl_partial), \
            mock.patch.object(R.kis, "positions_detail",
                              side_effect=_positions_partial), \
            mock.patch.object(R.legacy_migration, "_services_quiesced",
                              return_value=(True, "ok")):
        return R.apply_plan(
            plan, ack=f"APPLY {plan['plan_sha256']}", services_stopped=True,
            backup_dir=backup, now=now)


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


def test_accounting_recovery_pending_holds_budget_until_completion():
    """rejected 주문도 forensic apply 중에는 원래 예약액을 계속 잠근다."""
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        before = L._buy_reservation_costs(L._fold())
        assert before == (0.0, {}), before

        L._append({
            "ev": "migration_meta", "key": KEY,
            "meta": {
                "accounting_recovery_pending": True,
                "accounting_recovery_complete": False,
            },
        })
        during = L._buy_reservation_costs(L._fold())
        assert during == (6641190.384, {"A": 6641190.384}), during

        # pending 예약이 살아 있는 동안 1원짜리 후속 BUY도 같은 총한도에서
        # 전송 전 차단되어야 한다. 이 단언이 pending 분기 제거 mutation을 잡는다.
        candidate = {
            "side": "BUY", "market": "KR", "sleeve": "A",
            "price": 1, "fx": 1, "reservation_cost_krw": 1,
            "budget_total_held_krw": 0,
            "budget_total_limit_krw": 6641190.384,
            "budget_sleeve_held_krw": 0,
            "budget_sleeve_limit_krw": 6641190.384,
        }
        assert not L.try_record_submit(
            "recovery:blocked", "NEXT", 1, meta=candidate,
            min_interval_s=0)

        L._append({
            "ev": "migration_meta", "key": KEY,
            "meta": {
                "accounting_recovery_pending": False,
                "accounting_recovery_complete": True,
            },
        })
        after = L._buy_reservation_costs(L._fold())
        assert after == (0.0, {}), after
        assert L.try_record_submit(
            "recovery:released", "NEXT", 1, meta=candidate,
            min_interval_s=0)
    print("[PASS] recovery pending 동안 예약 유지 · 완료 뒤 해제")


def test_partial_exit_recovery_adopts_existing_economics_without_double_count():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        plan = _partial_plan()
        economic = plan["economic_accounting"]
        assert plan["final_qty"] == 37
        assert economic["mode"] == "preexisting-legacy-seed-close"
        assert abs(economic["seed_cost_krw"] - 6640863.6) < 1e-6
        assert round(economic["remaining_cost_krw"]) == 3320432
        assert round(economic["realized_pnl_krw"]) == 228749
        costbook_before = open(C._path(), "rb").read()

        result = _apply_partial(plan, os.path.join(tmp, "backup-partial"))
        assert result["ok"] and result["orders_sent"] == 0
        assert result["costbook_mutations"] == 0
        assert open(C._path(), "rb").read() == costbook_before
        buy = L.state_of(KEY)
        sell = L.state_of(SELL_KEY)
        assert buy["state"] == "filled" and buy["accounted"] == 74
        assert buy["accounting_recovery_complete"] is True
        assert sell["state"] == "filled" and sell["accounted"] == 37
        pos = P.load()["CVNA"]
        assert pos["qty"] == 37 and pos["pos_key"] == LEGACY_POS_KEY
        assert pos["half_done"] is True and pos["stop"] == 65.03
        assert pos["recovered_buy_qty"] == 74
        assert pos["recovered_sell_qty"] == 37
        lot = C._fold()["lots"][LEGACY_POS_KEY]
        assert lot["qty"] == 37 and round(lot["cost_krw"]) == 3320432
        history = H.snapshot()
        cvna = [row for row in history["trades"] if row["code"] == "CVNA"]
        assert [(row["side"], row["qty"]) for row in cvna] == [
            ("sell", 37), ("buy", 74)]
        assert all(row["verified"] is True for row in cvna)
        assert history["partial"] is False

        before_retry = {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        again = _apply_partial(
            plan, os.path.join(tmp, "unused-backup"), now=1002)
        assert again["already_applied"] is True
        assert before_retry == {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
    print("[PASS] 기존 74→37 경제장부 무변조 · BUY/잔여 정체성만 멱등 복구")


def test_partial_exit_plan_rejects_missing_or_distorted_sell_accounting():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        with open(C._path(), "a", encoding="utf-8") as fp:
            fp.write('{"ev":"close","key":"other","qty":1,'
                     '"event_id":"fill:xe:CVNA:half:#1:SELL:37"}\n')
        try:
            _partial_plan()
            raise AssertionError("중복 SELL event_id를 plan이 신뢰")
        except R.RecoveryRefused as exc:
            assert "중복" in str(exc)

    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        rows = []
        with open(C._path(), encoding="utf-8") as fp:
            for line in fp:
                event = json.loads(line)
                if event.get("ev") == "close":
                    event["realized_pnl_krw"] += 1
                rows.append(event)
        with open(C._path(), "w", encoding="utf-8") as fp:
            for event in rows:
                fp.write(json.dumps(event) + "\n")
        try:
            _partial_plan()
            raise AssertionError("왜곡된 SELL 손익을 plan이 신뢰")
        except R.RecoveryRefused as exc:
            assert "SELL 회계" in str(exc)
    print("[PASS] 부분매도 원문 event 중복·손익 왜곡은 plan 생성 전 거부")


def test_partial_exit_recovery_crash_retry_preserves_sell_and_costbook():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        plan = _partial_plan()
        costbook_before = open(C._path(), "rb").read()
        with mock.patch.object(P, "repair_buy_fill", side_effect=OSError("crash")), \
                mock.patch.object(R.kis, "fills", side_effect=_ccnl_partial), \
                mock.patch.object(R.kis, "positions_detail",
                                  side_effect=_positions_partial), \
                mock.patch.object(R.legacy_migration, "_services_quiesced",
                                  return_value=(True, "ok")):
            try:
                R.apply_plan(
                    plan, ack=f"APPLY {plan['plan_sha256']}",
                    services_stopped=True,
                    backup_dir=os.path.join(tmp, "backup-crash-partial"),
                    now=1001)
                raise AssertionError("crash injection did not fire")
            except OSError:
                pass
        assert open(C._path(), "rb").read() == costbook_before
        assert L.state_of(SELL_KEY)["accounted"] == 37
        result = _apply_partial(
            plan, os.path.join(tmp, "backup-retry-partial"), now=1002)
        assert result["ok"] and P.load()["CVNA"]["qty"] == 37
        assert C.open_qty("CVNA") == 37
        assert open(C._path(), "rb").read() == costbook_before
    print("[PASS] 잔여 포지션 교정 직전 크래시도 SELL·costbook 무변조 재시도")


def test_partial_exit_apply_rechecks_final_broker_quantity():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        plan = _partial_plan()
        before = {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        changed = lambda *_a, **_k: [
            {"code": "CVNA", "qty": 36, "avg": 65.03}]
        with mock.patch.object(R.kis, "fills", side_effect=_ccnl_partial), \
                mock.patch.object(R.kis, "positions_detail", side_effect=changed), \
                mock.patch.object(R.legacy_migration, "_services_quiesced",
                                  return_value=(True, "ok")):
            try:
                R.apply_plan(
                    plan, ack=f"APPLY {plan['plan_sha256']}",
                    services_stopped=True,
                    backup_dir=os.path.join(tmp, "must-not-exist"), now=1001)
                raise AssertionError("plan 뒤 잔고 변화를 apply가 무시")
            except R.RecoveryRefused:
                pass
        assert before == {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        assert not os.path.exists(os.path.join(tmp, "must-not-exist"))
    print("[PASS] plan 이후 37주 잔고 변화는 백업·mutation 전 fail-closed")


def test_partial_exit_plan_rejects_sell_filled_but_unaccounted():
    """SELL close가 있어도 ledger handoff 미완이면 BUY 복구를 시작하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        L._append({"ev": "accounted", "key": SELL_KEY, "accounted": 0})
        before = {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        try:
            _partial_plan()
            raise AssertionError("SELL accounted=0인데 복구 plan 생성")
        except R.RecoveryRefused as exc:
            assert "SELL 체결/회계" in str(exc)
        assert before == {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
    print("[PASS] SELL filled=37/accounted=0은 plan 생성 전 무변조 거부")


def test_partial_exit_plan_rejects_exact_one_won_rounding_delta():
    """운영자 정수 표기 허용은 |durable-requested| < 1원으로 고정한다."""
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        durable = 65.03 * 74 * 1380
        try:
            _partial_plan(cost_krw=durable - 1.0)
            raise AssertionError("원가 차이 정확히 1원인데 plan 생성")
        except R.RecoveryRefused as exc:
            assert "legacy BUY seed" in str(exc)
    print("[PASS] durable 원가 차이 Δ=1.0원 경계는 plan 거부")


def test_partial_exit_apply_rechecks_broker_again_after_backup():
    """첫 조회 뒤 움직인 잔고를 백업 후 두 번째 fresh 조회가 잡아야 한다."""
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _env(stack, tmp)
        _partial_exit_state()
        plan = _partial_plan()
        before = {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
        position_calls = 0

        def changed_after_first_proof(*_args, **_kwargs):
            nonlocal position_calls
            position_calls += 1
            qty = 37 if position_calls <= len(R.US_EXCGS) else 36
            return [{"code": "CVNA", "qty": qty, "avg": 65.03}]

        backup = os.path.join(tmp, "backup-before-second-proof")
        with mock.patch.object(R.kis, "fills", side_effect=_ccnl_partial), \
                mock.patch.object(
                    R.kis, "positions_detail",
                    side_effect=changed_after_first_proof), \
                mock.patch.object(R.legacy_migration, "_services_quiesced",
                                  return_value=(True, "ok")):
            try:
                R.apply_plan(
                    plan, ack=f"APPLY {plan['plan_sha256']}",
                    services_stopped=True, backup_dir=backup, now=1001)
                raise AssertionError("백업 뒤 잔고 변경을 2차 조회가 놓침")
            except R.RecoveryRefused as exc:
                assert "KIS 잔고 불일치" in str(exc)
        assert position_calls > len(R.US_EXCGS)  # 1차 통과 뒤 2차 진입 증명
        assert os.path.isfile(os.path.join(backup, "manifest.json"))
        assert before == {
            path: open(path, "rb").read()
            for path in (L.LEDGER_PATH, P.PATH, C._path())
        }
    print("[PASS] 1차 37주→백업 후 36주는 2차 fresh 조회에서 무변조 거부")


def main():
    test_exact_cost_and_absolute_position_are_idempotent()
    test_crash_after_costbook_recovers_without_duplicate_lot_or_qty()
    test_plan_requires_fresh_exact_broker_truth_and_unowned_symbol()
    test_accounting_recovery_pending_holds_budget_until_completion()
    test_partial_exit_recovery_adopts_existing_economics_without_double_count()
    test_partial_exit_plan_rejects_missing_or_distorted_sell_accounting()
    test_partial_exit_recovery_crash_retry_preserves_sell_and_costbook()
    test_partial_exit_apply_rechecks_final_broker_quantity()
    test_partial_exit_plan_rejects_sell_filled_but_unaccounted()
    test_partial_exit_plan_rejects_exact_one_won_rounding_delta()
    test_partial_exit_apply_rechecks_broker_again_after_backup()
    print("\n모든 forensic 회계 복구 테스트 통과.")


if __name__ == "__main__":
    main()
