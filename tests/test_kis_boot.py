"""O4 부팅 대사 + X4 파수꾼 KIS 배선 검증(모킹 — 전송 없음).

  1) UNKNOWN 없음 → 즉시 ok·게이트 열림
  2) UNKNOWN 1건 + ccnl 유일후보 → HIGH 해소·게이트 열림
  3) 조회 실패(None) → fail-closed: ok=False·게이트 닫힘
  4) LOW(후보 2) → 잠금 유지·low 카운트
  5) X4: _KisBroker.place_sell → ack/unknown/reject 정규화(dict 규약)
  6) 파수꾼 매수 경로 부재 유지(_KisBroker에 buy 없음)

실행: python -m tests.test_kis_boot
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup(tmp):
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]
    os.environ.update({"KIS_ENV": "mock", "KIS_MOCK_APPKEY": "k",
                       "KIS_MOCK_APPSECRET": "s", "KIS_MOCK_CANO": "50001234",
                       "KIS_TOKEN_CACHE": os.path.join(tmp, "tok.json"),
                       "KIS_ORDERS_ENABLED": "1"})
    import bot.kis as kis
    import bot.ledger as L
    import bot.kis_reconcile as R
    import bot.kis_boot as B
    importlib.reload(kis)
    importlib.reload(L)
    importlib.reload(R)
    importlib.reload(B)
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    return kis, L, B


def test_no_unknowns_opens_gate():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, B = _setup(tmp)
        assert B.trading_allowed() is False        # 대사 전엔 닫힘
        s = B.boot_reconcile()
        assert s["ok"] and s["unknowns"] == 0 and B.trading_allowed()
    print("[PASS] UNKNOWN 없음 → 즉시 ok·게이트 열림")


def test_high_resolution():
    with tempfile.TemporaryDirectory() as tmp:
        kis, L, B = _setup(tmp)
        L.record_submit("b1#1", "AAPL", 4, "손절",
                        meta={"side": "SELL", "excg": "NASD"})
        L.on_result("b1#1", "unknown", 0)
        nccs = {"rt_cd": "0", "output": []}
        ccnl = {"rt_cd": "0", "output": [
            {"odno": "9001", "pdno": "AAPL", "ft_ord_qty": "4",
             "ft_ccld_qty": "4", "sll_buy_dvsn_cd_name": "매도",
             "ord_tmd": "150000"}]}
        with mock.patch.object(kis, "open_orders", return_value=nccs), \
             mock.patch.object(kis, "fills", return_value=ccnl), \
             mock.patch.object(B, "kis", kis):
            s = B.boot_reconcile()
        assert s["ok"] and s["resolved"] == 1 and s["low"] == 0
        assert not L.is_locked("AAPL") and B.trading_allowed()
    print("[PASS] UNKNOWN 1건 → ccnl HIGH 해소·게이트 열림")


def test_query_failure_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        kis, L, B = _setup(tmp)
        L.record_submit("b2#1", "TSLA", 1, "손절",
                        meta={"side": "SELL", "excg": "NASD"})
        L.on_result("b2#1", "unknown", 0)
        with mock.patch.object(kis, "open_orders", return_value=None), \
             mock.patch.object(kis, "fills", return_value=None), \
             mock.patch.object(B, "kis", kis):
            s = B.boot_reconcile()
        assert not s["ok"] and not B.trading_allowed()   # fail-closed
        assert L.is_locked("TSLA")                       # 잠금 그대로
    print("[PASS] 조회 실패 → fail-closed(게이트 닫힘·잠금 유지)")


def test_low_keeps_lock():
    with tempfile.TemporaryDirectory() as tmp:
        kis, L, B = _setup(tmp)
        L.record_submit("b3#1", "NVDA", 2, "손절",
                        meta={"side": "SELL", "excg": "NASD"})
        L.on_result("b3#1", "unknown", 0)
        nccs = {"rt_cd": "0", "output": [
            {"odno": "9101", "pdno": "NVDA", "ft_ord_qty": "2",
             "nccs_qty": "2", "sll_buy_dvsn_cd_name": "매도", "ord_tmd": "150000"},
            {"odno": "9102", "pdno": "NVDA", "ft_ord_qty": "2",
             "nccs_qty": "2", "sll_buy_dvsn_cd_name": "매도", "ord_tmd": "150001"}]}
        with mock.patch.object(kis, "open_orders", return_value=nccs), \
             mock.patch.object(kis, "fills",
                               return_value={"rt_cd": "0", "output": []}), \
             mock.patch.object(B, "kis", kis):
            s = B.boot_reconcile()
        assert s["ok"] and s["low"] == 1 and L.is_locked("NVDA")
        assert B.trading_allowed()          # 전체 게이트는 열림(종목은 잠김)
    print("[PASS] LOW(후보2) → 종목 잠금 유지·전체 게이트는 열림")


def test_fill_notifications_use_explicit_trade_category():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, B = _setup(tmp)
        now = time.time()
        aged = [{
            "state": "ack", "side": "BUY", "submitted_at": now - 200,
            "market": "US", "symbol": "AAPL", "excg": "NASD",
        }]
        resolved = [
            {"symbol": "AAPL", "side": "BUY", "filled": 1, "residual": 0},
            {"symbol": "TSLA", "side": "SELL", "filled": 2, "residual": 1},
        ]
        with mock.patch.object(B.ledger, "open_orders", side_effect=[aged, []]), \
             mock.patch.object(B.kis, "open_orders",
                               return_value={"rt_cd": "0", "output": []}), \
             mock.patch.object(B.kis, "fills",
                               return_value={"rt_cd": "0", "output": []}), \
             mock.patch.object(B.kis_reconcile, "resolve_acks_from_rows",
                               return_value=resolved), \
             mock.patch.object(B.kis_reconcile, "resolve_acks_by_balance",
                               return_value=[]), \
             mock.patch.object(B, "_notify") as send:
            assert B._resolve_acks() == resolved
        assert send.call_count == 2
        assert send.call_args_list[0].kwargs == {
            "critical": False, "category": "trade"}
        assert send.call_args_list[1].kwargs == {
            "critical": True, "category": "trade"}
    print("[PASS] 매수·매도 체결확정 → 문구와 무관한 명시적 trade 분류")


def test_sentinel_kis_place_sell_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        import bot.sentinel as sn
        importlib.reload(sn)
        br = sn._KisBroker()
        # ack / unknown / blocked 3경로 정규화 확인 (kis_orders·quote 모킹)
        for act, want in (("ack", "ack"), ("unknown", "unknown"),
                          ("blocked", "rejected")):
            with mock.patch.object(br, "quote", return_value=100.0), \
                 mock.patch("bot.kis_orders.place_sell",
                            return_value={"ok": act == "ack", "act": act,
                                          "key": "k", "odno": "1"}):
                r = br.place_sell("AAPL", 3, "손절", "k#1")
            assert r["state"] == want, f"{act}→{r['state']} (기대 {want})"
        # 시세 없으면 발주 보류(rejected)
        with mock.patch.object(br, "quote", return_value=None):
            r = br.place_sell("AAPL", 3, "손절", "k#2")
        assert r["state"] == "rejected"
        # 매수 경로 부재(매도 전용 원칙)
        assert not hasattr(br, "place_buy") and not hasattr(br, "buy")
    print("[PASS] X4: place_sell 정규화(ack/unknown/rejected)·매수 경로 없음")


def main():
    test_no_unknowns_opens_gate()
    test_high_resolution()
    test_query_failure_fail_closed()
    test_low_keeps_lock()
    test_fill_notifications_use_explicit_trade_category()
    test_sentinel_kis_place_sell_mapping()
    print("\n모든 O4/X4 테스트 통과 — 부팅 대사 게이트·파수꾼 KIS 배선.")


if __name__ == "__main__":
    main()
