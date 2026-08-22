"""갇힌 보호 SELL/CANCEL·설명되지 않는 매도가능 고갈 관측 회귀."""
from __future__ import annotations

import importlib
import inspect
import json
import os
import tempfile
import time
from unittest import mock


def _setup(tmp: str):
    os.environ["ORDER_LEDGER_PATH"] = os.path.join(tmp, "orders.jsonl")
    os.environ["PROTECTION_ALERT_LATCH_PATH"] = os.path.join(tmp, "alerts.json")
    os.environ["PROTECTION_BLOCKED_ALERT_S"] = "60"
    os.environ["SELLABLE_GAP_AUDIT_S"] = "60"
    os.environ["SELLABLE_GAP_CONFIRMATIONS"] = "2"
    from bot import ledger, protection_observability as watch
    importlib.reload(ledger)
    importlib.reload(watch)
    ledger.LEDGER_PATH = os.environ["ORDER_LEDGER_PATH"]
    return ledger, watch


def _open_sell(ledger, key: str, symbol: str, *, age_s: float = 61,
               side: str = "SELL"):
    ledger._append({
        "ev": "submit", "key": key, "symbol": symbol, "intended": 5,
        "filled": 0, "state": "submitted", "reason": "test",
        "meta": {"side": side, "market": "US", "hldg_before": 11},
        "ts": time.time() - age_s,
    })
    ledger.on_result(key, "ack", 0, open_order=True)


def test_f3_blocked_protection_alert_once_persists_and_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        ledger, watch = _setup(tmp)
        _open_sell(ledger, "sell:ingr", "INGR")
        held = {"INGR": {"q": 11}}
        sent = []
        with mock.patch("bot.notify.send",
                        side_effect=lambda text, **kw: sent.append((text, kw)) or True):
            assert watch.audit_blocked_protection(
                held, scope_markets={"US"})
            assert not watch.audit_blocked_protection(
                held, scope_markets={"US"})
            # 프로세스 재시작 뒤에도 파일 래치가 중복을 막는다.
            importlib.reload(watch)
            assert not watch.audit_blocked_protection(
                held, scope_markets={"US"})
            ledger.reconcile("sell:ingr", 0, open_order=False)
            assert watch.audit_blocked_protection(
                held, scope_markets={"US"})
            assert not watch.audit_blocked_protection(
                held, scope_markets={"US"})
        assert len(sent) == 2
        assert "보호매도 판단" in sent[0][0] and "INGR" in sent[0][0]
        assert "해소" in sent[1][0]
        assert all(call[1].get("critical") is True
                   and call[1].get("category") == "trade" for call in sent)
        # 문구에 수량·금액·계좌는 싣지 않는다.
        assert "11주" not in sent[0][0] and "계좌" not in sent[0][0]
    print("[PASS] F3 오래된 보호차단 P0 1회·재시작 래치·해소 1회·무시크릿")


def test_f3_short_inflight_and_untrusted_ledger_are_silent():
    with tempfile.TemporaryDirectory() as tmp:
        ledger, watch = _setup(tmp)
        _open_sell(ledger, "sell:short", "SHORT", age_s=59)
        with mock.patch("bot.notify.send") as send:
            assert not watch.audit_blocked_protection(
                {"SHORT": {"q": 2}}, scope_markets={"US"})
        assert not send.called
        with open(ledger.LEDGER_PATH, "a", encoding="utf-8") as fp:
            fp.write("{broken\n")
        with mock.patch("bot.notify.send") as send2:
            assert not watch.audit_blocked_protection(
                {"SHORT": {"q": 2}}, scope_markets={"US"})
        assert not send2.called
    print("[PASS] F3 짧은 in-flight·원장불신은 경보/해소 판정 보류")


def test_alert_delivery_failure_is_not_latched():
    with tempfile.TemporaryDirectory() as tmp:
        ledger, watch = _setup(tmp)
        _open_sell(ledger, "sell:retry", "RETRY")
        delivery = mock.Mock(side_effect=[False, True])
        with mock.patch("bot.notify.send", delivery):
            assert not watch.audit_blocked_protection(
                {"RETRY": {"q": 3}}, scope_markets={"US"})
            assert watch.audit_blocked_protection(
                {"RETRY": {"q": 3}}, scope_markets={"US"})
            assert not watch.audit_blocked_protection(
                {"RETRY": {"q": 3}}, scope_markets={"US"})
        assert delivery.call_count == 2
    print("[PASS] 보호 P0 전송 실패는 래치하지 않고 다음 사이클 재시도")


