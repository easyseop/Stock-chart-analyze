"""눌림 대기 주문 — 1차 체결 후 제출·21일 만료·손절 이탈 취소."""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_buy, kis_pending as P, ledger as L


def _with_ledger(fn):
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl")):
            fn()


def _plan(parent_state="filled"):
    L.record_submit("kb:sig", "AAPL", 5, meta={
        "side": "BUY", "market": "US", "price": 100.0})
    L.on_result("kb:sig", parent_state, 5 if parent_state == "filled" else 0)
    P.create_half_plan(
        "kb:sig:pb", "AAPL", 5, parent_key="kb:sig", limit=95.0,
        stop=90.0, market="US", excg="NASD", fx=1400.0, sleeve="A",
        meta={"pos_key": "kb:sig", "stop": 90.0, "ccy": "USD",
              "opened": "2026-07-24", "tactic": "half"})


def test_submit_only_after_parent_fill():
    def run():
        _plan("ack")
        with mock.patch.object(P.kis, "holdings", return_value={}), \
             mock.patch.object(P.kis_buy, "execute_entry") as ex:
            P.process(quote_fn=lambda *_: 100.0)
            assert not ex.called
        L.on_result("kb:sig", "partial", 3, open_order=True)
        with mock.patch.object(P.kis, "holdings", return_value={"AAPL": 3}), \
             mock.patch.object(P.kis_buy, "execute_entry") as ex:
            P.process(quote_fn=lambda *_: 100.0)
            assert not ex.called                    # 1차 잔량이 살아 있으면 2차 금지
        L.on_result("kb:sig", "filled", 5)
        ret = kis_buy.BuyDecision(True, "sent", "ack", qty=5, planned_qty=5)
        with mock.patch.object(P.kis, "holdings", return_value={"AAPL": 5}), \
             mock.patch.object(P.kis_buy, "execute_entry", return_value=ret) as ex:
            out = P.process(quote_fn=lambda *_: 100.0)
        assert ex.called and ex.call_args.kwargs["qty_cap"] == 5
        assert out[0]["ok"]
        print("[PASS] half 2차: 1차 ack 중 보류·체결 확정 뒤 게이트 체인으로 제출")
    _with_ledger(run)


def test_expiry_and_stop_cancel():
    def run():
        _plan("filled")
        old = (datetime.date.today() - datetime.timedelta(days=22)).isoformat()
        # 계획의 created_day를 과거로 덮는 새 plan 이벤트.
        cur = L.state_of("kb:sig:pb")
        L.record_plan("kb:sig:pb", "AAPL", 5, meta={
            **cur, "pending": True, "created_day": old})
        out = P.process(quote_fn=lambda *_: 100.0)
        assert out[0]["act"] == "expired"
        assert L.state_of("kb:sig:pb")["state"] == "expired"

        L.record_submit("kb:stop:pb", "MSFT", 4, meta={
            "side": "BUY", "market": "US", "price": 95.0, "stop": 90.0,
            "pending": True})
        L.bind_broker_order("kb:stop:pb", "OD1")
        L.on_result("kb:stop:pb", "ack", 0)
        with mock.patch.object(P.kis_orders, "cancel_order",
                               return_value={"act": "canceled", "ok": True}) as cancel:
            out2 = P.process(quote_fn=lambda *_: 89.0)
        assert cancel.called and any(x["act"] == "canceled" for x in out2)
        assert L.state_of("kb:stop:pb")["state"] == "cancel_pending"
        with mock.patch.object(P.kis, "open_orders",
                               return_value={"rt_cd": "0", "output": []}), \
             mock.patch.object(P.kis, "fills",
                               return_value={"rt_cd": "0", "output": []}):
            out3 = P.process(quote_fn=lambda *_: 89.0)
        assert out3[0]["act"] == "cancelled"
        assert L.state_of("kb:stop:pb")["state"] == "cancelled"

        # 취소 확인 사이에 전량 체결된 경우 filled를 보존하고 cancelled로 덮지 않는다.
        L.record_submit("kb:race:pb", "NVDA", 2, meta={
            "side": "BUY", "market": "US", "price": 50.0, "stop": 45.0,
            "pending": True})
        L.bind_broker_order("kb:race:pb", "ODR")
        L.on_result("kb:race:pb", "cancel_pending", 0, open_order=True)
        ccnl = {"rt_cd": "0", "output": [{
            "odno": "ODR", "pdno": "NVDA", "ft_ord_qty": "2",
            "ft_ccld_qty": "2", "ft_ccld_unpr3": "49.5",
            "sll_buy_dvsn_cd": "02"}]}
        with mock.patch.object(P.kis, "open_orders",
                               return_value={"rt_cd": "0", "output": []}), \
             mock.patch.object(P.kis, "fills", return_value=ccnl):
            P.process(quote_fn=lambda *_: 49.5)
        assert L.state_of("kb:race:pb")["state"] == "filled"
        print("[PASS] pending: 21일 만료 + 손절 이탈 취소접수→브로커 확인→종료")
    _with_ledger(run)


def main():
    test_submit_only_after_parent_fill()
    test_expiry_and_stop_cancel()
    print("\nKIS 눌림 대기 주문 수명주기 통과.")


if __name__ == "__main__":
    main()
