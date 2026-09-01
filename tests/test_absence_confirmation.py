"""부재 증명 재확인 게이트 — 단일 스냅샷이 브로커 저널 지연을 '부재'로 오독했다.

실측 2건(2026-08-26 · 08-28):

  PAAS  08-26 11:01 ET 매도 5주 제출(odno 41030) → 같은 세션 5주 @52.99 체결.
        11:12 ET(제출 11분 뒤) 부재 증명이 돌 때 ccnl에 체결행이 아직 없었고
        잔고도 5주 그대로였다 → `rejected·filled=0`. 실제로는 전량 체결.
        결과: 유령 포지션(원장 5주 · 브로커 0주).
  OMCL  08-28 매수 1주 × 2건(odno 44603·44854)이 같은 경로로 오종결.
        결과: 무보호 고아 2주 — 손절선 없이 방치, 사후 감사로만 발견.

두 건 다 나중에 조회하면 체결행이 **보인다**. 저널은 늦을 뿐 틀리지 않는다.
그래서 해법은 더 엄격한 증거가 아니라 간격을 둔 재확인이다.
"""
import json
import tempfile
import time

import pytest

from bot import kis_reconcile as R
from bot import ledger as L


@pytest.fixture(autouse=True)
def _ledger(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", f"{tmp}/led.jsonl")
    # 부재 증명은 소유 경계가 무장된 계정에서만 돈다(미무장이면 게이트 이전에
    #   빠진다). 실제 경로와 같은 전제를 만든다 — baseline 빈 집합·동결 없음.
    monkeypatch.setenv("USER_BASELINE_PATH", f"{tmp}/baseline.json")
    monkeypatch.setenv("SYMBOL_FREEZE_PATH", f"{tmp}/freeze.json")
    with open(f"{tmp}/baseline.json", "w", encoding="utf-8") as fp:
        json.dump({"symbols": []}, fp)
    with open(f"{tmp}/freeze.json", "w", encoding="utf-8") as fp:
        json.dump({}, fp)
    yield


def _order(key="xe:PAAS:time:2026-08-06#1", symbol="PAAS", side="SELL",
           qty=5, before=5, odno="0000041030", age_s=601.0):
    L._append({"ev": "submit", "key": key, "symbol": symbol, "intended": qty,
               "filled": 0, "state": "submitted", "reason": "time stop",
               "ts": time.time() - age_s,
               "meta": {"side": side, "market": "US", "excg": "NYSE",
                        "hldg_before": before, "price": 52.99}})
    L.bind_broker_order(key, odno, ord_tmd="110100")
    L.on_result(key, "ack", 0)
    return key, symbol, before


def _proof(key, symbol, before, *, nccs=None, ccnl=None, holdings=None):
    return {key: {"nccs_rows": [] if nccs is None else nccs,
                  "ccnl_rows": [] if ccnl is None else ccnl,
                  "holdings": {symbol: before} if holdings is None else holdings}}


def test_single_observation_never_closes():
    """이것이 PAAS·OMCL을 만든 경로다 — 한 번 봤다고 닫으면 안 된다."""
    key, sym, before = _order()
    resolved, contradictions = R.resolve_acks_by_absence(_proof(key, sym, before))
    assert resolved == [] and contradictions == []
    assert L.state_of(key)["state"] == "ack", "단일 스냅샷으로 종결됐다"


def test_first_observation_records_a_durable_marker():
    key, sym, before = _order()
    R.resolve_acks_by_absence(_proof(key, sym, before))
    meta = L.state_of(key)["reconcile_meta"]
    assert meta["absence_first_at"] > 0
    assert meta["absence_first_hldg"] == before


def test_second_observation_after_window_closes():
    key, sym, before = _order()
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before), now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S + 1)
    assert len(resolved) == 1 and resolved[0]["state"] == "rejected"
    assert L.state_of(key)["state"] == "rejected"


def test_second_observation_inside_window_does_not_close():
    """간격 없는 재확인은 재확인이 아니다 — 같은 지연 스냅샷을 두 번 볼 뿐."""
    key, sym, before = _order()
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before), now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S - 1)
    assert resolved == [] and L.state_of(key)["state"] == "ack"


def test_paas_incident_is_prevented():
    """PAAS 재현 — 1회차엔 저널이 비어 보이고, 2회차에 체결행이 나타난다."""
    key, sym, before = _order()
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)   # 저널 지연
    late_fill = [{"odno": "0000041030", "pdno": "PAAS", "side": "SELL",
                  "ft_ccld_qty": "5", "ft_ccld_unpr3": "52.99"}]
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=late_fill),
        now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S + 1)
    assert resolved == [], "뒤늦게 나타난 체결행을 무시하고 종결했다"
    assert L.state_of(key)["state"] == "ack"


def test_omcl_incident_is_prevented():
    """OMCL 재현 — 매수 1주가 2회차에 저널에 나타난다."""
    key, sym, before = _order(key="kb:OMCL:OMCL-2026-08-28-now", symbol="OMCL",
                              side="BUY", qty=1, before=0, odno="0000044603")
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    late = [{"odno": "0000044603", "pdno": "OMCL", "side": "BUY",
             "ft_ccld_qty": "1", "ft_ccld_unpr3": "32.96"}]
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=late),
        now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S + 1)
    assert resolved == []
    assert L.state_of(key)["state"] == "ack"


def test_balance_move_disarms_the_marker():
    """증거가 깨지면 표식을 해제한다 — 낡은 표식으로 즉시 종결하면 안 된다."""
    key, sym, before = _order()
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    assert L.state_of(key)["reconcile_meta"]["absence_first_at"] > 0
    # 잔고가 움직였다 = 모순 → 무장 해제
    _, contradictions = R.resolve_acks_by_absence(
        _proof(key, sym, before, holdings={sym: before - 5}),
        now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S + 1)
    assert len(contradictions) == 1
    assert not L.state_of(key)["reconcile_meta"]["absence_first_at"]
    # 증거가 다시 깨끗해져도 창을 처음부터 다시 채워야 한다
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before), now_ts=t0 + R.ABSENCE_CONFIRM_WINDOW_S + 2)
    assert resolved == [], "해제된 표식이 무시되고 즉시 종결됐다"


def test_marker_survives_a_fold_from_disk():
    """표식은 append-only 원장에 있으므로 재기동에도 살아남는다."""
    key, sym, before = _order()
    t0 = time.time()
    R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    reread = {o["key"]: o for o in L.open_orders()}[key]
    assert reread["reconcile_meta"]["absence_first_at"] > 0


@pytest.mark.parametrize("bogus", [None, 0, -1, float("nan"), "abc"])
def test_corrupt_marker_is_treated_as_first_observation(bogus):
    """표식이 손상되면 '아직 못 봤다'로 본다 — fail-closed."""
    key, sym, before = _order()
    if bogus is not None:
        L.record_reconcile_meta(key, reason="test",
                                meta={"absence_first_at": bogus})
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before), now_ts=time.time() + 10_000)
    assert resolved == [], f"손상 표식({bogus!r})으로 즉시 종결됐다"


def test_future_marker_is_rejected():
    """시계 되감김·조작된 표식이 창을 건너뛰지 못한다."""
    key, sym, before = _order()
    t0 = time.time()
    L.record_reconcile_meta(key, reason="test",
                            meta={"absence_first_at": t0 + 86_400})
    resolved, _ = R.resolve_acks_by_absence(_proof(key, sym, before), now_ts=t0)
    assert resolved == []
