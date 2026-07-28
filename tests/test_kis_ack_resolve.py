"""ack(접수) 주문 잔고-delta 대사 검증 — resolve_acks_by_balance(전송 없음).

치명 구멍(2026-07-15 검토) 수정 확인: KIS 주문응답은 '접수'까지만이라 ack가
원장에 영원히 남아 ① 파수꾼이 그 종목 손절을 스킵(open_order_count≥1) ②
재진입 영구 차단 ③ 미러 캡 인플레이트. 잔고로 full-fill 증명 시에만 filled 전이.

  · BUY full-fill: before0→now3, intended3 → filled + open_order_count 해소
  · SELL full-fill: before3→now0 → filled
  · age 게이트: 갓 제출은 보류(체결 진행 중 스냅샷 오독 방지)
  · 부분(delta<Q): 보류(자동 결론 금지) · 이상(delta>Q): 동결
  · 동시 주문(2건)·baseline 심볼·잔고 실패(None): 보류
  · 파수꾼 시나리오: BUY ack 해소 후 open_order_count==0 → 손절 재발주 가능

실행: python -m tests.test_kis_ack_resolve
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
        if k.startswith(("KIS_", "USER_BASELINE", "SYMBOL_FREEZE")):
            del os.environ[k]
    os.environ.update({
        "KIS_ENV": "mock", "KIS_MOCK_APPKEY": "k", "KIS_MOCK_APPSECRET": "s",
        "KIS_MOCK_CANO": "50001234",
        "KIS_TOKEN_CACHE": os.path.join(tmp, "tok.json"),
        "USER_BASELINE_PATH": os.path.join(tmp, "base.json"),
        "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
    })
    mods = {}
    for name in ("kis", "ledger", "ownership", "kis_reconcile"):
        m = importlib.import_module(f"bot.{name}")
        importlib.reload(m)
        mods[name] = m
    mods["ledger"].LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    mods["ownership"].capture_baseline([])         # armed(깨끗한 계좌)
    return mods


def _ack(L, key, sym, side, qty, before, age_s=200.0):
    """제출+ack를 원장에 기록(제출시각은 age_s초 전으로)."""
    L._append({"ev": "submit", "key": key, "symbol": sym, "intended": qty,
               "filled": 0, "state": "submitted", "reason": "t",
               "meta": {"side": side, "hldg_before": before, "market": "US"
                        if not sym[:5].isdigit() else "KR"},
               "ts": time.time() - age_s})
    L.on_result(key, "ack", 0)


def test_buy_full_fill_resolves_and_unblocks():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R = M["ledger"], M["kis_reconcile"]
        _ack(L, "kb:a#1", "AAPL", "BUY", 3, before=0)
        assert L.open_order_count("AAPL") == 1     # 해소 전: 파수꾼이 손절 스킵하는 상태
        rs = R.resolve_acks_by_balance(
            {"US": {"AAPL": 3}}, complete_snapshot=True)
        assert len(rs) == 1 and rs[0]["state"] == "filled" and rs[0]["side"] == "BUY"
        assert L.state_of("kb:a#1")["state"] == "filled"
        assert L.open_order_count("AAPL") == 0     # ★ 손절 재발주 가능해짐
        assert L.can_submit("AAPL", min_interval_s=0)
    print("[PASS] BUY ack full-fill → filled 전이 + 파수꾼 손절 차단 해소")


def test_sell_full_fill_resolves():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R = M["ledger"], M["kis_reconcile"]
        _ack(L, "s#1", "005930", "SELL", 3, before=3)
        rs = R.resolve_acks_by_balance(               # 팔려서 잔고에서 사라짐
            {"KR": {}}, complete_snapshot=True)
        assert len(rs) == 1 and rs[0]["state"] == "filled" and rs[0]["side"] == "SELL"
        assert rs[0]["residual"] == 0
    print("[PASS] SELL ack full-fill(잔고 소멸) → filled 확정")


def test_age_gate_holds_fresh_orders():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R = M["ledger"], M["kis_reconcile"]
        _ack(L, "kb:b#1", "AAPL", "BUY", 3, before=0, age_s=5)   # 5초 전 — 어림
        rs = R.resolve_acks_by_balance(
            {"US": {"AAPL": 3}}, complete_snapshot=True)
        assert rs == [] and L.state_of("kb:b#1")["state"] == "ack"
    print("[PASS] 갓 제출(age<90s)은 보류 — 체결 진행 중 오독 방지")


def test_partial_holds_and_anomaly_freezes():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R, O = M["ledger"], M["kis_reconcile"], M["ownership"]
        _ack(L, "kb:c#1", "AAPL", "BUY", 3, before=0)
        rs = R.resolve_acks_by_balance(
            {"US": {"AAPL": 2}}, complete_snapshot=True)         # 부분(2<3)
        assert rs == [] and L.state_of("kb:c#1")["state"] == "ack"
        rs = R.resolve_acks_by_balance(
            {"US": {"AAPL": 5}}, complete_snapshot=True)         # 이상(5>3)
        assert rs == [] and O.is_frozen("AAPL")                  # 외부 개입 의심 동결
    print("[PASS] 부분은 보류(자동 결론 금지) · 초과 delta는 동결")


def test_concurrent_and_baseline_and_fail_hold():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R, O = M["ledger"], M["kis_reconcile"], M["ownership"]
        _ack(L, "kb:d#1", "TSLA", "BUY", 2, before=0)
        _ack(L, "kb:d#2", "TSLA", "BUY", 2, before=0)            # 동시 2건 — 귀속 불가
        assert R.resolve_acks_by_balance(
            {"US": {"TSLA": 2}}, complete_snapshot=True) == []
        O.capture_baseline([{"ovrs_pdno": "MSFT"}])              # 사용자 기보유
        _ack(L, "kb:e#1", "MSFT", "BUY", 1, before=0)
        assert R.resolve_acks_by_balance(
            {"US": {"MSFT": 1}}, complete_snapshot=True) == []
        _ack(L, "kb:f#1", "NVDA", "BUY", 1, before=0)
        assert R.resolve_acks_by_balance(
            {"US": None}, complete_snapshot=True) == []          # 잔고 실패 → 보류
    print("[PASS] 동시주문·baseline 심볼·잔고실패 → 전부 보류(fail-closed)")


def test_migrated_baseline_sell_resolves_but_uses_sell_order_price():
    """baseline 예외는 이관 pos_key+원가+before 3중 일치 SELL만 통과한다."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R, O = M["ledger"], M["kis_reconcile"], M["ownership"]
        from bot import costbook as C, kis_positions as P
        O.capture_baseline([{"ovrs_pdno": "CAG"}])
        pos_key = "legacy:A:CAG:2026-07-20"
        with mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")), \
             mock.patch.dict(os.environ, {
                 "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
             }):
            C.add_lot(
                pos_key, "CAG", 10, 100.0, fx=1380.0, sleeve="A",
                event_id="legacy-seed",
            )
            P.migrate_legacy(
                "CAG", qty=10, entry=100.0, stop=90.0, stop0=90.0,
                ccy="USD", pos_key=pos_key, opened="2026-07-20",
                sleeve="A", event_id="legacy-position",
            )
            L._append({
                "ev": "submit", "key": "sell:cag", "symbol": "CAG",
                "intended": 5, "filled": 0, "state": "submitted",
                "meta": {
                    "side": "SELL", "hldg_before": 5,
                    "legacy_hldg_before": 10,
                    "legacy_migrated_order": True, "market": "US",
                    "price": 110.0, "pos_key": pos_key, "sleeve": "A",
                    "fx": 1380.0, "ccy": "USD",
                },
                "ts": time.time() - 200,
            })
            L.on_result("sell:cag", "ack", 0)
            # 구버전의 잘못된 hldg_before 대사로 이미 close-only 동결됐더라도,
            # durable 이관 3중 증명을 만족하는 이 SELL만 좁게 해소해야 한다.
            O.freeze("CAG", "legacy hldg_before mismatch")
            assert O.is_frozen("CAG")
            rs = R.resolve_acks_by_balance(
                {"US": {"CAG": 5}},
                fill_prices={"US": {"CAG": 100.0}},
                complete_snapshot=True,
            )
            assert len(rs) == 1 and rs[0]["state"] == "filled"
            assert L.state_of("sell:cag")["fill_price"] == 110.0
            assert L.state_of("sell:cag")["fill_price_source"] \
                == "submitted-fallback"
            assert C.open_qty("CAG") == 5 and P.load()["CAG"]["qty"] == 5

            # pos_key가 다르면 같은 baseline·같은 delta라도 자동 귀속하지 않는다.
            P.migrate_legacy(
                "MSFT", qty=10, entry=100.0, stop=90.0, stop0=90.0,
                ccy="USD", pos_key="legacy:A:MSFT:2026-07-20",
                opened="2026-07-20", sleeve="A", event_id="msft-position",
            )
            C.add_lot(
                "legacy:A:MSFT:2026-07-20", "MSFT", 10, 100.0,
                fx=1380.0, sleeve="A", event_id="msft-seed",
            )
            O.capture_baseline([{"ovrs_pdno": "MSFT"}])
            L._append({
                "ev": "submit", "key": "sell:msft", "symbol": "MSFT",
                "intended": 5, "filled": 0, "state": "submitted",
                "meta": {
                    "side": "SELL", "hldg_before": 10, "market": "US",
                    "price": 110.0, "pos_key": "wrong", "sleeve": "A",
                    "fx": 1380.0, "ccy": "USD",
                },
                "ts": time.time() - 200,
            })
            L.on_result("sell:msft", "ack", 0)
            assert R.resolve_acks_by_balance(
                {"US": {"MSFT": 5}}, complete_snapshot=True) == []
            assert L.state_of("sell:msft")["state"] == "ack"

            # 이관 3중 증명이 없는 일반 동결 주문은 정확한 잔고 delta여도 보류한다.
            _ack(L, "sell:nvda", "NVDA", "SELL", 5, before=10)
            O.freeze("NVDA", "operator freeze")
            assert R.resolve_acks_by_balance(
                {"US": {"NVDA": 5}}, only_keys={"sell:nvda"}) == []
            assert L.state_of("sell:nvda")["state"] == "ack"
    print("[PASS] 동결된 migrated baseline SELL만 해소·일반 동결/baseline은 보류")


