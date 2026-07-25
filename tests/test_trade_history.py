"""확정 체결 거래이력 결합·손익·보안 경계 검증."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from bot import costbook, kis_positions, ledger, trade_history


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
        costbook.add_lot(
            pos_key, "ABC", 10, 100.0, fx=1000.0, sleeve="B",
            event_id="fill:buy:ABC:BUY:10")
        kis_positions.apply_buy_fill(
            "ABC", qty=10, price=100.0, stop=90.0, ccy="USD",
            pos_key=pos_key, name="테스트 종목", opened="2026-07-01",
            sleeve="B", target=120.0, event_id="fill:buy:ABC:BUY:10")

        _record_sale(
            "kis:ABC:stop#1", pos_key, code="ABC", qty=4, price=90.0,
            reason="하드 손절(손절가 이탈)", sleeve="B")
        payload = trade_history.snapshot()

    assert payload["available"] is True
    assert payload["read_only"] is True
    assert payload["summary"]["sell_fills"] == 1
    assert payload["summary"]["losses"] == 1
    row = payload["trades"][0]
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


def test_corrupt_order_ledger_hides_history_fail_closed():
    with tempfile.TemporaryDirectory() as td, \
            mock.patch.object(ledger, "LEDGER_PATH", os.path.join(td, "orders.jsonl")), \
            mock.patch.object(kis_positions, "PATH", os.path.join(td, "positions.jsonl")), \
            mock.patch.dict(os.environ, {
                "COSTBOOK_PATH": os.path.join(td, "costbook.jsonl"),
            }):
        Path(ledger.LEDGER_PATH).write_text("{broken\n", encoding="utf-8")
        payload = trade_history.snapshot()
    assert payload["available"] is False
    assert payload["partial"] is True
    assert payload["trades"] == []
    assert payload["summary"]["realized_pnl_krw"] is None


def main():
    test_confirmed_sales_join_exact_prices_reasons_and_pnl()
    print("[PASS] 거래이력 평단·매도가·사유·부분매도·실현손익 결합")
    test_corrupt_order_ledger_hides_history_fail_closed()
    print("[PASS] 주문 원장 손상 시 거래이력 fail-closed")
    print("\n확정 체결 거래이력 검증 통과.")


if __name__ == "__main__":
    main()
