"""확정 체결 거래이력 결합·손익·보안 경계 검증."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from bot import costbook, kis_positions, ledger, trade_history


def _record_buy(key: str, pos_key: str, *, code: str, qty: int,
                price: float, sleeve: str = "A",
                fx: float = 1000.0) -> None:
    ledger.record_submit(
        key, code, qty, "전략 진입",
        meta={
            "side": "BUY", "pos_key": pos_key, "sleeve": sleeve,
            "fx": fx, "ccy": "USD", "market": "US", "stop": price * .9,
            "name": "테스트 종목", "opened": "2026-07-01",
        })
    ledger.on_result(
        key, "filled", qty, fill_price=price,
        fill_price_source="broker", open_order=False)
    event_id = f"fill:{key}:BUY:{qty}"
    costbook.add_lot(
        pos_key, code, qty, price, fx=fx, sleeve=sleeve,
        event_id=event_id)
    kis_positions.apply_buy_fill(
        code, qty=qty, price=price, stop=price * .9, ccy="USD",
        pos_key=pos_key, name="테스트 종목", opened="2026-07-01",
        sleeve=sleeve, target=price * 1.2, event_id=event_id)


def _record_sale(key: str, pos_key: str, *, code: str, qty: int,
                 price: float, reason: str, sleeve: str = "A",
                 fx: float = 1000.0) -> None:
    ledger.record_submit(
        key, code, qty, reason,
        meta={
            "side": "SELL", "pos_key": pos_key, "sleeve": sleeve,
            "fx": fx, "ccy": "USD", "market": "US",
            "name": "테스트 종목", "opened": "2026-07-01",
        })
    ledger.on_result(
        key, "filled", qty, fill_price=price,
        fill_price_source="broker", open_order=False)
    event_id = f"fill:{key}:SELL:{qty}"
    costbook.close_lot(
        pos_key, qty, price * qty * fx,
        sleeve=sleeve, day_kst="2026-07-25", event_id=event_id)
    kis_positions.apply_sell_fill(
        code, qty=qty, price=price, pos_key=pos_key, event_id=event_id)


def test_confirmed_sales_join_exact_prices_reasons_and_pnl():
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ledger, "LEDGER_PATH", os.path.join(td, "orders.jsonl")), \
            mock.patch.object(kis_positions, "PATH", os.path.join(td, "positions.jsonl")), \
            mock.patch.dict(os.environ, {
                "COSTBOOK_PATH": os.path.join(td, "costbook.jsonl"),
            }):
        pos_key = "sb:unit:ABC"
        _record_buy(
            "buy:ABC", pos_key, code="ABC", qty=10, price=100.0,
            sleeve="B")

        _record_sale(
            "kis:ABC:stop#1", pos_key, code="ABC", qty=4, price=90.0,
            reason="하드 손절(손절가 이탈)", sleeve="B")
        lock_paths = [
            Path(ledger.LEDGER_PATH + ".lock"),
            Path(kis_positions.PATH + ".lock"),
            Path(os.environ["COSTBOOK_PATH"] + ".lock"),
        ]
        before = [(path.stat().st_mtime_ns, path.stat().st_mode) for path in lock_paths]
        with mock.patch.object(
                ledger, "_file_lock",
                side_effect=AssertionError("read-only history must not open RW lock")), \
                mock.patch.object(
                    kis_positions, "_file_lock",
                    side_effect=AssertionError("read-only history must not open RW lock")), \
                mock.patch.object(
                    costbook, "_file_lock",
                    side_effect=AssertionError("read-only history must not open RW lock")):
            payload = trade_history.snapshot()
        after = [(path.stat().st_mtime_ns, path.stat().st_mode) for path in lock_paths]

    assert payload["available"] is True
    assert payload["read_only"] is True
    assert payload["summary"]["fills"] == 2
    assert payload["summary"]["buy_fills"] == 1
    assert payload["summary"]["sell_fills"] == 1
    assert payload["summary"]["losses"] == 1
    sells = [row for row in payload["trades"] if row["side"] == "sell"]
    buys = [row for row in payload["trades"] if row["side"] == "buy"]
    assert len(sells) == 1 and len(buys) == 1
    buy = buys[0]
    assert buy["qty"] == 10 and buy["fill_price"] == 100.0
    assert buy["amount_native"] == 1000.0
    assert buy["amount_krw"] == 1_000_000.0
    assert buy["average_price_after"] == 100.0
    assert buy["position_qty_after"] == 10
    assert buy["realized_pnl_krw"] is None and buy["verified"] is True
    row = sells[0]
    assert row["code"] == "ABC" and row["name"] == "테스트 종목"
    assert row["sleeve"] == "B" and row["reason_kind"] == "stop"
    assert row["qty"] == 4 and row["remaining_qty"] == 6
    assert row["partial_exit"] is True
    assert row["entry_price"] == 100.0 and row["exit_price"] == 90.0
    assert row["realized_pnl_krw"] == -40_000.0
    assert row["return_pct"] == -10.0
    assert row["verified"] is True
    encoded = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("kis:ABC:stop#1", "sb:unit:ABC", "ODNO", "CANO",
                      "APPSECRET", td):
        assert forbidden not in encoded
    assert before == after


def test_corrupt_order_ledger_hides_history_fail_closed():
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ledger, "LEDGER_PATH", os.path.join(td, "orders.jsonl")), \
            mock.patch.object(kis_positions, "PATH", os.path.join(td, "positions.jsonl")), \
            mock.patch.dict(os.environ, {
                "COSTBOOK_PATH": os.path.join(td, "costbook.jsonl"),
            }):
        Path(ledger.LEDGER_PATH).write_text("{broken\n", encoding="utf-8")
        Path(ledger.LEDGER_PATH + ".lock").touch()
        payload = trade_history.snapshot()
    assert payload["available"] is False
    assert payload["partial"] is True
    assert payload["trades"] == []
    assert payload["summary"]["realized_pnl_krw"] is None


def test_partial_buy_fills_are_visible_once_and_ack_is_not_a_trade():
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ledger, "LEDGER_PATH", os.path.join(td, "orders.jsonl")), \
            mock.patch.object(kis_positions, "PATH", os.path.join(td, "positions.jsonl")), \
            mock.patch.dict(os.environ, {
                "COSTBOOK_PATH": os.path.join(td, "costbook.jsonl"),
            }):
        key, pos_key = "buy:partial", "kb:partial"
        ledger.record_submit(
            key, "AAPL", 10, "전략 A 진입",
            meta={
                "side": "BUY", "pos_key": pos_key, "sleeve": "A",
                "fx": 1000.0, "ccy": "USD", "market": "US",
                "stop": 90.0, "name": "Apple", "opened": "2026-07-25",
            })
        ledger.on_result(key, "ack", 0, open_order=True)
        assert trade_history.snapshot()["trades"] == []  # 접수만으로는 이력 아님

        for cumulative, delta, price in ((4, 4, 101.0), (10, 6, 102.0)):
            ledger.on_result(
                key, "partial" if cumulative < 10 else "filled",
                cumulative, fill_price=price,
                fill_price_source="broker", open_order=cumulative < 10)
            event_id = f"fill:{key}:BUY:{cumulative}"
            costbook.add_lot(
                pos_key, "AAPL", delta, price, fx=1000.0,
                sleeve="A", event_id=event_id)
            kis_positions.apply_buy_fill(
                "AAPL", qty=delta, price=price, stop=90.0, ccy="USD",
                pos_key=pos_key, name="Apple", opened="2026-07-25",
                sleeve="A", target=130.0, event_id=event_id)
            # 크래시 재대사가 같은 event_id를 다시 남겨도 화면 중복은 없어야 한다.
            if cumulative == 4:
                kis_positions.apply_buy_fill(
                    "AAPL", qty=delta, price=price, stop=90.0, ccy="USD",
                    pos_key=pos_key, name="Apple", opened="2026-07-25",
                    sleeve="A", target=130.0, event_id=event_id)

        payload = trade_history.snapshot()
    buys = sorted(
        [row for row in payload["trades"] if row["side"] == "buy"],
        key=lambda row: row["position_qty_after"])
    assert len(buys) == 2
    assert [(row["qty"], row["position_qty_after"]) for row in buys] == [
        (4, 4), (6, 10)]
    assert buys[0]["average_price_after"] == 101.0
    assert buys[1]["average_price_after"] == 101.6
    assert payload["summary"]["fills"] == 2
    assert payload["summary"]["buy_fills"] == 2
    assert payload["summary"]["sell_fills"] == 0


def test_legacy_open_is_not_double_counted_and_keyless_sell_is_ambiguous():
    position_events = [
        {"ev": "open", "code": "ABC", "qty": 10, "entry": 100,
         "pos_key": "pa", "sleeve": "A", "ccy": "USD"},
        {"ev": "buy_fill", "code": "ABC", "qty": 10, "price": 101,
         "pos_key": "pa", "sleeve": "A", "ccy": "USD",
         "event_id": "fill:buy-a:BUY:10", "ts": 1_700_000_000},
        {"ev": "buy_fill", "code": "ABC", "qty": 5, "price": 99,
         "pos_key": "pb", "sleeve": "B", "ccy": "USD",
         "event_id": "fill:buy-b:BUY:5", "ts": 1_700_000_001},
        {"ev": "sell_fill", "code": "ABC", "qty": 2, "price": 90,
         "event_id": "fill:sell-unknown:SELL:2", "ts": 1_700_000_002},
    ]
    cost_events = [
        {"ev": "add", "event_id": "fill:buy-a:BUY:10",
         "cost_krw": 1_010_000, "ts": 1_700_000_000},
        {"ev": "add", "event_id": "fill:buy-b:BUY:5",
         "cost_krw": 495_000, "ts": 1_700_000_001},
        {"ev": "close", "event_id": "fill:sell-unknown:SELL:2",
         "cost_closed_krw": 202_000, "realized_pnl_krw": -22_000,
         "proceeds_krw": 180_000, "ts": 1_700_000_002},
    ]
    orders = [
        {"key": "buy-a", "side": "BUY", "pos_key": "pa", "sleeve": "A"},
        {"key": "buy-b", "side": "BUY", "pos_key": "pb", "sleeve": "B"},
        {"key": "sell-unknown", "side": "SELL", "reason": "손절"},
    ]
    rows, incomplete = trade_history._trade_rows(
        position_events, cost_events, orders)
    buys = sorted(
        [row for row in rows if row["side"] == "buy"],
        key=lambda row: row["sleeve"])
    assert [(row["sleeve"], row["qty"], row["position_qty_after"])
            for row in buys] == [("A", 10, 10), ("B", 5, 5)]
    assert buys[0]["average_price_after"] == 101.0
    assert not [row for row in rows if row["side"] == "sell"]
    assert incomplete == 1


def main():
    test_confirmed_sales_join_exact_prices_reasons_and_pnl()
    print("[PASS] 거래이력 매수·매도·평단·부분매도·실현손익 결합")
    test_partial_buy_fills_are_visible_once_and_ack_is_not_a_trade()
    print("[PASS] 주문접수 제외·부분매수 개별 표시·event_id 중복 제거")
    test_legacy_open_is_not_double_counted_and_keyless_sell_is_ambiguous()
    print("[PASS] legacy open 이중계상 방지·key 없는 복수 lot 매도 숨김")
    test_corrupt_order_ledger_hides_history_fail_closed()
    print("[PASS] 주문 원장 손상 시 거래이력 fail-closed")
    print("\n확정 체결 거래이력 검증 통과.")


if __name__ == "__main__":
    main()