def test_f4_gap_formula_latch_failure_and_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        _ledger, watch = _setup(tmp)
        held = {"INGR": {"q": 11}}
        sent = []
        alert = {"total": {"INGR": 11}, "sellable": {"INGR": 1},
                 "open_sell": {"INGR": 0}}
        explained = {"total": {"INGR": 11}, "sellable": {"INGR": 6},
                     "open_sell": {"INGR": 5}}
        partly_unexplained = {
            "total": {"INGR": 11}, "sellable": {"INGR": 1},
            "open_sell": {"INGR": 5}}
        with mock.patch("bot.notify.send",
                        side_effect=lambda text, **kw: sent.append((text, kw)) or True):
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=alert)
            assert watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=alert)
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=alert)
            importlib.reload(watch)
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=alert)
            # 조회 실패는 사고를 해소로 오독하지 않고 기존 래치를 유지한다.
            with mock.patch.object(watch, "_collect_sellable_snapshot",
                                   return_value=None):
                assert not watch.audit_sellable_gaps(
                    {}, scope_markets={"US"})
            assert json.load(open(os.environ["PROTECTION_ALERT_LATCH_PATH"],
                                  encoding="utf-8"))["sellable_gap"] == ["INGR"]
            assert watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=explained)
        assert len(sent) == 2 and "부족" in sent[0][0] and "해소" in sent[1][0]
        assert "90.9%" in sent[0][0]
        assert all(call[1].get("critical") and call[1].get("category") == "trade"
                   for call in sent)

        # 5주 열린 SELL로 전부 설명되면 침묵, 5주가 남으면 P0.
        with tempfile.TemporaryDirectory() as tmp2:
            _ledger2, watch2 = _setup(tmp2)
            with mock.patch("bot.notify.send", return_value=True) as send:
                assert not watch2.audit_sellable_gaps(
                    held, scope_markets={"US"}, snapshot=explained)
                assert not watch2.audit_sellable_gaps(
                    held, scope_markets={"US"}, snapshot=partly_unexplained)
                assert watch2.audit_sellable_gaps(
                    held, scope_markets={"US"}, snapshot=partly_unexplained)
            assert send.call_count == 1 and "45.5%" in send.call_args.args[0]
    print("[PASS] F4 11/1/0 P0·11/6/5 정상·11/1/5 P0·실패보류·영속래치·회복")


def test_f4_collects_complete_pages_and_partial_sell_remaining():
    with tempfile.TemporaryDirectory() as tmp:
        _ledger, watch = _setup(tmp)
        quantities = [
            {"total": {"INGR": 11}, "sellable": {"INGR": 6}},
            {"total": {}, "sellable": {}},
            {"total": {}, "sellable": {}},
        ]
        open_rows = [{"rt_cd": "0", "output": [{
            "odno": "1", "pdno": "INGR", "ft_ord_qty": "5",
            "ft_ccld_qty": "2", "nccs_qty": "3",
            "sll_buy_dvsn_cd_name": "매도"}]},
            {"rt_cd": "0", "output": []},
            {"rt_cd": "0", "output": []}]
        with mock.patch("bot.kis.holding_quantities", side_effect=quantities), \
             mock.patch("bot.kis.open_orders", side_effect=open_rows):
            snap = watch._collect_sellable_snapshot({"US"})
        assert snap == {"total": {"INGR": 11}, "sellable": {"INGR": 6},
                        "open_sell": {"INGR": 3}}

        # 잔고/미체결 중 어느 한 거래소라도 실패하면 부분 결과를 반환하지 않는다.
        with mock.patch("bot.kis.holding_quantities", return_value=None), \
             mock.patch("bot.kis.open_orders"):
            assert watch._collect_sellable_snapshot({"US"}) is None
        quantities2 = [
            {"total": {"INGR": 11}, "sellable": {"INGR": 6}},
            {"total": {}, "sellable": {}},
            {"total": {}, "sellable": {}},
        ]
        with mock.patch("bot.kis.holding_quantities", side_effect=quantities2), \
             mock.patch("bot.kis.open_orders", side_effect=[open_rows[0], None]):
            assert watch._collect_sellable_snapshot({"US"}) is None
    print("[PASS] F4 완전 3거래소·부분체결 잔여3 합산·잔고/nccs 실패=None")


def test_f4_requires_persistent_gap_and_resets_transient_counter():
    with tempfile.TemporaryDirectory() as tmp:
        _ledger, watch = _setup(tmp)
        held = {"INGR": {"q": 11}}
        gap = {"total": {"INGR": 11}, "sellable": {"INGR": 1},
               "open_sell": {"INGR": 0}}
        clear = {"total": {"INGR": 11}, "sellable": {"INGR": 11},
                 "open_sell": {"INGR": 0}}
        with mock.patch("bot.notify.send", return_value=True) as send:
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)       # 1회차 침묵
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=clear)     # 해소 → 카운터 리셋
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)       # 다시 1회차
            importlib.reload(watch)                              # 재시작에도 카운터 영속
            assert watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)       # 같은 갭 2회차
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)       # 중복 경보 0
        assert send.call_count == 1
        state = json.load(open(os.environ["PROTECTION_ALERT_LATCH_PATH"],
                               encoding="utf-8"))
        assert state["sellable_gap"] == ["INGR"]
        assert state["sellable_gap_counts"]["INGR"]["count"] == 3
    print("[PASS] G3 갭 1회 침묵·2회 경보·해소 리셋·재시작 영속")


