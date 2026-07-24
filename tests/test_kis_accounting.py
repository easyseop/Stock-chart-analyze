"""체결 기반 회계 — ack 미반영·부분체결 증가분·매도 실현손익·슬리브 분리."""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import costbook as C, kis_accounting as A, kis_positions as P
from bot import kis_reconcile as R, ledger as L


def _meta(side="BUY", sleeve="A"):
    return {"side": side, "market": "US", "price": 100.0, "pos_key": "pos:AAPL",
            "sleeve": sleeve, "fx": 1400.0, "ccy": "USD", "stop": 90.0,
            "target": 130.0, "name": "Apple", "opened": "2026-07-24"}


def test_partial_fill_then_final_and_sell():
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")), \
         mock.patch.dict(os.environ, {"COSTBOOK_PATH": os.path.join(tmp, "cost.jsonl")}):
        L.record_submit("buy:1", "AAPL", 10, meta=_meta())
        L.bind_broker_order("buy:1", "OD1")
        L.on_result("buy:1", "ack", 0)
        assert C.open_qty("AAPL") == 0 and P.load() == {}       # ack는 체결 아님

        r1 = R.resolve_acks_from_rows([{
            "odno": "OD1", "pdno": "AAPL", "side": "BUY", "ord_qty": 10,
            "filled": 4, "price": 101.0, "src": "ccnl", "open": True}])
        assert r1[0]["state"] == "partial" and C.open_qty("AAPL") == 4
        assert P.load()["AAPL"]["qty"] == 4

        r2 = R.resolve_acks_from_rows([{
            "odno": "OD1", "pdno": "AAPL", "side": "BUY", "ord_qty": 10,
            "filled": 10, "price": 102.0, "src": "ccnl", "open": False}])
        assert r2[0]["state"] == "filled" and C.open_qty("AAPL") == 10
        assert round(P.load()["AAPL"]["entry"], 2) == 101.60
        # 같은 체결을 다시 대사해도 accounted 때문에 중복 lot가 생기지 않는다.
        assert A.sync_fill("buy:1", filled_qty=10, fill_price=102.0)["delta"] == 0

        L.record_submit("sell:1", "AAPL", 4, meta=_meta(side="SELL"))
        L.on_result("sell:1", "partial", 2, fill_price=110.0,
                    fill_price_source="ccnl", open_order=True)
        a1 = A.sync_fill("sell:1")
        assert a1["delta"] == 2 and a1["pnl"] > 0 and C.open_qty("AAPL") == 8
        L.on_result("sell:1", "filled", 4, fill_price=111.0,
                    fill_price_source="ccnl", open_order=False)
        a2 = A.sync_fill("sell:1")
        assert a2["delta"] == 2 and C.open_qty("AAPL") == 6
        assert P.load()["AAPL"]["qty"] == 6 and C.realized_on() > 0
        print("[PASS] ack 미반영 → 부분4 → 완전10 → 매도 부분2+잔여2, 중복회계 0")


def test_sleeve_isolation_and_reject():
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")), \
         mock.patch.dict(os.environ, {"COSTBOOK_PATH": os.path.join(tmp, "cost.jsonl")}):
        meta = {**_meta(sleeve="B"), "pos_key": "pos:B"}
        L.record_submit("buy:B", "MSFT", 3, meta=meta)
        L.on_result("buy:B", "filled", 3, fill_price=50.0,
                    fill_price_source="ccnl", open_order=False)
        assert A.sync_fill("buy:B")["ok"]
        assert C.open_cost_total("B") == 3 * 50 * 1400
        assert C.open_cost_total("A") == 0

        L.record_submit("reject", "NVDA", 2, meta={**_meta(), "pos_key": "pos:N"})
        L.on_result("reject", "rejected", 0)
        assert A.sync_fill("reject", filled_qty=0, fill_price=100)["delta"] == 0
        assert C.open_qty("NVDA") == 0
        print("[PASS] 슬리브 B 원가 분리 + 거절 주문 회계 미반영")


