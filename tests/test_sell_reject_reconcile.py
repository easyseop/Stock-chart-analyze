"""TAP 매도 거절 재발 방지: 부재 증명·사유·실패 가시화 검증."""
from __future__ import annotations

import os
import tempfile
import time
from unittest import mock

from bot import kis_boot as B
from bot import kis_reconcile as R
from bot import ledger as L


def _paths(tmp: str) -> None:
    L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
    os.environ["KIS_RECONCILE_STATUS_PATH"] = os.path.join(tmp, "status.json")
    B._STATE.update(done=False, low=0, last_success_at=None,
                    failure_streak=0, last_error="", failure_alerted=False)


def _ack(key: str = "xe:TAP:time:2026-07-20", *, symbol: str = "TAP",
         side: str = "SELL", qty: int = 80, before: int = 80,
         age_s: float = 601, market: str = "US", excg: str = "NYSE",
         odno: str = "0000038291") -> dict:
    L._append({"ev": "submit", "key": key, "symbol": symbol,
               "intended": qty, "filled": 0, "state": "submitted",
               "reason": "time stop", "ts": time.time() - age_s,
               "meta": {"side": side, "hldg_before": before,
                        "market": market, "excg": excg}})
    L.bind_broker_order(key, odno, ord_tmd="223114")
    L.on_result(key, "ack", 0)
    return {"key": key, **(L.state_of(key) or {})}


def _proof(order: dict, *, nccs=None, ccnl=None, holdings=None) -> dict:
    return {order["key"]: {
        "nccs_rows": [] if nccs is None else nccs,
        "ccnl_rows": [] if ccnl is None else ccnl,
        "holdings": {order["symbol"]: int(order["hldg_before"])}
        if holdings is None else holdings,
    }}


def test_tap_absence_proof_rejects_and_records_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack()
        rs, contradictions = R.resolve_acks_by_absence(_proof(order))
        assert not contradictions and len(rs) == 1
        assert rs[0]["state"] == "rejected" and rs[0]["filled"] == 0
        state = L.state_of(order["key"])
        assert state["state"] == "rejected" and not state["open"]
        assert state["reconcile_reason"] == "absence-proof"
        meta = state["reconcile_meta"]
        assert meta["nccs_count"] == meta["ccnl_count"] == 0
        assert meta["hldg_before"] == meta["hldg_now"] == 80
        assert meta["broker_reason"] == "사유 미상(부재 증명)"
        assert R.resolve_acks_by_absence(_proof(order))[0] == []
    print("[PASS] TAP 형태: 600s+·두 주문조회 부재·잔고불변 → rejected+근거")


def test_raw_response_trust_contract():
    assert R.trusted_response_rows({"rt_cd": "0", "output": []}) == []
    assert R.trusted_response_rows({"rt_cd": "1", "output": []}) is None
    assert R.trusted_response_rows(
        {"rt_cd": "0", "output": [], "ctx_area_nk200": "NEXT"}) is None
    assert R.trusted_response_rows({"rt_cd": "0"}) is None
    assert R.trusted_response_rows({"rt_cd": "0", "output": {}}) is None
    assert R.trusted_response_rows(
        {"rt_cd": "0", "output1": [], "ctx_area_nk100": "NEXT"},
        domestic=True) is None
    print("[PASS] 원응답: rt_cd·연속키·행 스키마 완전성 계약")


def test_failure_is_never_absence_and_age_gate():
    bad_proofs = [
        {"nccs_rows": None, "ccnl_rows": [], "holdings": {"TAP": 80}},
        {"nccs_rows": [], "ccnl_rows": None, "holdings": {"TAP": 80}},
        {"nccs_rows": [], "ccnl_rows": [], "holdings": None},
        {"nccs_rows": {}, "ccnl_rows": [], "holdings": {"TAP": 80}},
        {"nccs_rows": [], "ccnl_rows": ["bad"], "holdings": {"TAP": 80}},
    ]
    for proof in bad_proofs:
        with tempfile.TemporaryDirectory() as tmp:
            _paths(tmp)
            order = _ack()
            rs, contradictions = R.resolve_acks_by_absence({order["key"]: proof})
            assert rs == contradictions == []
            assert L.state_of(order["key"])["state"] == "ack"
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack(age_s=599)
        rs, contradictions = R.resolve_acks_by_absence(_proof(order))
        assert rs == contradictions == [] and L.state_of(order["key"])["state"] == "ack"
    print("[PASS] None/불신형 응답 5종은 부재 아님 · 599s는 보류")


def test_order_presence_partial_and_balance_contradiction_hold():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack()
        same = {"ODNO": "38291"}
        rs, contradictions = R.resolve_acks_by_absence(_proof(order, ccnl=[same]))
        assert rs == contradictions == [] and L.state_of(order["key"])["state"] == "ack"
        rs, contradictions = R.resolve_acks_by_absence(
            _proof(order, holdings={"TAP": 79}))
        assert rs == [] and len(contradictions) == 1
        assert contradictions[0]["hldg_before"] == 80
        assert contradictions[0]["hldg_now"] == 79
        assert L.state_of(order["key"])["state"] == "ack"
    print("[PASS] ODNO 선행0 정규화·부분체결 존재·잔고 모순 → 자동정산 0")


def test_closed_row_reason_is_sanitized_and_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack()
        rows = [{"odno": "38291", "pdno": "TAP", "side": "SELL",
                 "ord_qty": 80, "filled": 0, "price": 0,
                 "src": "ccnl", "open": False,
                 "msg_cd": "20310000\x00", "msg1": "X\n" + "a" * 300,
                 "broker_status": ""}]
        with mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}):
            rs = R.resolve_acks_from_rows(rows)
        assert len(rs) == 1 and rs[0]["state"] == "rejected"
        meta = L.state_of(order["key"])["reconcile_meta"]
        assert len(meta["msg1"]) == 200 and "\n" not in meta["msg1"]
        assert meta["msg_cd"] == "20310000"
    print("[PASS] 브로커 종결행 msg_cd/msg1 저장·제어문자 제거·200자 상한")


