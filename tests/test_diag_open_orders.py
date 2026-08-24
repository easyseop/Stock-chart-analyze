"""`/진단`의 열린 주문 카운터와 잔고 구간 heartbeat 회귀 고정.

실측 2026-08-24 22:31 KST: INGR SELL이 3일째 열려 있고 F3가 "보호매도 판단
4306분 차단"을 P0로 발행하는 동안, 같은 `/진단` 응답이 "원장: 열린 주문 0 ✅"를
출력했다. 원인은 카운터가 `_fold()` 행에 존재하지 않는 `open_order` 키를 보고
있어 **입력과 무관하게 항상 0**이었던 것. 관측이 조용히 거짓이면 운영자는
차단 상태를 정상으로 읽는다.
"""
import os
import tempfile

import pytest


@pytest.fixture()
def led(monkeypatch):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "led.jsonl")
    from bot import ledger
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    return ledger


def _open_n(ledger) -> int:
    return sum(1 for c in ledger._fold().values() if ledger.fold_is_open(c))


def test_open_counter_matches_open_orders(led):
    led.record_submit("k1", "INGR", 5, "half", {"side": "SELL"})
    led.on_result("k1", "ack", 0, open_order=True)
    assert _open_n(led) == len(led.open_orders()) == 1


def test_counter_is_not_hardwired_to_zero(led):
    for i in range(3):
        led.record_submit(f"k{i}", f"S{i}", 1, "x", {"side": "SELL"})
    assert _open_n(led) == 3


def test_terminal_states_are_not_open(led):
    led.record_submit("k1", "AAA", 1, "x", {"side": "SELL"})
    led.on_result("k1", "filled", 1)
    led.record_submit("k2", "BBB", 1, "x", {"side": "SELL"})
    led.on_result("k2", "rejected", 0)
    assert _open_n(led) == len(led.open_orders()) == 0


def test_closed_partial_is_not_open(led):
    led.record_submit("k1", "CCC", 5, "x", {"side": "SELL"})
    led.on_result("k1", "partial", 2, open_order=False)
    assert _open_n(led) == len(led.open_orders()) == 0
    led.on_result("k1", "partial", 2, open_order=True)
    assert _open_n(led) == len(led.open_orders()) == 1


def test_balance_loop_beats_between_markets(monkeypatch):
    """시장별 잔고 조회 사이에도 전진 증거가 남아야 한다.

    beat가 3콜 전체를 감싸는 하나뿐이면 heartbeat 나이가 세 콜의 **합계**로
    누적돼 60s(P0)·120s(L1 상향) 경계를 상시 왕복한다.
    """
    from bot import kis, kis_positions, sentinel, settings

    beats: list[dict] = []
    calls: list[tuple[str, str]] = []

    def _positions_detail(market, excg=None):
        calls.append((market, excg))
        # 각 블로킹 콜이 시작되는 시점에 직전 beat가 이미 찍혀 있어야 한다.
        assert len(beats) == len(calls)
        return []

    monkeypatch.setattr(kis, "positions_detail", _positions_detail)
    monkeypatch.setattr(kis_positions, "load", lambda: {})
    monkeypatch.setattr(settings, "market_open", lambda ccy: ccy == "USD")

    broker = sentinel._KisBroker.__new__(sentinel._KisBroker)
    broker.on_beat = lambda **kw: beats.append(kw)

    assert broker.holdings() == {}
    assert calls == [("US", "NASD"), ("US", "NYSE"), ("US", "AMEX")]
    assert [b["excg"] for b in beats] == ["NASD", "NYSE", "AMEX"]
    assert all(b["phase"] == "balance" for b in beats)


def test_holdings_survives_missing_beat_callback(monkeypatch):
    """콜백 미주입(paper·구버전 어댑터)에도 잔고 조회는 그대로 돈다."""
    from bot import kis, kis_positions, sentinel, settings

    monkeypatch.setattr(kis, "positions_detail", lambda m, excg=None: [])
    monkeypatch.setattr(kis_positions, "load", lambda: {})
    monkeypatch.setattr(settings, "market_open", lambda ccy: ccy == "USD")
    broker = sentinel._KisBroker.__new__(sentinel._KisBroker)
    assert broker.on_beat is None
    assert broker.holdings() == {}


def test_check_once_injects_beat_callback(monkeypatch):
    """파수꾼이 사이클마다 콜백을 실제로 꽂아준다 — 배선 회귀 고정."""
    from bot import sentinel

    broker = sentinel._PaperBroker()
    state: dict = {}
    monkeypatch.setattr(sentinel, "_reconcile_open", lambda b: None)
    monkeypatch.setattr(sentinel, "_fetch_positions",
                        lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    try:
        sentinel.check_once(broker, state)
    except RuntimeError:
        pass
    assert callable(broker.on_beat)
