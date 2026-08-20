"""legacy BUY 장부 이관의 fail-closed·멱등·부분청산 복구 검증."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
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
    stack.enter_context(mock.patch.object(
        M.alpha, "STATE_PATH", os.path.join(tmp, "alpha_state.json")))
    stack.enter_context(mock.patch.object(M.alpha, "publish_dash"))
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
        M.alpha._save({
            "day": {"US": {"series": [["23:30", -16.4, -.2]]}},
            "days": [{"d": "2026-07-28", "mkt": "US",
                      "acct": -16.4, "idx": -.2}],
        })
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
        assert result["performance_rebase"]["rebased"] is True
        rebased = M.alpha._load()
        assert rebased["day"] == {} and rebased["days"] == []
        assert rebased["performance_epoch"]["id"] == plan["plan_sha256"]
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
            "alpha_state.json",
        }
        assert backup_manifest["files"]["costbook.jsonl"]["exists"] is False
        assert backup_manifest["files"]["alpha_state.json"]["exists"] is True
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
        assert again["performance_rebase"]["already_applied"] is True
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


def test_expired_plan_can_recover_only_completed_performance_rebase():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        _open("AAPL", 10)
        _buy("buy:aapl", "AAPL", 10)
        O.capture_baseline([{"ovrs_pdno": "AAPL"}])
        M.alpha._save({
            "day": {"US": {"series": [["23:30", -17.0, 0.0]]}},
            "days": [{"d": "2026-07-28", "mkt": "US",
                      "acct": -17.0, "idx": 0.0}],
        })
        snapshot = {"AAPL": {"qty": 10, "avg": 100.0, "market": "US"}}
        stamp = time.time()
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot):
            plan = M.build_plan(now=stamp)
        apply_ack = f"APPLY {plan['plan_sha256']}"
        backup_dir = os.path.join(tmp, "backup-rebase-crash")
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")), \
             mock.patch.object(
                 M.alpha, "rebase_after_accounting_migration",
                 side_effect=OSError("fault after all accounting")):
            try:
                M.apply_plan(
                    plan, ack=apply_ack, services_stopped=True,
                    now=stamp + 1, backup_dir=backup_dir,
                )
                raise AssertionError("성과 리베이스 fault가 발생하지 않음")
            except OSError:
                pass
        assert L.state_of("buy:aapl")["accounted"] == 10
        assert C.open_qty("AAPL") == 10 and P.load()["AAPL"]["qty"] == 10
        assert not (M.alpha._load().get("performance_epoch") or {}).get("id")

        # 기존 백업을 바꾼 경로로는 alpha만이라도 절대 변경하지 않는다.
        tampered_backup = os.path.join(tmp, "backup-tampered-copy")
        shutil.copytree(backup_dir, tampered_backup)
        with open(
                os.path.join(tampered_backup, "order_ledger.jsonl"),
                "ab") as fp:
            fp.write(b"tamper")
        recover_ack = f"RECOVER {plan['plan_sha256']}"
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            try:
                M.recover_performance_rebase(
                    plan, ack=recover_ack, services_stopped=True,
                    backup_dir=tampered_backup,
                    now=stamp + M.PLAN_MAX_AGE_S + 10,
                )
                raise AssertionError("손상 backup 복구가 거부되지 않음")
            except M.MigrationRefused as exc:
                assert "backup 무결성 불일치" in str(exc)
        assert not (M.alpha._load().get("performance_epoch") or {}).get("id")

        journal_sizes = (
            os.path.getsize(L.LEDGER_PATH),
            os.path.getsize(P.PATH),
            os.path.getsize(os.environ["COSTBOOK_PATH"]),
        )
        with mock.patch.object(M, "_broker_snapshot", return_value=snapshot), \
             mock.patch.object(M, "_services_quiesced",
                               return_value=(True, "ok")):
            recovered = M.recover_performance_rebase(
                plan, ack=recover_ack, services_stopped=True,
                backup_dir=backup_dir,
                now=stamp + M.PLAN_MAX_AGE_S + 10,
            )
            again = M.recover_performance_rebase(
                plan, ack=recover_ack, services_stopped=True,
                backup_dir=backup_dir,
                now=stamp + M.PLAN_MAX_AGE_S + 20,
            )
        assert recovered["ok"] and recovered["orders_sent"] == 0
        assert recovered["performance_rebase"]["rebased"] is True
        assert again["performance_rebase"]["already_applied"] is True
        assert M.alpha._load()["performance_epoch"]["id"] == plan["plan_sha256"]
        assert journal_sizes == (
            os.path.getsize(L.LEDGER_PATH),
            os.path.getsize(P.PATH),
            os.path.getsize(os.environ["COSTBOOK_PATH"]),
        )
    print("[PASS] 회계완료→리베이스 crash를 만료 plan+원본 backup으로만 멱등 복구")


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
    """정지 증명 두 갈래(2026-08-20 수정) — mask 또는 부활주체 전원 정지.

    실측 결함: /etc 실파일 유닛에는 `mask --runtime`이 우선순위에 가려 무효라
    masked-runtime에 도달할 수 없었다. 유닛별로 ①mask ②guardian 전원 inactive
    중 하나면 통과한다. is-active·pgrep·heartbeat 요구는 불변.
    """
    def fake_run_factory(active_map, enabled_map, pgrep_rc=1):
        def fake_run(cmd, **kw):
            if cmd[0] == "pgrep":
                return mock.Mock(stdout="", returncode=pgrep_rc)
            sub, unit = cmd[1], cmd[2]
            if sub == "is-active":
                return mock.Mock(stdout=active_map.get(unit, "inactive") + "\n",
                                 returncode=0)
            return mock.Mock(stdout=enabled_map.get(unit, "enabled") + "\n",
                             returncode=0)
        return fake_run

    hb = mock.patch.object(M.heartbeat, "age_s",
                           return_value=M.heartbeat.AGE_HARD_S + 1)
    # ① mask 증명 — guardian이 살아 있어도 masked면 통과(기존 동작 유지).
    with hb, mock.patch.object(M.subprocess, "run", fake_run_factory(
            {"watchdog.service": "active"},
            {"sentinel.service": "masked-runtime", "buyloop.service": "masked"})):
        assert M._services_quiesced() == (True, "ok")
    # ② guardian 정지 증명 — mask 불가 배치는 **disabled**(부팅 링크 절단)까지
    #    요구한다(Codex 역검토 P1: enabled면 재부팅 시 multi-user.target이 재기동).
    disabled_units = {"sentinel.service": "disabled", "buyloop.service": "disabled"}
    with hb, mock.patch.object(M.subprocess, "run",
                               fake_run_factory({}, dict(disabled_units))):
        assert M._services_quiesced() == (True, "ok")
    # P1 반례 고정: enabled + guardian 전원 inactive → 거부.
    with hb, mock.patch.object(M.subprocess, "run", fake_run_factory({}, {})):
        ok, why = M._services_quiesced()
    assert ok is False and "disable --now" in why, why
    # guardian 하나라도 살아 있으면(=재기동 가능) enabled 상태는 거부.
    for guard in ("watchdog.service", "autodeploy.service", "autodeploy.timer"):
        with hb, mock.patch.object(M.subprocess, "run", fake_run_factory(
                {guard: "active"}, dict(disabled_units))):
            ok, why = M._services_quiesced()
        assert ok is False and guard in why, (guard, why)
    # inactive 외 전 상태 거부 — deactivating(종료 경합)·failed·unknown 포함.
    for bad in ("activating", "deactivating", "failed", "unknown"):
        with hb, mock.patch.object(M.subprocess, "run", fake_run_factory(
                {"autodeploy.timer": bad}, dict(disabled_units))):
            ok, why = M._services_quiesced()
        assert ok is False and bad in why, (bad, why)
    # 주문 유닛 자체가 active면 어느 갈래로도 통과 불가.
    with hb, mock.patch.object(M.subprocess, "run", fake_run_factory(
            {"buyloop.service": "active"}, dict(disabled_units))):
        ok, why = M._services_quiesced()
    assert ok is False and "buyloop.service=active" in why
    # guardian 조회 실패는 fail-closed — mask 없으면 거부.
    def broken_guardian(cmd, **kw):
        if cmd[0] == "pgrep":
            return mock.Mock(stdout="", returncode=1)
        if cmd[1] == "is-active" and cmd[2] == "watchdog.service":
            raise OSError("query down")
        sub = cmd[1]
        return mock.Mock(stdout=("inactive" if sub == "is-active"
                                 else "disabled") + "\n", returncode=0)
    with hb, mock.patch.object(M.subprocess, "run", broken_guardian):
        ok, why = M._services_quiesced()
    assert ok is False and "정지 필요" in why
    # 수동 프로세스 발견 시 거부(기존 계약 유지).
    with hb, mock.patch.object(M.subprocess, "run", fake_run_factory(
            {}, dict(disabled_units), pgrep_rc=0)):
        fake = fake_run_factory({}, dict(disabled_units), pgrep_rc=0)
        def with_pid(cmd, **kw):
            r = fake(cmd, **kw)
            if cmd[0] == "pgrep":
                return mock.Mock(stdout="1234\n", returncode=0)
            return r
        with mock.patch.object(M.subprocess, "run", with_pid):
            ok, why = M._services_quiesced()
    assert ok is False and "systemd 밖" in why
def main():
    test_plan_is_read_only_and_apply_reconstructs_all_three_shapes()
    test_crash_before_buy_accounted_is_resumable_and_reservation_stays()
    test_crash_after_ack_reconcile_resumes_sell_accounting()
    test_crash_after_sell_costbook_close_does_not_reseed_phantom_lot()
    test_expired_plan_can_recover_only_completed_performance_rebase()
    test_apply_rechecks_paths_positions_and_post_backup_quiescence()
    test_fail_closed_on_qty_overflow_snapshot_change_and_running_services()
    test_migration_import_graph_has_no_order_plane()
    test_services_quiesced_requires_every_unit_inactive()
    print("\nlegacy migration 검증 통과.")


if __name__ == "__main__":
    main()
