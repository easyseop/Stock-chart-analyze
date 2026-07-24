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
        broker_state = (
            {"AAPL": 5}, {"AAPL": 500 * 1400.0},
            [{"key": "kb:sig:pb", "symbol": "AAPL", "qty": 5,
              "cost": 5 * 95 * 1400.0, "sleeve": "A"}],
            set(), {"AAPL": "A"})
        with mock.patch("bot.kis_buyloop._broker_state",
                        return_value=broker_state), \
             mock.patch.object(P.kis_buy, "execute_entry", return_value=ret) as ex:
            out = P.process(quote_fn=lambda *_: 100.0)
        assert ex.called and ex.call_args.kwargs["qty_cap"] == 5
        assert ex.call_args.kwargs["open_cost_krw"] == 500 * 1400.0
        assert ex.call_args.kwargs["total_open_cost_krw"] == 500 * 1400.0
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


def test_protection_cancels_buy_before_sell():
    """손절 발화 시 BUY 잔량은 취소 '확인' 전까지 SELL과 동시 진행하지 않는다."""
    def run():
        L.record_submit("kb:alk", "ALK", 8, meta={
            "side": "BUY", "market": "US", "excg": "NYSE",
            "stop": 88.0})
        L.bind_broker_order("kb:alk", "OD-BUY")
        L.on_result("kb:alk", "partial", 3, open_order=True)
        with mock.patch.object(P.notify, "send"), \
             mock.patch.object(P.kis_orders, "cancel_order",
                               return_value={"act": "canceled", "ok": True}) as cancel:
            assert P.cancel_open_buys_for_protection("ALK") is False
        assert cancel.call_args.args[3] == 5             # 잔여 5주만 취소
        cur = L.state_of("kb:alk")
        assert cur["state"] == "cancel_pending" and cur["filled"] == 3

        # 취소 API 접수가 아니라 브로커 미체결 목록 소멸까지 확인돼야 True.
        with mock.patch.object(P, "_cancel_confirmed", return_value=True):
            assert P.cancel_open_buys_for_protection("ALK") is True
        cur2 = L.state_of("kb:alk")
        assert cur2["state"] == "cancelled" and cur2["filled"] == 3
        assert not L.open_orders("ALK", side="BUY")

        # 아직 브로커에 보내지 않은 눌림 계획도 손절 전에 종료돼 재매수를 막는다.
        L.record_plan("kb:alk:pb", "ALK", 2, meta={
            "side": "BUY", "pending": True, "stop": 88.0})
        assert P.cancel_open_buys_for_protection("ALK") is True
        assert L.state_of("kb:alk:pb")["state"] == "cancelled"
        print("[PASS] 손절 우선: BUY 잔여 취소접수→확인, 부분체결 3주 보존")
    _with_ledger(run)