def test_f4_signature_change_resets_counter_before_accumulating():
    """H2: 해소가 아닌 갭 서명 변경도 새 관찰 1회차로 다시 센다."""
    with tempfile.TemporaryDirectory() as tmp:
        _ledger, watch = _setup(tmp)
        held = {"INGR": {"q": 11}}
        gap_a = {"total": {"INGR": 11}, "sellable": {"INGR": 1},
                 "open_sell": {"INGR": 0}}
        gap_b = {"total": {"INGR": 11}, "sellable": {"INGR": 3},
                 "open_sell": {"INGR": 0}}
        with mock.patch("bot.notify.send", return_value=True) as send:
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap_a)
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap_b)
            state = json.load(open(os.environ["PROTECTION_ALERT_LATCH_PATH"],
                                   encoding="utf-8"))
            changed = state["sellable_gap_counts"]["INGR"]
            assert changed == {"signature": "11:3:0", "count": 1}
            assert send.call_count == 0
            assert watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap_b)
        assert send.call_count == 1
    print("[PASS] H2 갭 서명 변경은 count=1 리셋·같은 서명 2회차만 경보")


def test_f4_counter_preserves_closed_market_and_retries_delivery_or_write_failure():
    with tempfile.TemporaryDirectory() as tmp:
        _ledger, watch = _setup(tmp)
        held = {"INGR": {"q": 11}}
        gap = {"total": {"INGR": 11}, "sellable": {"INGR": 1},
               "open_sell": {"INGR": 0}}
        clear = {"total": {"INGR": 11}, "sellable": {"INGR": 11},
                 "open_sell": {"INGR": 0}}
        assert not watch.audit_sellable_gaps(
            held, scope_markets={"US"}, snapshot=gap)
        # KR만 열린 사이클은 US 카운터를 지우거나 늘리지 않는다.
        counts = watch._advance_gap_counts({}, scope_markets={"KR"})
        assert counts == {"INGR": 1}

        delivery = mock.Mock(side_effect=[False, True])
        with mock.patch("bot.notify.send", delivery):
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)   # 2회차, 전송 실패
            assert watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=gap)   # 다음 감사 재시도 성공
        assert delivery.call_count == 2

        before = open(os.environ["PROTECTION_ALERT_LATCH_PATH"], "rb").read()
        with mock.patch.object(watch, "_write_unlocked", side_effect=OSError("disk")), \
             mock.patch("bot.notify.send") as send:
            assert not watch.audit_sellable_gaps(
                held, scope_markets={"US"}, snapshot=clear)
        assert not send.called
        assert open(os.environ["PROTECTION_ALERT_LATCH_PATH"], "rb").read() == before
    print("[PASS] G3 닫힌시장 카운터 유지·전송실패 재시도·저장실패 byte 불변")


def test_f4_runs_only_in_ops_loop_and_sentinel_keeps_f3():
    from bot import ops_status, protection_observability as watch, sentinel

    class EmptyKisBroker:
        name = "kis"
        def holdings(self):
            return {}

    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sentinel.SENT_PATH = os.path.join(tmp, "sent.json")
        state = {}
        f3 = mock.Mock(return_value=False)
        f4 = mock.Mock(return_value=False)
        blocking_balance = mock.Mock(side_effect=AssertionError("F4 KIS call in sentinel"))
        blocking_orders = mock.Mock(side_effect=AssertionError("F4 KIS call in sentinel"))
        with mock.patch.object(sentinel, "_fetch_positions", return_value=([], 0)), \
             mock.patch.object(sentinel, "_market_open", return_value=True), \
             mock.patch.object(sentinel, "_notify", return_value=True), \
             mock.patch.object(watch, "audit_blocked_protection", f3), \
             mock.patch.object(watch, "audit_sellable_gaps", f4), \
             mock.patch("bot.kis_positions.load", return_value={}), \
             mock.patch("bot.ownership.baseline", return_value=set()), \
             mock.patch("bot.balance_health.record_success"), \
             mock.patch("bot.kis.holding_quantities", blocking_balance), \
             mock.patch("bot.kis.open_orders", blocking_orders):
            sentinel.check_once(EmptyKisBroker(), state)
        assert f3.call_count == 1
        assert f4.call_count == 0
        assert blocking_balance.call_count == 0 and blocking_orders.call_count == 0

        ops_status._last_sellable_gap_audit_at = 0.0
        with mock.patch("bot.settings.market_open", return_value=True), \
             mock.patch("bot.kis_positions.load", return_value={
                 "INGR": {"qty": 11, "ccy": "USD"}}), \
             mock.patch.object(watch, "audit_sellable_gaps", return_value=False) as audit:
            assert not ops_status.maybe_audit_sellable_gaps(now=1000)
            assert not ops_status.maybe_audit_sellable_gaps(now=1001)
        assert audit.call_count == 1
        assert audit.call_args.kwargs["scope_markets"] == {"KR", "US"}
        assert audit.call_args.args[0] == {"INGR": {"q": 11}}
        ops_status._last_sellable_gap_audit_at = 0.0
        with mock.patch("bot.settings.market_open", return_value=True), \
             mock.patch("bot.kis_positions.load", return_value={
                 "INGR": {"qty": 11, "ccy": ""}}), \
             mock.patch.object(watch, "audit_sellable_gaps") as invalid_audit:
            assert not ops_status.maybe_audit_sellable_gaps(now=2000)
        assert not invalid_audit.called
    print("[PASS] G1 sentinel F4/KIS spy 0·F3 1회·ops 주기 F4 1회")


