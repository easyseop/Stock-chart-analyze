"""legacy BUY 장부 이관의 fail-closed·멱등·부분청산 복구 검증."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest import mock

from bot import (
    costbook as C,
    kill as K,
    kis as KIS,
    kis_positions as P,
    ledger as L,
    legacy_migration as M,
    ownership as O,
    trade_history as H,
)


def _paths(stack: ExitStack, tmp: str) -> None:
    stack.enter_context(mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")))
    stack.enter_context(mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")))
    stack.enter_context(mock.patch.object(K, "LOG_PATH", os.path.join(tmp, "kill-log.jsonl")))
    stack.enter_context(mock.patch.object(KIS, "ENV", "mock"))
    stack.enter_context(mock.patch.dict(os.environ, {
        "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
        "USER_BASELINE_PATH": os.path.join(tmp, "baseline.json"),
        "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
        "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
    }))
    with open(os.environ["KILL_STATE_PATH"], "w", encoding="utf-8") as f:
        json.dump({"level": 1}, f)


def _open(code: str, qty: int, *, opened: str = "2026-07-20") -> None:
    P.record(
        code, qty=qty, entry=100.0, stop=90.0, ccy="USD",
        name=code, opened=opened, sleeve="A", pos_key="",
    )


def _buy(key: str, code: str, qty: int) -> None:
    L.record_submit(
        key, code, qty, "legacy buy",
        meta={"side": "BUY", "market": "US", "excg": "NASD",
              "price": 100.0, "hldg_before": 0},
    )
    L.on_result(
        key, "filled", qty, fill_price=100.0,
        fill_price_source="legacy", open_order=False,
    )


def _sell(key: str, code: str, qty: int, *, ack: bool,
          opened: str = "2026-07-20", price: float = 110.0,
          fill_price: float | None = None,
          fill_price_source: str = "legacy",
          submitted_at: float | None = None) -> None:
    meta = {
        "side": "SELL", "market": "US", "excg": "NASD",
        # 실제 구버전 버그: 절반매도에서 전체 보유(10)가 아니라 주문수량을 저장.
        "price": price, "hldg_before": qty,
    }
    if ack:
        meta.update({
            "pos_key": f"legacy:A:{code}:{opened}",
            "sleeve": "A", "fx": 1380.0, "ccy": "USD",
        })
    L._append({
        "ev": "submit", "key": key, "symbol": code,
        "intended": qty, "filled": 0, "state": "submitted",
        "reason": "legacy sell", "meta": meta,
        "ts": time.time() if submitted_at is None else float(submitted_at),
    })
    if ack:
        L.on_result(key, "ack", 0)
    else:
        L.on_result(
            key, "filled", qty,
            fill_price=price if fill_price is None else fill_price,
            fill_price_source=fill_price_source, open_order=False,
        )


def _fixture() -> dict[str, dict]:
    _open("AAPL", 10)
    _buy("buy:aapl", "AAPL", 10)

    _open("CAG", 10)
    _buy("buy:cag", "CAG", 10)
    _sell("sell:cag", "CAG", 5, ack=True, price=110.0)

    _open("BAM", 10)
    _buy("buy:bam", "BAM", 10)
    _sell(
        "sell:bam", "BAM", 10, ack=False, price=80.0,
        fill_price=100.0, fill_price_source="balance-average",
        submitted_at=1704153600.0,
    )
    P.close("BAM")

    O.capture_baseline([
        {"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "CAG"}, {"ovrs_pdno": "BAM"},
    ])
    return {
        "AAPL": {"qty": 10, "avg": 100.0, "market": "US"},
        "BAM": {"qty": 0, "avg": 0.0, "market": "US"},
        "CAG": {"qty": 5, "avg": 100.0, "market": "US"},
    }


def test_plan_is_read_only_and_apply_reconstructs_all_three_shapes():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        snapshot = _fixture()
        # 이관 대상이 아닌 같은 시장 ACK. 후보 전용 hmap에서 누락된 종목을
        # now=0으로 간주해 실수로 filled 처리하면 안 된다.
        L.record_submit(
            "sell:unrelated", "MSFT", 5, "unrelated open ack",
            meta={
                "side": "SELL", "market": "US", "excg": "NASD",
                "price": 200.0, "hldg_before": 5,
                "pos_key": "unrelated:MSFT", "sleeve": "A",
                "fx": 1380.0, "ccy": "USD",
            },
        )
        L.on_result("sell:unrelated", "ack", 0)
        before_sizes = {
            "ledger": os.path.getsize(L.LEDGER_PATH),
            "positions": os.path.getsize(P.PATH),
        }
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        assert len(plan["entries"]) == 3
        bam_plan = next(row for row in plan["entries"]
                        if row["symbol"] == "BAM")
        assert bam_plan["sell_price"] == 80.0
        assert bam_plan["sell_price_source"] == "submitted-fallback"
        assert bam_plan["sell_day_kst"] == "2024-01-02"
        assert plan["safety"]["orders_sent"] is False
        assert plan["journal_paths"] == M._journal_paths()
        assert os.path.getsize(L.LEDGER_PATH) == before_sizes["ledger"]
        assert os.path.getsize(P.PATH) == before_sizes["positions"]
        assert C.open_qty("AAPL") == 0

        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            try:
                M.apply_plan(
                    plan, ack="wrong", services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-wrong"),
                )
                raise AssertionError("wrong ack가 거부되지 않음")
            except M.MigrationRefused:
                pass
        assert C.open_qty("AAPL") == 0

        ack = f"APPLY {plan['plan_sha256']}"
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            result = M.apply_plan(
                plan, ack=ack, services_stopped=True, now=stamp + 1,
                backup_dir=os.path.join(tmp, "backup-1"),
            )
        assert result["ok"] and result["orders_sent"] == 0 and result["count"] == 3
        assert C.open_qty("AAPL") == 10
        assert C.open_qty("CAG") == 5
        assert C.open_qty("BAM") == 0
        assert P.load()["AAPL"]["qty"] == 10
        assert P.load()["CAG"]["qty"] == 5
        assert "BAM" not in P.load()
        assert L.state_of("sell:cag")["state"] == "filled"
        assert L.state_of("sell:cag")["fill_price"] == 110.0
        assert L.state_of("buy:cag")["accounted"] == 10
        assert L.state_of("buy:bam")["accounted"] == 10
        assert L.state_of("sell:unrelated")["state"] == "ack"
        cag_event = C._fold()["event_results"]["fill:sell:cag:SELL:5"]
        assert cag_event["pnl"] == 5 * (110.0 - 100.0) * 1380.0
        bam_event = C._fold()["event_results"]["fill:sell:bam:SELL:10"]
        assert bam_event["pnl"] == 10 * (80.0 - 100.0) * 1380.0
        assert C._fold()["daily_realized"]["2024-01-02"] == bam_event["pnl"]
        with open(
                os.path.join(tmp, "backup-1", "manifest.json"),
                encoding="utf-8") as fp:
            backup_manifest = json.load(fp)
        assert set(backup_manifest["files"]) == {
            "order_ledger.jsonl", "kis_positions.jsonl", "costbook.jsonl",
        }
        assert backup_manifest["files"]["costbook.jsonl"]["exists"] is False
        for label, source in {
            "order_ledger.jsonl": L.LEDGER_PATH,
            "kis_positions.jsonl": P.PATH,
        }.items():
            copied = os.path.join(tmp, "backup-1", label)
            assert os.stat(copied).st_mode & 0o777 == 0o600
            with open(source, "rb") as left, open(copied, "rb") as right:
                current_bytes, backup_bytes = left.read(), right.read()
            assert current_bytes != backup_bytes  # apply 뒤 원장은 append돼야 함
            assert hashlib.sha256(backup_bytes).hexdigest() \
                == backup_manifest["files"][label]["sha256"]
            assert len(backup_bytes) == backup_manifest["files"][label]["size"]
            assert backup_manifest["files"][label]["size"] > 0
        manifest_path = os.path.join(tmp, "backup-1", "manifest.json")
        assert os.stat(manifest_path).st_mode & 0o777 == 0o600
        history = H.snapshot()
        assert history["summary"]["buy_fills"] == 3
        assert history["summary"]["sell_fills"] == 2
        legacy_buys = [row for row in history["trades"]
                       if row["side"] == "buy"]
        assert {row["code"] for row in legacy_buys} == {"AAPL", "BAM", "CAG"}
        assert all(row["price_estimated"] and not row["verified"]
                   for row in legacy_buys)
        cag_sale = next(row for row in history["trades"]
                        if row["side"] == "sell" and row["code"] == "CAG")
        assert cag_sale["price_estimated"] is True
        assert cag_sale["fill_price_source"] == "submitted-fallback"
        bam_sale = next(row for row in history["trades"]
                        if row["side"] == "sell" and row["code"] == "BAM")
        assert bam_sale["exit_price"] == 80.0
        assert bam_sale["price_estimated"] is True
        assert bam_sale["verified"] is False
        assert bam_sale["fill_price_source"] == "submitted-fallback"

        sizes = (
            os.path.getsize(L.LEDGER_PATH),
            os.path.getsize(P.PATH),
            os.path.getsize(os.environ["COSTBOOK_PATH"]),
        )
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            again = M.apply_plan(
                plan, ack=ack, services_stopped=True, now=stamp + 2,
                backup_dir=os.path.join(tmp, "backup-2"),
            )
        assert again["ok"] and sizes == (
            os.path.getsize(L.LEDGER_PATH),
            os.path.getsize(P.PATH),
            os.path.getsize(os.environ["COSTBOOK_PATH"]),
        )
    print("[PASS] plan 무변경·3형태 복구·SELL 제출가 손익·재실행 byte 멱등")


def test_crash_before_buy_accounted_is_resumable_and_reservation_stays():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("AAPL", 10)
        _buy("buy:aapl", "AAPL", 10)
        O.capture_baseline([{"ovrs_pdno": "AAPL"}])
        snapshot = {
            "AAPL": {"qty": 10, "avg": 100.0, "market": "US"},
        }
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")), \
             mock.patch.object(L, "mark_accounted",
                               side_effect=OSError("fault before accounted")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-crash-1"),
                )
                raise AssertionError("fault injection이 발생하지 않음")
            except OSError:
                pass
        assert C.open_qty("AAPL") == 10
        assert P.load()["AAPL"]["qty"] == 10
        assert int(L.state_of("buy:aapl").get("accounted") or 0) == 0

        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            out = M.apply_plan(
                plan, ack=ack, services_stopped=True, now=stamp + 2,
                backup_dir=os.path.join(tmp, "backup-crash-2"),
            )
        assert out["ok"] and L.state_of("buy:aapl")["accounted"] == 10
        assert C.open_qty("AAPL") == 10 and P.load()["AAPL"]["qty"] == 10
    print("[PASS] accounted 직전 crash → 예약 유지·동일 plan 멱등 복구")


def test_crash_after_ack_reconcile_resumes_sell_accounting():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("CAG", 10)
        _buy("buy:cag", "CAG", 10)
        _sell("sell:cag", "CAG", 5, ack=True, price=110.0)
        O.capture_baseline([{"ovrs_pdno": "CAG"}])
        snapshot = {
            "CAG": {"qty": 5, "avg": 100.0, "market": "US"},
        }
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")), \
             mock.patch.object(
                 M.kis_accounting, "sync_fill",
                 side_effect=OSError("fault after reconcile")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-ack-crash-1"),
                )
                raise AssertionError("ACK reconcile fault가 발생하지 않음")
            except OSError:
                pass
        assert L.state_of("sell:cag")["state"] == "filled"
        assert int(L.state_of("sell:cag").get("accounted") or 0) == 0
        assert C.open_qty("CAG") == 10 and P.load()["CAG"]["qty"] == 10
        assert int(L.state_of("buy:cag").get("accounted") or 0) == 0

        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            result = M.apply_plan(
                plan, ack=ack, services_stopped=True, now=stamp + 2,
                backup_dir=os.path.join(tmp, "backup-ack-crash-2"),
            )
        assert result["ok"]
        assert L.state_of("sell:cag")["accounted"] == 5
        assert L.state_of("buy:cag")["accounted"] == 10
        assert C.open_qty("CAG") == 5 and P.load()["CAG"]["qty"] == 5
    print("[PASS] ACK reconcile 직후 crash → 동일 plan SELL 회계 복구")


def test_crash_after_sell_costbook_close_does_not_reseed_phantom_lot():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("BAM", 10)
        _buy("buy:bam", "BAM", 10)
        _sell(
            "sell:bam", "BAM", 10, ack=False, price=80.0,
            fill_price=100.0, fill_price_source="balance-average",
            submitted_at=1704153600.0,
        )
        P.close("BAM")
        O.capture_baseline([{"ovrs_pdno": "BAM"}])
        snapshot = {"BAM": {"qty": 0, "avg": 0.0, "market": "US"}}
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"

        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")), \
             mock.patch.object(
                 P, "apply_sell_fill",
                 side_effect=OSError("fault after durable close")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-sell-crash-1"),
                )
                raise AssertionError("SELL close 이후 fault가 발생하지 않음")
            except OSError:
                pass

        folded = C._fold()
        original_cost = 10 * 100.0 * 1380.0
        assert folded["totals"]["buy_cost"] == original_cost
        assert C.open_qty("BAM") == 0
        assert "fill:sell:bam:SELL:10" in folded["event_results"]
        assert int(L.state_of("sell:bam").get("accounted") or 0) == 0

        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            out = M.apply_plan(
                plan, ack=ack, services_stopped=True, now=stamp + 2,
                backup_dir=os.path.join(tmp, "backup-sell-crash-2"),
            )
        folded = C._fold()
        assert out["ok"] and C.open_qty("BAM") == 0
        assert folded["totals"]["buy_cost"] == original_cost
        assert folded["totals"]["sell_proceeds"] == 10 * 80.0 * 1380.0
        assert int(L.state_of("sell:bam").get("accounted") or 0) == 10
        assert "BAM" not in P.load()
    print("[PASS] SELL close→position 사이 crash 재실행에도 팬텀 lot·손익 중복 0")


def test_apply_rechecks_paths_positions_and_post_backup_quiescence():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("AAPL", 10)
        _buy("buy:aapl", "AAPL", 10)
        O.capture_baseline([{"ovrs_pdno": "AAPL"}])
        snapshot = {"AAPL": {"qty": 10, "avg": 100.0, "market": "US"}}
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"

        with mock.patch.object(
                L, "LEDGER_PATH", os.path.join(tmp, "wrong-cwd-ledger.jsonl")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-path-mismatch"),
                )
                raise AssertionError("원장 절대경로 불일치가 거부되지 않음")
            except M.MigrationRefused as exc:
                assert "절대경로 불일치" in str(exc)

        with open(P.PATH, "a", encoding="utf-8") as fp:
            fp.write("{broken-position-json\n")
        corrupt_backup = os.path.join(tmp, "backup-position-corrupt")
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=corrupt_backup,
                )
                raise AssertionError("apply 직전 positions 손상이 거부되지 않음")
            except M.MigrationRefused as exc:
                assert "kis_positions 손상" in str(exc)
        assert not os.path.exists(corrupt_backup)

    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("AAPL", 10)
        _buy("buy:aapl", "AAPL", 10)
        O.capture_baseline([{"ovrs_pdno": "AAPL"}])
        snapshot = {"AAPL": {"qty": 10, "avg": 100.0, "market": "US"}}
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"
        backup_dir = os.path.join(tmp, "backup-process-reappeared")
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(
                 M, "_services_quiesced",
                 side_effect=[(True, "ok"), (False, "manual process")]):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=backup_dir,
                )
                raise AssertionError("백업 후 서비스 재등장이 거부되지 않음")
            except M.MigrationRefused as exc:
                assert "백업 후 주문 서비스 재등장" in str(exc)
        assert os.path.isdir(backup_dir)            # forensic 백업은 보존
        assert C.open_qty("AAPL") == 0
        assert int(L.state_of("buy:aapl").get("accounted") or 0) == 0
    print("[PASS] 원장경로·positions 손상·백업 후 프로세스 TOCTOU 전부 mutation 전 차단")


def test_fail_closed_on_qty_overflow_snapshot_change_and_running_services():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("AAPL", 10)
        _buy("buy:aapl", "AAPL", 10)
        O.capture_baseline([{"ovrs_pdno": "AAPL"}])
        overflow = {
            "AAPL": {"qty": 11, "avg": 100.0, "market": "US"},
        }
        with mock.patch.object(M, "_broker_snapshot", return_value=overflow):
            try:
                M.build_plan()
                raise AssertionError("qty overflow가 거부되지 않음")
            except M.MigrationRefused:
                pass

        good = {"AAPL": {"qty": 10, "avg": 100.0, "market": "US"}}
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=good):
            plan = M.build_plan(now=stamp)
        ack = f"APPLY {plan['plan_sha256']}"
        with mock.patch.object(M, "_broker_snapshot", return_value=good):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=False, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-running"),
                )
                raise AssertionError("running services가 거부되지 않음")
            except M.MigrationRefused:
                pass
        tampered = json.loads(json.dumps(plan))
        tampered["entries"][0]["current_qty"] = 9
        with mock.patch.object(M, "_broker_snapshot", return_value=good):
            try:
                M.apply_plan(
                    tampered, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-tampered"),
                )
                raise AssertionError("tampered plan이 거부되지 않음")
            except M.MigrationRefused:
                pass
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True,
                    now=stamp + M.PLAN_MAX_AGE_S + 1,
                    backup_dir=os.path.join(tmp, "backup-expired"),
                )
                raise AssertionError("expired plan이 거부되지 않음")
            except M.MigrationRefused:
                pass
        changed = {"AAPL": {"qty": 9, "avg": 100.0, "market": "US"}}
        with mock.patch.object(M, "_broker_snapshot", return_value=changed), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            try:
                M.apply_plan(
                    plan, ack=ack, services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-changed"),
                )
                raise AssertionError("snapshot change가 거부되지 않음")
            except M.MigrationRefused:
                pass
        assert C.open_qty("AAPL") == 0
        assert int(L.state_of("buy:aapl").get("accounted") or 0) == 0

        nonfinite = json.loads(json.dumps(plan))
        nonfinite["expires_at"] = float("inf")
        nonfinite["plan_sha256"] = M.plan_hash(nonfinite)
        with mock.patch.object(M, "_broker_snapshot", return_value=good):
            try:
                M.apply_plan(
                    nonfinite,
                    ack=f"APPLY {nonfinite['plan_sha256']}",
                    services_stopped=True, now=stamp + 1,
                    backup_dir=os.path.join(tmp, "backup-nonfinite-time"),
                )
                raise AssertionError("Infinity timestamp가 거부되지 않음")
            except M.MigrationRefused:
                pass

        P.record(
            "AAPL", qty=10, entry=100.0, stop=float("nan"), ccy="USD",
            name="AAPL", opened="2026-07-20", sleeve="A",
        )
        with mock.patch.object(M, "_broker_snapshot", return_value=good):
            try:
                M.build_plan()
                raise AssertionError("NaN stop이 거부되지 않음")
            except M.MigrationRefused:
                pass
    print("[PASS] qty 초과·서비스 미정지·plan 변조/만료·snapshot 변경 fail-closed")


def test_migration_import_graph_has_no_order_plane():
    forbidden = {"bot.kis_orders", "bot.kis_buy", "bot.sentinel"}
    assert not (forbidden & set(sys.modules))
    source = open(M.__file__, encoding="utf-8").read()
    assert all(f"import {name}" not in source for name in forbidden)
    print("[PASS] legacy migration import graph에 주문 전송 모듈 없음")


def test_services_quiesced_requires_every_unit_inactive():
    def _proc(state: str, returncode: int = 0):
        return mock.Mock(stdout=state + "\n", returncode=returncode)

    with mock.patch.object(M.heartbeat, "age_s",
                           return_value=M.heartbeat.AGE_HARD_S + 1), \
         mock.patch.object(
             M.subprocess, "run",
             side_effect=[
                 _proc("inactive"), _proc("masked-runtime"),
                 _proc("inactive"), _proc("masked"),
                 _proc("", returncode=1),
             ]) as run:
        assert M._services_quiesced() == (True, "ok")
        assert [call.args[0][1] for call in run.call_args_list] == [
            "is-active", "is-enabled", "is-active", "is-enabled", "-f",
        ]

    with mock.patch.object(
            M.subprocess, "run",
            side_effect=[
                _proc("inactive"), _proc("masked"),
                _proc("active"),
            ]):
        ok, why = M._services_quiesced()
    assert ok is False and "buyloop.service=active" in why

    with mock.patch.object(
            M.subprocess, "run",
            side_effect=[_proc("inactive"), _proc("enabled")]):
        ok, why = M._services_quiesced()
    assert ok is False and "mask --runtime" in why

    with mock.patch.object(
            M.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("systemctl", 3)):
        ok, why = M._services_quiesced()
    assert ok is False and "상태 조회 실패" in why

    with mock.patch.object(M.heartbeat, "age_s",
                           return_value=M.heartbeat.AGE_HARD_S + 1), \
         mock.patch.object(
             M.subprocess, "run",
             side_effect=[
                 _proc("inactive"), _proc("masked"),
                 _proc("inactive"), _proc("masked"),
                 _proc("1234"),
             ]):
        ok, why = M._services_quiesced()
    assert ok is False and "systemd 밖" in why

    with mock.patch.object(M.heartbeat, "age_s", return_value=10.0), \
         mock.patch.object(
             M.subprocess, "run",
             side_effect=[
                 _proc("inactive"), _proc("masked"),
                 _proc("inactive"), _proc("masked"),
                 _proc("", returncode=1),
             ]):
        ok, why = M._services_quiesced()
    assert ok is False and "heartbeat가 아직 신선함" in why
    print("[PASS] inactive+mask·수동프로세스0·heartbeat stale만 apply 허용")


def main():
    test_plan_is_read_only_and_apply_reconstructs_all_three_shapes()
    test_crash_before_buy_accounted_is_resumable_and_reservation_stays()
    test_crash_after_ack_reconcile_resumes_sell_accounting()
    test_crash_after_sell_costbook_close_does_not_reseed_phantom_lot()
    test_apply_rechecks_paths_positions_and_post_backup_quiescence()
    test_fail_closed_on_qty_overflow_snapshot_change_and_running_services()
    test_migration_import_graph_has_no_order_plane()
    test_services_quiesced_requires_every_unit_inactive()
    print("\nlegacy migration 검증 통과.")


if __name__ == "__main__":
    main()
