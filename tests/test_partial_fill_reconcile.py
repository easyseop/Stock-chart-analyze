"""부분체결 주문도 브로커 조회 대상이어야 한다 — TRUP 실측 재현.

실측 2026-09-02: TRUP 매도 7주가 브로커에서 전량 체결됐는데(odno 41895
@28.08128571) 원장은 `partial 1/7 · accounted=1`에 멈춰 6주가 미회계로 남았다.
`resolve_acks_from_rows`는 partial을 이미 받는데도(kis_reconcile.py:321) 볼
체결행이 없었다 — 브로커 조회 대상 목록(`_resolve_acks`의 `aged`)이
submitted/ack만 포함해 그 주문의 거래소·날짜를 애초에 읽지 않았기 때문이다.

다른 주문이 우연히 같은 거래소·날짜를 조회해 주지 않으면 스스로는 못 빠져나온다.
"""
import datetime
import tempfile
import time
from zoneinfo import ZoneInfo

import pytest

from bot import kis_boot, kis_reconcile, ledger as L


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", f"{tmp}/orders.jsonl")
    monkeypatch.setenv("KIS_RECONCILE_STATUS_PATH", f"{tmp}/status.json")
    monkeypatch.setenv("USER_BASELINE_PATH", f"{tmp}/baseline.json")
    monkeypatch.setenv("SYMBOL_FREEZE_PATH", f"{tmp}/freeze.json")
    for path, body in ((f"{tmp}/baseline.json", '{"symbols": []}'),
                       (f"{tmp}/freeze.json", "{}")):
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(body)
    return tmp


def _partial_sell(key="xe:TRUP:time:2026-08-12#2", symbol="TRUP",
                  odno="0000041895", intended=7, filled=1, age_s=3600.0):
    L._append({"ev": "submit", "key": key, "symbol": symbol,
               "intended": intended, "filled": 0, "state": "submitted",
               "reason": "B 타임스탑 21일", "ts": time.time() - age_s,
               "meta": {"side": "SELL", "market": "US", "excg": "NASD",
                        "hldg_before": intended, "price": 28.08,
                        "pos_key": "sb:TRUP:TRUP-2026-08-12-shelf",
                        "sleeve": "B", "fx": 1380.0, "ccy": "USD"}})
    L.bind_broker_order(key, odno, ord_tmd="223200")
    L.on_result(key, "partial", filled, open_order=True)
    return key


def _et_day(ts) -> str:
    return datetime.datetime.fromtimestamp(
        float(ts), ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _run_resolve(monkeypatch, *, fills_by_day):
    """`kis_boot._resolve_acks`를 실제로 돌리고, 조회된 (거래소, 날짜)를 남긴다.

    필터를 테스트에서 복제하면 프로덕션 코드를 바꿔도 통과한다(동어반복).
    실제 함수를 호출하고 **브로커 조회가 일어났는지**로 판정한다.
    """
    from bot import kis, ownership
    queried: list[tuple[str, str]] = []

    def _fills(excg=None, start=None, end=None):
        queried.append((excg, start))
        return {"rt_cd": "0", "output": fills_by_day.get((excg, start), [])}

    monkeypatch.setattr(kis, "enabled", lambda: True)
    monkeypatch.setattr(kis, "open_orders",
                        lambda excg=None: {"rt_cd": "0", "output": []})
    monkeypatch.setattr(kis, "fills", _fills)
    monkeypatch.setattr(kis, "holdings", lambda market, excg=None: {})
    monkeypatch.setattr(kis, "positions_detail",
                        lambda market, excg=None: [])
    monkeypatch.setattr(ownership, "baseline", lambda: set())
    kis_boot._resolve_acks()
    return queried


def _ccnl(odno="0000041895", qty=7, price=28.08128571):
    return {"odno": odno, "pdno": "TRUP", "sll_buy_dvsn_cd": "01",
            "ft_ccld_qty": str(qty), "ft_ccld_unpr3": f"{price:.8f}",
            "ord_qty": "7"}


def test_partial_order_drives_a_broker_query(env, monkeypatch):
    """이것이 TRUP을 묶어둔 지점 — 조회가 일어나야 체결행을 볼 수 있다."""
    _partial_sell()
    queried = _run_resolve(monkeypatch, fills_by_day={})
    assert queried, "partial만 열려 있을 때 브로커 조회가 한 번도 없었다"
    assert any(excg == "NASD" for excg, _day in queried)


def test_partial_fill_completes_end_to_end(env, monkeypatch):
    """전량 체결행이 오면 잔량 6주까지 회계된다 — 실제 경로로."""
    key = _partial_sell()
    day = _et_day(L.state_of(key)["submitted_at"])
    _run_resolve(monkeypatch, fills_by_day={("NASD", day): [_ccnl()]})
    assert L.state_of(key)["filled"] == 7, L.state_of(key)


def test_young_partial_is_not_queried(env, monkeypatch):
    """갓 접수된 부분체결은 아직 조회하지 않는다(체결 진행 중일 수 있다)."""
    _partial_sell(age_s=1.0)
    assert _run_resolve(monkeypatch, fills_by_day={}) == []


def test_terminal_order_is_not_queried(env, monkeypatch):
    key = _partial_sell(key="xe:TRUP:done")
    L.on_result(key, "filled", 7, open_order=False)
    assert _run_resolve(monkeypatch, fills_by_day={}) == []


def test_absence_proof_never_closes_a_partial(env):
    """체결이 이미 있는 주문을 '거절'로 닫으면 안 된다.

    재확인 창을 **채운 뒤에도** 닫히지 않아야 한다 — 1회 관측만으로 판정하면
    하드닝의 표식 단계에 가려 이 계약이 검증되지 않는다.
    """
    key = _partial_sell()
    proof = {key: {"nccs_rows": [], "ccnl_rows": [], "holdings": {"TRUP": 7}}}
    t0 = time.time()
    kis_reconcile.resolve_acks_by_absence(proof, now_ts=t0,
                                          orders=L.open_orders())
    resolved, contradictions = kis_reconcile.resolve_acks_by_absence(
        proof, now_ts=t0 + kis_reconcile.ABSENCE_CONFIRM_WINDOW_S + 1,
        orders=L.open_orders())
    assert resolved == [] and contradictions == []
    assert L.state_of(key)["state"] == "partial"