def test_f4_closed_markets_do_not_call_audit():
    """H3: 양 시장 장외에는 F4 함수 자체를 호출하지 않는다."""
    from bot import ops_status, protection_observability as watch

    ops_status._last_sellable_gap_audit_at = 0.0
    with mock.patch("bot.settings.market_open", return_value=False), \
         mock.patch("bot.kis_positions.load", return_value={}) as load, \
         mock.patch.object(watch, "audit_sellable_gaps",
                           return_value=False) as audit:
        assert not ops_status.maybe_audit_sellable_gaps(now=3000)
    assert load.call_count == 0
    assert audit.call_count == 0
    print("[PASS] H3 양 시장 닫힘이면 원장/F4 감사 호출 0회")


def test_read_only_wiring_preserves_protection_skip_and_public_ntfy_contract():
    from bot import kis_telegram, notify, ops_status, protection_observability as watch, sentinel
    src = inspect.getsource(watch)
    for banned in ("place_sell", "place_buy", "raise_level", "lower_level",
                   "ownership.unfreeze"):
        assert banned not in src, banned
    sentinel_src = inspect.getsource(sentinel.check_once)
    assert "protection_observability.check" in sentinel_src
    assert "audit_sellable_gaps" not in sentinel_src
    assert "audit_sellable_gaps" not in inspect.getsource(watch.check)
    assert "audit_sellable_gaps" in inspect.getsource(ops_status.maybe_audit_sellable_gaps)
    assert "maybe_audit_sellable_gaps" in inspect.getsource(kis_telegram.main)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    telegram_unit = open(os.path.join(root, "infra/server/telegram.service"),
                         encoding="utf-8").read()
    beacon = open(os.path.join(root, "infra/server/health_beacon.sh"),
                  encoding="utf-8").read()
    assert "Restart=always" in telegram_unit
    assert "BEACON_UNITS:-sentinel buyloop telegram" in beacon
    assert 'phase="before_protection_audit"' in sentinel_src
    assert ('ledger.open_order_count(code, side="SELL") >= 1' in sentinel_src
            and 'ledger.open_order_count(code, side="CANCEL") >= 1' in sentinel_src)
    # 공개 ntfy는 종목/비율을 버리고 category-only 고정문구만 만든다.
    public = notify._p0_ntfy_body(
        "🚨 INGR 설명되지 않는 매도가능 부족 90.9%", category="trade")
    assert "INGR" not in public and "90.9" not in public
    assert public == "🚨 P0 경보(trade) — 상세는 텔레그램·/진단에서 확인"
    print("[PASS] F3/F4 읽기전용·기존 손절 skip 유지·공개 ntfy category-only")


def main():
    test_f3_blocked_protection_alert_once_persists_and_recovers()
    test_f3_short_inflight_and_untrusted_ledger_are_silent()
    test_alert_delivery_failure_is_not_latched()
    test_f4_gap_formula_latch_failure_and_recovery()
    test_f4_collects_complete_pages_and_partial_sell_remaining()
    test_f4_requires_persistent_gap_and_resets_transient_counter()
    test_f4_signature_change_resets_counter_before_accumulating()
    test_f4_counter_preserves_closed_market_and_retries_delivery_or_write_failure()
    test_f4_runs_only_in_ops_loop_and_sentinel_keeps_f3()
    test_f4_closed_markets_do_not_call_audit()
    test_read_only_wiring_preserves_protection_skip_and_public_ntfy_contract()
    print("\n보호매도 차단/매도가능 고갈 관측 회귀 통과.")


if __name__ == "__main__":
    main()