def test_protection_retries_after_confirmed_cancel_failure():
    """취소 1회 확정 실패 뒤 새 키로 재시도하고, 미확정 취소는 중복 전송하지 않는다."""
    def run():
        L.record_submit("kb:retry", "ALK", 8, meta={
            "side": "BUY", "market": "US", "excg": "NYSE", "stop": 88.0})
        L.bind_broker_order("kb:retry", "OD-RETRY")
        L.on_result("kb:retry", "partial", 3, open_order=True)
        calls = []

        def cancel(key, symbol, odno, qty, **kw):
            calls.append(key)
            assert L.try_record_cancel(
                key, symbol, attempt_group=kw["attempt_group"],
                meta={"side": "CANCEL"})
            if len(calls) == 1:
                L.on_result(key, "rejected", 0)
                return {"ok": False, "act": "rate_limited", "key": key}
            L.on_result(key, "filled", 0)
            return {"ok": True, "act": "canceled", "key": key}

        with mock.patch.object(P.notify, "send"), \
             mock.patch.object(P.kis_orders, "cancel_order",
                               side_effect=cancel):
            assert P.cancel_open_buys_for_protection("ALK") is False
            assert L.state_of("kb:retry")["state"] == "partial"
            assert P.cancel_open_buys_for_protection("ALK") is False
        assert calls == [
            "kb:retry:protect-cxl#1",
            "kb:retry:protect-cxl#2",
        ]
        assert L.state_of("kb:retry")["state"] == "cancel_pending"
        with mock.patch.object(P, "_cancel_confirmed", return_value=True):
            assert P.cancel_open_buys_for_protection("ALK") is True
        assert L.state_of("kb:retry")["filled"] == 3

        # 응답유실 취소는 다음 사이클에 새 HTTP를 내지 않는다(Q4).
        L.record_submit("kb:unknown-cxl", "MSFT", 4, meta={
            "side": "BUY", "market": "US", "excg": "NASD", "stop": 300.0})
        L.bind_broker_order("kb:unknown-cxl", "OD-UNKNOWN")
        L.on_result("kb:unknown-cxl", "partial", 1, open_order=True)
        base = "kb:unknown-cxl:protect-cxl"
        key = base + "#1"
        assert L.try_record_cancel(
            key, "MSFT", attempt_group=base, meta={"side": "CANCEL"})
        L.on_result(key, "unknown", 0)
        with mock.patch.object(
                P.kis_orders, "cancel_order",
                side_effect=AssertionError("미확정 취소 중복 전송")):
            assert P.cancel_open_buys_for_protection("MSFT") is False
        print("[PASS] 취소 확정실패→고유키 재시도→손절 인계, UNKNOWN 중복취소 0")
    _with_ledger(run)


def test_b_plan_uses_b_budget_and_all_reservations():
    """B 계획주문이 A 기본시드/빈 open_cost로 우회하지 않는다."""
    def run():
        L.record_submit("sb:parent", "ALK", 1, meta={
            "side": "BUY", "market": "US", "price": 100.0, "sleeve": "B"})
        L.on_result("sb:parent", "filled", 1)
        P.create_half_plan(
            "sb:parent:pb", "ALK", 2, parent_key="sb:parent",
            limit=95.0, stop=90.0, market="US", excg="NYSE",
            fx=1400.0, sleeve="B",
            meta={"pos_key": "sb:parent", "sleeve": "B", "stop": 90.0})
        current = {"key": "sb:parent:pb", "symbol": "ALK", "qty": 2,
                   "cost": 266_000.0, "sleeve": "B"}
        other = {"key": "sb:other", "symbol": "SIG", "qty": 1,
                 "cost": 4_400_000.0, "sleeve": "B"}
        broker_state = (
            {"ALK": 1}, {"ALK": 100_000.0}, [current, other],
            set(), {"ALK": "B"})
        ret = kis_buy.BuyDecision(False, "sizing", "cap", qty=0)
        env = {
            "BOT_OPERATING_TOTAL_KRW": "35000000",
            "BOT_OPERATING_BUFFER_PCT": "0.05",
            "BOT_SEED_KRW": "30000000",
            "BOT_SEED_SB_KRW": "5000000",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("bot.kis_buyloop._broker_state",
                        return_value=broker_state), \
             mock.patch.object(P.kis_buy, "execute_entry", return_value=ret) as ex:
            P.process(quote_fn=lambda *_: 100.0)
        kw = ex.call_args.kwargs
        assert kw["seed_krw"] == 4_750_000
        assert kw["open_cost_krw"] == 4_500_000
        assert kw["total_open_cost_krw"] == 4_500_000
        print("[PASS] B 계획: 475만 배분시드·held+다른 예약원가로 게이트")
    _with_ledger(run)


def main():
    test_submit_only_after_parent_fill()
    test_expiry_and_stop_cancel()
    test_protection_cancels_buy_before_sell()
    test_protection_retries_after_confirmed_cancel_failure()
    test_b_plan_uses_b_budget_and_all_reservations()
    print("\nKIS 눌림 대기 주문 수명주기 통과.")


if __name__ == "__main__":
    main()