def test_unknown_reconcile_recovers_accounting():
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")), \
         mock.patch.dict(os.environ, {"COSTBOOK_PATH": os.path.join(tmp, "cost.jsonl")}):
        L.record_submit("lost", "AAPL", 3, meta=_meta())
        L.bind_broker_order("lost", "ODX", ord_tmd="101500")
        L.on_result("lost", "unknown", 0)
        out = R.reconcile_unknowns(
            {"rt_cd": "0", "output": []},
            {"rt_cd": "0", "output": [{
                "odno": "ODX", "pdno": "AAPL", "ft_ord_qty": "3",
                "ft_ccld_qty": "3", "ft_ccld_unpr3": "103.25",
                "sll_buy_dvsn_cd": "02", "ord_tmd": "101500"}]})
        assert out[0]["state"] == "filled" and out[0]["accounting"]["ok"]
        assert C.open_qty("AAPL") == 3 and P.load()["AAPL"]["entry"] == 103.25
        print("[PASS] UNKNOWN→ccnl HIGH: 실제 체결가·수량으로 원가/포지션 복구")


def test_accounting_retry_after_crash_is_idempotent():
    """costbook/position 기록 뒤 accounted 직전 크래시가 나도 재시도 중복 0."""
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")), \
         mock.patch.dict(os.environ, {"COSTBOOK_PATH": os.path.join(tmp, "cost.jsonl")}):
        L.record_submit("buy:crash", "AAPL", 3, meta=_meta())
        L.on_result("buy:crash", "filled", 3, fill_price=100.0,
                    fill_price_source="ccnl", open_order=False)
        with mock.patch.object(
                L, "_append_unlocked",
                side_effect=OSError("accounted 직전 크래시")):
            try:
                A.sync_fill("buy:crash")
                raise AssertionError("fault injection이 발생하지 않음")
            except OSError:
                pass
        assert C.open_qty("AAPL") == 3 and P.load()["AAPL"]["qty"] == 3
        assert int(L.state_of("buy:crash").get("accounted") or 0) == 0

        retried = A.sync_fill("buy:crash")
        assert retried["ok"] and retried["delta"] == 3
        assert C.open_qty("AAPL") == 3 and P.load()["AAPL"]["qty"] == 3
        assert L.state_of("buy:crash")["accounted"] == 3
        print("[PASS] 회계 마지막 기록 fault→재시도해도 costbook/포지션 중복 0")


def test_unaccounted_fill_alerts_once_after_three_cycles():
    """여러 예약이 묶여도 3회 뒤 요약 1개만 경보하고 회계 후 정리한다."""
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch.object(A, "WATCH_PATH", os.path.join(tmp, "watch.json")), \
         mock.patch("bot.notify.send", return_value=True) as send:
        L.record_submit("buy:watch", "AAPL", 3, meta=_meta())
        L.on_result("buy:watch", "filled", 3, open_order=False)
        L.record_submit(
            "buy:watch2", "MSFT", 2,
            meta={**_meta(), "pos_key": "pos:MSFT"})
        L.on_result("buy:watch2", "filled", 2, open_order=False)

        assert A.monitor_unaccounted_fills(alert_cycles=3)["alerts"] == []
        assert A.monitor_unaccounted_fills(alert_cycles=3)["alerts"] == []
        third = A.monitor_unaccounted_fills(alert_cycles=3)
        assert third["pending"] == 2 and len(third["alerts"]) == 2
        assert {row["cycles"] for row in third["alerts"]} == {3}
        assert send.call_count == 1
        message = send.call_args.args[0]
        assert "회계 지연 2건" in message and "AAPL(3/0)" in message
        assert "MSFT(2/0)" in message and "예약은 계속 유지" in message

        fourth = A.monitor_unaccounted_fills(alert_cycles=3)
        assert fourth["pending"] == 2 and fourth["alerts"] == []
        assert send.call_count == 1

        L.mark_accounted("buy:watch", 3)
        L.mark_accounted("buy:watch2", 2)
        done = A.monitor_unaccounted_fills(alert_cycles=3)
        assert done["pending"] == 0 and A._watch_load()["items"] == {}
        print("[PASS] filled>accounted 여러 건도 3회 뒤 요약 1회·회계 후 정리")


def main():
    test_partial_fill_then_final_and_sell()
    test_sleeve_isolation_and_reject()
    test_unknown_reconcile_recovers_accounting()
    test_accounting_retry_after_crash_is_idempotent()
    test_unaccounted_fill_alerts_once_after_three_cycles()
    print("\nKIS 체결 회계 검증 통과.")


if __name__ == "__main__":
    main()
