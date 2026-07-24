"""파수꾼–kis_chase 운영 배선: 주문·취소확인·부분체결 잔여 재주문."""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ledger as L, sentinel as S


def test_operational_chase_dependencies():
    fills = [{"rt_cd": "0", "output": []}]
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")), \
         mock.patch("bot.kis.us_excg_of", return_value="NASD"), \
         mock.patch("bot.kis.last_price", return_value=99.0), \
         mock.patch("bot.kis.holdings", return_value={"AAPL": 10}), \
         mock.patch("bot.kis.open_orders", return_value={"rt_cd": "0", "output": []}), \
         mock.patch("bot.kis.fills", side_effect=lambda **_: fills[0]):
        placed = []

        def place(key, symbol, qty, price, **kw):
            placed.append((key, qty, price))
            L.record_submit(key, symbol, qty, meta={
                "side": "SELL", "market": "US", "price": price,
                "chase": True, "ref_price": 100.0})
            L.bind_broker_order(key, f"OD{len(placed)}")
            L.on_result(key, "ack", 0)
            return {"ok": True, "act": "ack", "odno": f"OD{len(placed)}"}

        with mock.patch("bot.kis_orders.place_sell", side_effect=place), \
             mock.patch("bot.kis_orders.cancel_order",
                        return_value={"ok": True, "act": "canceled"}):
            ch = S._new_us_chase(
                "AAPL", 10, 100.0, "kis:acct:AAPL:2026-07-24:sell",
                {"pos_key": "pos:AAPL", "sleeve": "A"})
            ch.cfg.repost_after_s = 0
            ch.step()                                  # 1차 10주
            first = ch.current["key"]
            ch.step()                                  # 취소 접수
            assert ch.current.get("cancel_pending")
            fills[0] = {"rt_cd": "0", "output": [{
                "odno": "OD1", "pdno": "AAPL", "ft_ord_qty": "10",
                "ft_ccld_qty": "4", "ft_ccld_unpr3": "99.0",
                "sll_buy_dvsn_cd": "01"}]}
            ch.step()                                  # nccs 부재=취소확정, 잔여6 재주문
        assert placed[0][1] == 10 and placed[1][1] == 6
        assert L.state_of(first)["state"] == "cancelled"
        print("[PASS] sentinel chase: 취소확정 후 부분체결4 제외·잔여6만 재주문")


def main():
    test_operational_chase_dependencies()
    print("\n파수꾼 미국주 손절 chase 운영 배선 통과.")


if __name__ == "__main__":
    main()