def test_partial_balance_map_requires_exact_keys_or_complete_contract():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L, R = M["ledger"], M["kis_reconcile"]
        _ack(L, "sell:tsla", "TSLA", "SELL", 5, before=5)
        # TSLA가 빠진 부분 map을 전체 잔고로 오인하면 now=0으로 fake-resolve된다.
        assert R.resolve_acks_by_balance({"US": {"AAPL": 1}}) == []
        assert L.state_of("sell:tsla")["state"] == "ack"
        assert R.resolve_acks_by_balance(
            {"US": {"AAPL": 1}}, only_keys={"unrelated"}) == []
        assert L.state_of("sell:tsla")["state"] == "ack"
    print("[PASS] 부분 잔고 map은 complete/exact-key 계약 없이는 ACK 확정 0건")


def test_boot_wires_ack_resolution():
    """kis_boot.boot_reconcile이 ack 대사를 함께 수행하는지(잔고 모킹)."""
    from unittest import mock
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        L = M["ledger"]
        kis_boot = importlib.import_module("bot.kis_boot")
        importlib.reload(kis_boot)
        # kis_boot 내부의 ledger/kis_reconcile은 reload된 모듈과 동일 객체 확인
        kis_boot.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "kb:g#1", "AAPL", "BUY", 3, before=0)
        with mock.patch.object(kis_boot.kis, "holdings",
                               return_value={"AAPL": 3}):
            d = kis_boot.boot_reconcile()
        assert d["ok"] and d.get("ack_resolved") == 1
        assert L.state_of("kb:g#1")["state"] == "filled"
    print("[PASS] boot_reconcile 배선 — 매 대사 사이클 ack 자동 확정")


def main():
    test_buy_full_fill_resolves_and_unblocks()
    test_sell_full_fill_resolves()
    test_age_gate_holds_fresh_orders()
    test_partial_holds_and_anomaly_freezes()
    test_concurrent_and_baseline_and_fail_hold()
    test_migrated_baseline_sell_resolves_but_uses_sell_order_price()
    test_partial_balance_map_requires_exact_keys_or_complete_contract()
    test_boot_wires_ack_resolution()
    print("\nack 잔고대사 검증 통과 — 접수 주문이 파수꾼/캡/재진입을 영구 차단하는 구멍 해소.")


if __name__ == "__main__":
    main()
