"""국내(KR) 잔고 기반 UNKNOWN 대사 — 안전 정리 검증(초과매도 구조적 봉쇄).

reconcile_unknowns_kr는 오직 '정확 full-fill'을 잔고가 증명할 때만 SELL을 filled로
확정한다. 미체결/부분/불명/조회실패/모호는 전부 LOW(잠금 유지) — 재주문 원천 차단.
BUY는 항상 LOW(신뢰 근거 없음). 이상치(방향역전·과다·impossible)는 동결.

실행: python -m tests.test_kis_reconcile_kr
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup(tmp):
    """원장·ownership 경로 격리 + 모듈 리로드."""
    os.environ["KIS_ENV"] = "mock"
    os.environ["USER_BASELINE_PATH"] = os.path.join(tmp, "baseline.json")
    os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
    import bot.kis as kis
    import bot.ledger as L
    import bot.ownership as own
    import bot.kis_reconcile as R
    importlib.reload(kis)
    importlib.reload(L)
    importlib.reload(own)
    importlib.reload(R)
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    own.capture_baseline([])            # arm 깨끗한 계좌(기본) — 개별 테스트가 덮어씀
    return kis, L, own, R


def _mk_unknown(L, key, sym, qty, side="SELL", hldg_before=None, market="KR"):
    meta = {"side": side, "market": market}
    if hldg_before is not None:
        meta["hldg_before"] = int(hldg_before)
    L.record_submit(key, sym, qty, "손절", meta=meta)
    L.on_result(key, "unknown", 0)


def _bal(sym_qty: dict, rt="0", ctx="", extra_rows=None):
    rows = [{"pdno": s, "hldg_qty": str(q)} for s, q in sym_qty.items()]
    rows += (extra_rows or [])
    return {"rt_cd": rt, "output1": rows, "ctx_area_nk100": ctx}


def _by_key(results, key):
    return next((r for r in results if r["key"] == key), None)


def test_high_full_sell():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        res = R.reconcile_unknowns_kr(_bal({"005930": 0}))   # 완전 청산
        r = _by_key(res, "p#1")
        assert r["confidence"] == L.CONF_HIGH and r["state"] == "filled"
        assert r["filled"] == 10 and not L.is_locked("005930")
    print("[PASS] 정확 full SELL(잔고 0) → HIGH·잠금해제")


def test_low_no_move_oversell_guard():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        res = R.reconcile_unknowns_kr(_bal({"005930": 10}))  # 그대로(미체결)
        r = _by_key(res, "p#1")
        assert r["confidence"] == L.CONF_LOW and r["candidates"] == 0
        assert L.is_locked("005930")                         # 잠금 유지 = 재매도 차단
    print("[PASS] 미체결(잔고 불변) → LOW·잠금유지(초과매도 방지 핵심)")


def test_low_partial():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        res = R.reconcile_unknowns_kr(_bal({"005930": 6}))   # 4주만 팔림
        assert _by_key(res, "p#1")["confidence"] == L.CONF_LOW
        assert L.is_locked("005930")
    print("[PASS] 부분체결(delta<Q) → LOW(자동해소 금지)")


def test_low_two_unknowns_same_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "a#1", "005930", 5, "SELL", hldg_before=10)
        _mk_unknown(L, "b#1", "005930", 5, "SELL", hldg_before=10)
        res = R.reconcile_unknowns_kr(_bal({"005930": 0}))   # net는 깨끗해 보여도
        assert all(r["confidence"] == L.CONF_LOW for r in res)
        assert L.is_locked("005930")
    print("[PASS] 동일종목 UNKNOWN 2건 → 둘 다 LOW(net 귀속 불가)")


def test_low_query_fail_and_incomplete():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        assert R.reconcile_unknowns_kr(None)[0]["confidence"] == L.CONF_LOW
        assert R.reconcile_unknowns_kr(_bal({}, rt="1"))[0]["confidence"] == L.CONF_LOW
        # 불완전(연속조회 남음) — 심볼 부재를 0주로 오해 금지
        r = R.reconcile_unknowns_kr(_bal({"005930": 0}, ctx="MOREKEY"))[0]
        assert r["confidence"] == L.CONF_LOW and L.is_locked("005930")
    print("[PASS] 조회실패·rt_cd≠0·불완전 응답 → LOW")


def test_low_ownership_gates():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, own, R = _setup(tmp)
        # 사용자 기보유(baseline) 심볼 → 잔고가 봇+사용자 혼합 → LOW
        own.capture_baseline([{"pdno": "005930"}])
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        assert R.reconcile_unknowns_kr(_bal({"005930": 0}))[0]["confidence"] == L.CONF_LOW
    print("[PASS] 사용자 기보유(baseline) 종목 → LOW")


def test_low_unarmed():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, own, R = _setup(tmp)
        os.remove(os.environ["USER_BASELINE_PATH"])          # 미armed
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        assert R.reconcile_unknowns_kr(_bal({"005930": 0}))[0]["confidence"] == L.CONF_LOW
    print("[PASS] ownership 미armed → LOW(전 거부)")


def test_low_no_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=None)
        assert R.reconcile_unknowns_kr(_bal({"005930": 0}))[0]["confidence"] == L.CONF_LOW
    print("[PASS] before 스냅샷 없음 → LOW")


def test_freeze_impossible_sell():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, own, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=3)   # Q>before
        r = R.reconcile_unknowns_kr(_bal({"005930": 0}))[0]
        assert r["confidence"] == L.CONF_LOW and own.is_frozen("005930")
    print("[PASS] impossible sell(Q>before) → LOW·동결")


def test_freeze_anomaly_increase():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, own, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        r = R.reconcile_unknowns_kr(_bal({"005930": 15}))[0]         # 오히려 증가
        assert r["confidence"] == L.CONF_LOW and own.is_frozen("005930")
    print("[PASS] 방향역전(잔고 증가) → LOW·동결")


def test_buy_always_low():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 5, "BUY", hldg_before=0)
        res = R.reconcile_unknowns_kr(_bal({"005930": 5}))           # full-fill 처럼 보여도
        assert res[0]["confidence"] == L.CONF_LOW and L.is_locked("005930")
    print("[PASS] BUY → 항상 LOW(신뢰 근거 없음)")


def test_us_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        kis, L, _, R = _setup(tmp)
        _mk_unknown(L, "u#1", "AAPL", 3, "SELL", hldg_before=3, market="US")
        res = R.reconcile_unknowns_kr(_bal({}))
        assert res == [] and L.is_locked("AAPL")                     # KR 대사가 안 건드림
    print("[PASS] US UNKNOWN은 KR 대사가 건드리지 않음")


def test_idempotent_after_high():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, _, R = _setup(tmp)
        _mk_unknown(L, "p#1", "005930", 10, "SELL", hldg_before=10)
        R.reconcile_unknowns_kr(_bal({"005930": 0}))
        res2 = R.reconcile_unknowns_kr(_bal({"005930": 0}))          # 두 번째 = no-op
        assert res2 == [] and not L.is_locked("005930")
    print("[PASS] HIGH 해소 후 재호출 no-op(멱등)")


def main():
    test_high_full_sell()
    test_low_no_move_oversell_guard()
    test_low_partial()
    test_low_two_unknowns_same_symbol()
    test_low_query_fail_and_incomplete()
    test_low_ownership_gates()
    test_low_unarmed()
    test_low_no_snapshot()
    test_freeze_impossible_sell()
    test_freeze_anomaly_increase()
    test_buy_always_low()
    test_us_untouched()
    test_idempotent_after_high()
    for k in ("USER_BASELINE_PATH", "SYMBOL_FREEZE_PATH"):
        os.environ.pop(k, None)
    print("\n국내 잔고대사 안전정리 통과 — SELL full-fill만 자동, 나머지 LOW/동결.")


if __name__ == "__main__":
    main()