def test_boot_tap_path_notifies_once_and_contradiction_does_not_fall_through():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        _ack()
        sent = []
        empty = {"rt_cd": "0", "output": []}
        with mock.patch.object(B.kis, "market_of_symbol", return_value="US"), \
                mock.patch.object(B.kis, "open_orders", return_value=empty), \
                mock.patch.object(B.kis, "fills", return_value=empty), \
                mock.patch.object(B.kis, "holdings",
                                  side_effect=lambda market, excg=None: {"TAP": 80}), \
                mock.patch.object(B.kis, "enabled", return_value=False), \
                mock.patch.object(B, "_notify",
                                  side_effect=lambda text, **kw: sent.append((text, kw))):
            rs = B._resolve_acks()
            assert len(rs) == 1 and rs[0]["state"] == "rejected"
            assert len(sent) == 1 and "부재 증명" in sent[0][0]
            assert sent[0][1]["critical"] is True
            assert B._resolve_acks() == [] and len(sent) == 1

    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack()
        sent = []
        empty = {"rt_cd": "0", "output": []}
        with mock.patch.object(B.kis, "market_of_symbol", return_value="US"), \
                mock.patch.object(B.kis, "open_orders", return_value=empty), \
                mock.patch.object(B.kis, "fills", return_value=empty), \
                mock.patch.object(B.kis, "holdings",
                                  side_effect=lambda market, excg=None: {"TAP": 79}), \
                mock.patch.object(B.kis, "enabled", return_value=False), \
                mock.patch.object(B, "_notify",
                                  side_effect=lambda text, **kw: sent.append(text)):
            assert B._resolve_acks() == []
            assert L.state_of(order["key"])["state"] == "ack"
            assert len([x for x in sent if "대사 모순" in x]) == 1
            assert B._resolve_acks() == []
            assert len([x for x in sent if "대사 모순" in x]) == 1
    print("[PASS] 부재증명 SELL 경보 1회 · 잔고모순은 종결/잔고대사 모두 차단")


def test_kr_mock_fallback_and_live_prohibition():
    empty = {"rt_cd": "0", "output": []}
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        _ack(key="xe:005930:time:2026-07-20", symbol="005930", market="KR",
             excg="KRX", odno="0007")
        fallback_calls = []
        with mock.patch.object(B.kis, "market_of_symbol", return_value="KR"), \
                mock.patch.object(B.kis, "IS_MOCK", True), \
                mock.patch.object(B.kis, "domestic_open_orders", return_value=None), \
                mock.patch.object(B.kis, "domestic_unfilled_orders",
                                  side_effect=lambda: fallback_calls.append(1) or empty), \
                mock.patch.object(B.kis, "domestic_fills", return_value=empty), \
                mock.patch.object(B.kis, "holdings", return_value={"005930": 80}), \
                mock.patch.object(B.kis, "enabled", return_value=False), \
                mock.patch.object(B, "_notify"):
            assert B._resolve_acks()[0]["state"] == "rejected"
        assert fallback_calls == [1]

    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack(key="xe:005930:time:2026-07-20", symbol="005930",
                     market="KR", excg="KRX", odno="0007")
        with mock.patch.object(B.kis, "market_of_symbol", return_value="KR"), \
                mock.patch.object(B.kis, "IS_MOCK", False), \
                mock.patch.object(B.kis, "domestic_open_orders", return_value=None), \
                mock.patch.object(B.kis, "domestic_unfilled_orders",
                                  side_effect=AssertionError("live fallback called")), \
                mock.patch.object(B.kis, "domestic_fills", return_value=empty), \
                mock.patch.object(B.kis, "holdings", return_value={"005930": 80}), \
                mock.patch.object(B.kis, "enabled", return_value=False), \
                mock.patch.object(B, "_notify"):
            assert B._resolve_acks() == []
        assert L.state_of(order["key"])["state"] == "ack"
    print("[PASS] KR mock 강한 폴백은 부재증명 · live 폴백 호출 0")


def test_reconcile_failure_streak_alert_once_and_reset():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        sent = []
        with mock.patch.object(B, "RECONCILE_FAILURE_ALERT_N", 2), \
                mock.patch.object(B, "_notify",
                                  side_effect=lambda text, **kw: sent.append(text)):
            B._record_failure("nccs unavailable")
            assert B.reconcile_health()["failure_streak"] == 1 and not sent
            B._record_failure("ccnl unavailable")
            assert B.reconcile_health()["failure_streak"] == 2 and len(sent) == 1
            B._record_failure("balance unavailable")
            assert B.reconcile_health()["failure_streak"] == 3 and len(sent) == 1
            B._record_success()
        health = B.reconcile_health()
        assert health["failure_streak"] == 0 and health["last_success_at"]
    print("[PASS] 대사 실패 streak 증가·임계 1회 경보·성공 리셋")


def main():
    test_tap_absence_proof_rejects_and_records_evidence()
    test_raw_response_trust_contract()
    test_failure_is_never_absence_and_age_gate()
    test_order_presence_partial_and_balance_contradiction_hold()
    test_closed_row_reason_is_sanitized_and_bounded()
    test_boot_tap_path_notifies_once_and_contradiction_does_not_fall_through()
    test_kr_mock_fallback_and_live_prohibition()
    test_reconcile_failure_streak_alert_once_and_reset()
    print("\n매도 거절 대사 검증 통과.")


if __name__ == "__main__":
    main()
