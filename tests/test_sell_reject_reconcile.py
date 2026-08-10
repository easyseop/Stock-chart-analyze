"""TAP 매도 거절 재발 방지: 부재 증명·사유·실패 가시화 검증."""
from __future__ import annotations

import os
import json
import io
import tempfile
import time
from unittest import mock

from bot import kis_boot as B
from bot import kis as K
from bot import kis_reconcile as R
from bot import ledger as L
from bot import ownership as O


def _paths(tmp: str) -> None:
    L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
    os.environ["KIS_RECONCILE_STATUS_PATH"] = os.path.join(tmp, "status.json")
    os.environ["USER_BASELINE_PATH"] = os.path.join(tmp, "baseline.json")
    os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
    with open(os.environ["USER_BASELINE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({"symbols": []}, fp)
    with open(os.environ["SYMBOL_FREEZE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({}, fp)
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
    assert R.trusted_response_rows(
        {"rt_cd": "0", "output": [], "_tr_cont": "F"}) is None
    assert R.trusted_response_rows(
        {"rt_cd": "0", "msg_cd": "20312000", "output": []}) is None
    assert R.trusted_response_rows(
        {"rt_cd": "0", "output": [{}] * 15}) is None
    raw = {"rt_cd": "0", "msg_cd": "20310000", "msg1": "query complete",
           "output": [{"odno": "38291", "ovrs_pdno": "TAP",
                       "sll_buy_dvsn_cd": "01", "ft_ord_qty": "80",
                       "ft_ccld_qty": "0", "nccs_qty": "80"}]}
    rows = R.trusted_response_rows(raw)
    norm = R.normalize_rows(None, {"rt_cd": "0", "output": rows})
    assert norm[0]["msg_cd"] == "20310000" and norm[0]["msg1"] == "query complete"
    assert norm[0]["msg_source"] == "response"
    assert norm[0]["broker_reason"] == ""    # 일반 조회완료를 거절사유로 과장 금지
    print("[PASS] 원응답: rt_cd·연속키·행 스키마 완전성 계약")


def test_kis_get_preserves_tr_cont_header():
    response = io.BytesIO(b'{"rt_cd":"0","msg_cd":"20310000","output":[]}')
    response.headers = {"tr_cont": "F"}
    with mock.patch.object(K, "_token", return_value="test-token"), \
            mock.patch.object(K, "_cred", return_value=("key", "secret")), \
            mock.patch.object(K._LIMITER, "acquire", return_value=True), \
            mock.patch.object(K.urllib.request, "urlopen", return_value=response):
        raw = K._get("/read-only", "TEST", {})
    assert raw["_tr_cont"] == "F"
    assert R.trusted_response_rows(raw) is None
    print("[PASS] KIS GET가 tr_cont 헤더를 보존해 절단 페이지를 차단")


def test_absence_evidence_counts_and_ownership_gate():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        order = _ack()
        unrelated_n = [{"odno": "91"}, {"odno": "92"}]
        unrelated_c = [{"odno": "93"}]
        rs, contradictions = R.resolve_acks_by_absence(
            _proof(order, nccs=unrelated_n, ccnl=unrelated_c))
        assert len(rs) == 1 and not contradictions
        meta = L.state_of(order["key"])["reconcile_meta"]
        assert meta["nccs_count"] == 2 and meta["ccnl_count"] == 1
        assert meta["odno_absent"] is True

    for mode in ("unarmed", "frozen", "baseline"):
        with tempfile.TemporaryDirectory() as tmp:
            _paths(tmp)
            order = _ack()
            if mode == "unarmed":
                os.unlink(os.environ["USER_BASELINE_PATH"])
            elif mode == "frozen":
                with open(os.environ["SYMBOL_FREEZE_PATH"], "w",
                          encoding="utf-8") as fp:
                    json.dump({"TAP": {"why": "operator freeze"}}, fp)
            else:
                with open(os.environ["USER_BASELINE_PATH"], "w",
                          encoding="utf-8") as fp:
                    json.dump({"symbols": ["TAP"]}, fp)
            rs, contradictions = R.resolve_acks_by_absence(_proof(order))
            assert rs == contradictions == []
            assert L.state_of(order["key"])["state"] == "ack"
    print("[PASS] 부재 meta 실측 행수·미armed/동결/baseline 자동종결 차단")


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


def test_absence_reject_requires_single_symbol_inflight():
    """같은 종목의 fresh broker 주문이 공존하면 오래된 ACK를 단독 종결하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        old = _ack()
        _ack(key="xe:TAP:btgt:2026-08-11#1", qty=5, before=80,
             age_s=10, odno="0000038292")
        rs, contradictions = R.resolve_acks_by_absence(_proof(old))
        assert rs == contradictions == []
        assert L.state_of(old["key"])["state"] == "ack"
    print("[PASS] 동일 종목 fresh in-flight 공존 → 오래된 ACK 부재증명 금지")


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


def test_us_absence_scans_all_exchanges_and_keeps_live_order():
    """원장 거래소가 틀리거나 없어도 다른 미국 거래소의 주문을 놓치지 않는다."""
    for recorded_excg in ("NASD", ""):
        with tempfile.TemporaryDirectory() as tmp:
            _paths(tmp)
            order = _ack(excg=recorded_excg)
            empty = {"rt_cd": "0", "output": []}
            live = {"rt_cd": "0", "output": [{
                "odno": "38291", "ovrs_pdno": "TAP",
                "sll_buy_dvsn_cd": "01", "ft_ord_qty": "80",
                "ft_ccld_qty": "0", "nccs_qty": "80",
            }]}
            queried = []

            def open_orders(excg="NASD"):
                queried.append(("nccs", excg))
                return live if excg == "NYSE" else empty

            def fills(excg="NASD", start="", end=""):
                queried.append(("ccnl", excg))
                return empty

            with mock.patch.object(B.kis, "market_of_symbol", return_value="US"), \
                    mock.patch.object(B.kis, "open_orders", side_effect=open_orders), \
                    mock.patch.object(B.kis, "fills", side_effect=fills), \
                    mock.patch.object(B.kis, "holdings",
                                      side_effect=lambda market, excg=None: {"TAP": 80}), \
                    mock.patch.object(B.kis, "enabled", return_value=False), \
                    mock.patch.object(B, "_notify") as sent:
                assert B._resolve_acks() == []
            assert {ex for kind, ex in queried if kind == "nccs"} == {
                "NASD", "NYSE", "AMEX"}
            assert L.state_of(order["key"])["state"] == "ack"
            assert sent.call_count == 0
    print("[PASS] US 3거래소 union: NYSE 생존 주문을 NASD/미기재 원장도 보존")


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
        live_fallback = mock.Mock(side_effect=AssertionError("live fallback called"))
        with mock.patch.object(B.kis, "market_of_symbol", return_value="KR"), \
                mock.patch.object(B.kis, "IS_MOCK", False), \
                mock.patch.object(B.kis, "domestic_open_orders", return_value=None), \
                mock.patch.object(B.kis, "domestic_unfilled_orders",
                                  live_fallback), \
                mock.patch.object(B.kis, "domestic_fills", return_value=empty), \
                mock.patch.object(B.kis, "holdings", return_value={"005930": 80}), \
                mock.patch.object(B.kis, "enabled", return_value=False), \
                mock.patch.object(B, "_notify"):
            assert B._resolve_acks() == []
        assert live_fallback.call_count == 0
        assert L.state_of(order["key"])["state"] == "ack"
    print("[PASS] KR mock 강한 폴백은 부재증명 · live 폴백 호출 0")


def test_reconcile_failure_streak_alert_once_and_reset():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        sent = []
        with mock.patch.object(B, "RECONCILE_FAILURE_ALERT_N", 2), \
                mock.patch.object(B, "_notify",
                                  side_effect=lambda text, **kw: sent.append(text) or True):
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


def test_reconcile_failure_alert_latches_only_after_delivery():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        delivery = mock.Mock(side_effect=[False, True])
        with mock.patch.object(B, "RECONCILE_FAILURE_ALERT_N", 2), \
                mock.patch.object(B, "_notify", delivery):
            B._record_failure("first")
            B._record_failure("delivery down")     # 첫 임계 경보 실패
            with open(os.environ["KIS_RECONCILE_STATUS_PATH"],
                      encoding="utf-8") as fp:
                assert json.load(fp)["failure_alerted"] is False
            B._record_failure("retry succeeds")    # 다음 사이클 재시도 성공
            with open(os.environ["KIS_RECONCILE_STATUS_PATH"],
                      encoding="utf-8") as fp:
                assert json.load(fp)["failure_alerted"] is True
            B._record_failure("no duplicate")
        assert delivery.call_count == 2
    print("[PASS] 대사 연속실패 경보는 전송 실패 시 재시도·성공 뒤에만 래치")


def test_reconcile_success_during_alert_does_not_relock_old_failure():
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)

        def succeeds_after_recovery(*_args, **_kwargs):
            B._record_success()
            return True

        with mock.patch.object(B, "RECONCILE_FAILURE_ALERT_N", 1), \
                mock.patch.object(B, "_notify", side_effect=succeeds_after_recovery):
            B._record_failure("transient")
        with open(os.environ["KIS_RECONCILE_STATUS_PATH"],
                  encoding="utf-8") as fp:
            state = json.load(fp)
        assert state["failure_streak"] == 0
        assert state["failure_alerted"] is False
    print("[PASS] 경보 전송 중 성공 대사가 와도 지난 실패 래치 재잠금 없음")


def test_shared_health_file_cannot_open_local_trading_gate():
    """다른 프로세스가 쓴 진단 상태는 done/low를 전달하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        _paths(tmp)
        path = os.environ["KIS_RECONCILE_STATUS_PATH"]
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"done": True, "low": 0, "failure_streak": 4,
                       "last_error": "peer query failed"}, fp)
        B._STATE.update(done=False, low=3, last_success_at=None,
                        failure_streak=0, last_error="", failure_alerted=False)
        B._record_success()
        assert B._STATE["done"] is False and B._STATE["low"] == 3
        with open(path, encoding="utf-8") as fp:
            persisted = json.load(fp)
        assert "done" not in persisted and "low" not in persisted
        assert persisted["failure_streak"] == 0
    print("[PASS] 공유 health 파일은 로컬 done/low 매매 게이트와 완전 분리")


def main():
    test_tap_absence_proof_rejects_and_records_evidence()
    test_raw_response_trust_contract()
    test_kis_get_preserves_tr_cont_header()
    test_absence_evidence_counts_and_ownership_gate()
    test_failure_is_never_absence_and_age_gate()
    test_order_presence_partial_and_balance_contradiction_hold()
    test_absence_reject_requires_single_symbol_inflight()
    test_closed_row_reason_is_sanitized_and_bounded()
    test_boot_tap_path_notifies_once_and_contradiction_does_not_fall_through()
    test_us_absence_scans_all_exchanges_and_keeps_live_order()
    test_kr_mock_fallback_and_live_prohibition()
    test_reconcile_failure_streak_alert_once_and_reset()
    test_reconcile_failure_alert_latches_only_after_delivery()
    test_reconcile_success_during_alert_does_not_relock_old_failure()
    test_shared_health_file_cannot_open_local_trading_gate()
    print("\n매도 거절 대사 검증 통과.")


if __name__ == "__main__":
    main()
