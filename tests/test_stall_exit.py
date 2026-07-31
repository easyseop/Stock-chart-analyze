"""전략 A 정체청산 공용상태·KIS·autopaper 경계 회귀 테스트."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from unittest import mock

from bot import kis_exits as X
from bot import kis_positions as P
from bot import ledger as L
from bot import stall_exit as S
from scanner import autopaper as AP
from scanner import backtest as BT


def _state(days=0, counted="2026-07-01"):
    return {
        "half": True, "high": 130.0, "stall_anchor_high": 130.0,
        "high_day": "2026-07-01", "stall_days": days,
        "counted_days": counted,
    }


def test_pure_boundaries_and_no_duplicate_days():
    state = _state(14)
    day15, events = S.advance(
        state, half_confirmed=True, price=130, entry=100, stop0=90,
        day="2026-07-02", valid_trading_day=True, enabled=True)
    assert day15["stall_days"] == 15 and events == ("tighten",)
    same, events = S.advance(
        day15, half_confirmed=True, price=130, entry=100, stop0=90,
        day="2026-07-02", valid_trading_day=True, enabled=True)
    assert same["stall_days"] == 15 and events == ()
    day30, events = S.advance(
        {**day15, "stall_days": 29, "counted_days": "2026-07-02"},
        half_confirmed=True, price=130, entry=100, stop0=90,
        day="2026-07-03", valid_trading_day=True, enabled=True)
    assert day30["stall_days"] == 30 and events == ("exit",)
    assert S.trail_r(day15, run_mode="live") == 1.0
    assert S.trail_r(day15, run_mode="shadow") == 1.5
    assert S.exit_due(day30, run_mode="live")
    assert not S.exit_due(day30, run_mode="shadow")
    print("[PASS] 15/30 경계·동일 날짜 중복 방지·shadow 무집행")


def test_meaningful_high_exact_boundary_resets_without_lowering_stop():
    reset, events = S.advance(
        _state(19), half_confirmed=True, price=132.5,
        entry=100, stop0=90, day="2026-07-04",
        valid_trading_day=True, enabled=True, new_high_r=.25)
    assert events == ("meaningful_high",)
    assert reset["stall_days"] == 0 and reset["stall_anchor_high"] == 132.5
    # 폭은 1.5R로 복원되지만 이미 120까지 올린 손절선을 내리는 액션은 없다.
    acts = X.decide(
        100, 90, 120, 132.5, 5, reset, "2026-06-01", "2026-07-04",
        stall_mode="live")
    assert not any(action[0] == "raise" for action in acts)
    print("[PASS] 정확히 +0.25R 신고가 리셋·1.5R 복원·손절선 불내림")


def test_closed_market_missing_quote_and_ack_do_not_count():
    base = {"half": False, "high": 100.0}
    for valid, price in ((False, 120), (True, 0)):
        out, events = S.advance(
            base, half_confirmed=False, price=price,
            entry=100, stop0=90, day="2026-07-04",
            valid_trading_day=valid, enabled=True)
        assert not out.get("stall_days") and not events
        if not valid:
            assert out["high"] == 100.0
    print("[PASS] 장 닫힘·무효시세·절반 미체결 ACK는 정체 기산 안 함")


class _Broker:
    name = "test"

    def __init__(self, price=130):
        self.price = price
        self.calls = []

    def quote(self, code, ccy):
        return self.price

    def place_sell(self, code, qty, reason, key):
        self.calls.append((code, qty, reason, key))
        return {"state": "rejected", "filled": 0}


def _paths(tmp):
    P.PATH = os.path.join(tmp, "positions.jsonl")
    L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
    X.STATE_PATH = os.path.join(tmp, "exits.json")
    P.record(
        "AAA", stop=90, ccy="USD", entry=100, qty=5,
        opened="2026-06-01", sleeve="A", pos_key="pos:aaa")
    P.raise_stop("AAA", 115)
    return {"AAA": {"code": "AAA", "q": 5, "ccy": "USD", "stop": 115}}


def test_kis_shadow_and_live_use_existing_sell_path():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.settings, "market_open", return_value=True), \
            mock.patch.object(X.notify, "send"):
        held = _paths(tmp)
        X._save({"AAA": _state(14)})
        broker = _Broker()
        with mock.patch.object(X, "STALL_MODE", "shadow"):
            X.manage(broker, held, "2026-07-02")
        assert P.load()["AAA"]["stop"] == 115
        assert broker.calls == []
        assert X._load()["AAA"]["stall_days"] == 15

        X._save({"AAA": _state(29)})
        with mock.patch.object(X, "STALL_MODE", "live"):
            X.manage(broker, held, "2026-07-02")
        assert len(broker.calls) == 1
        assert "정체 청산" in broker.calls[0][2]
        assert broker.calls[0][3].startswith("xe:AAA:stall:2026-06-01#")
    print("[PASS] KIS shadow 주문/추가래칫 0 · live 기존 멱등 매도경로")


def test_closed_market_does_not_prune_other_market_stall_state():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.notify, "send"):
        _paths(tmp)
        X._save({"AAA": _state(12)})
        X.manage(_Broker(), {}, "2026-07-02")
        assert X._load()["AAA"]["stall_days"] == 12
        P.close("AAA")
        X.manage(_Broker(), {}, "2026-07-02")
        assert "AAA" not in X._load()
    print("[PASS] 닫힌 시장 holdings 누락은 정체상태 유지·원장 close만 prune")


def test_corrupt_state_recovers_half_and_alerts_once_without_selling():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.settings, "market_open", return_value=True), \
            mock.patch.object(X.notify, "send") as alert:
        held = _paths(tmp)
        key = "xe:AAA:half:2026-06-01#1"
        L.record_submit(key, "AAA", 5, "익절 +1R 절반",
                        meta={"side": "SELL"})
        L.on_result(key, "filled", 5, open_order=False)
        with open(X.STATE_PATH, "w", encoding="utf-8") as fp:
            fp.write("{broken")
        broker = _Broker(price=120)
        with mock.patch.object(X, "STALL_MODE", "shadow"):
            X.manage(broker, held, "2026-07-02")
            X.manage(broker, held, "2026-07-02")
        assert broker.calls == []
        assert P.load()["AAA"]["stop"] >= 115
        messages = [str(call.args[0]) for call in alert.call_args_list]
        assert sum("정체청산 상태 손상" in text for text in messages) == 1
        assert X._load()["AAA"]["stall_days"] == 0
        assert P.load()["AAA"]["half_done"] is True
        assert stat.S_IMODE(os.stat(X.STATE_PATH).st_mode) == 0o600
    print("[PASS] 상태 손상 시 매도 0·기존 보호 유지·동일손상 경보 1회")


def test_corrupt_state_without_durable_half_proof_stays_quarantined():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.settings, "market_open", return_value=True), \
            mock.patch.object(X.notify, "send"):
        held = _paths(tmp)
        with open(X.STATE_PATH, "w", encoding="utf-8") as fp:
            fp.write("{broken")
        broker = _Broker(price=120)
        with mock.patch.object(X, "STALL_MODE", "live"):
            # 손상을 다른 시장 세션에서 먼저 감지해도 AAA 격리가 보존돼야 한다.
            X.manage(broker, {}, "2026-07-30")
            X.manage(broker, held, "2026-07-30")
            X.manage(broker, held, "2026-07-30")
        state = X._load()["AAA"]
        assert state["state_recovery_quarantine"] is True
        assert not state.get("half") and broker.calls == []
        assert not P.load()["AAA"].get("half_done")
    print("[PASS] 손상+절반 증명 없음은 재익절·타임스탑 모두 fail-closed")


def test_one_share_half_done_is_durable_across_state_corruption():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.settings, "market_open", return_value=True), \
            mock.patch.object(X.notify, "send"):
        _paths(tmp)
        P.close("AAA")
        P.record(
            "AAA", stop=100, ccy="USD", entry=100, qty=1,
            opened="2026-06-01", sleeve="A", pos_key="pos:aaa:one")
        held = {"AAA": {
            "code": "AAA", "q": 1, "ccy": "USD", "stop": 100,
        }}
        with open(X.STATE_PATH, "w", encoding="utf-8") as fp:
            fp.write("{broken")
        broker = _Broker(price=120)
        with mock.patch.object(X, "STALL_MODE", "live"):
            X.manage(broker, held, "2026-07-02")
        assert broker.calls == []
        assert X._load()["AAA"]["half"] is True
        assert P.load()["AAA"]["half_done"] is True
        events = [
            json.loads(line) for line in open(P.PATH, encoding="utf-8")
            if line.strip()
        ]
        assert sum(row.get("ev") == "half_done" for row in events) == 1
    print("[PASS] 1주 half_done은 원장 영속·손상 복구 후 즉시청산 없음")


def test_b_sleeve_is_unchanged():
    acts = X.decide_b(140, 130, 5, "2026-07-01", "2026-07-10")
    assert acts == []
    assert "stall" not in json.dumps(acts)
    print("[PASS] 전략 B는 정체청산 미적용")


def test_autopaper_live_matches_30_day_exit():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(AP, "_update_fx"), \
            mock.patch.object(AP, "_state_branch_snapshot", return_value=None), \
            mock.patch.object(AP, "_trading_lock_status", return_value="off"), \
            mock.patch.object(AP, "_market_open", return_value=True), \
            mock.patch.object(AP, "_earnings_d", return_value=None), \
            mock.patch.object(AP, "_price_age_min", return_value=None), \
            mock.patch.object(AP, "_today", return_value="2026-07-02"), \
            mock.patch.object(AP, "STALL_MODE", "live"):
        AP.STATE_PATH = os.path.join(tmp, "autopaper.json")
        state = {
            "v": AP.VERSION, "cash": AP.START - 500, "start": AP.START,
            "pending": {}, "log": [], "closed": [],
            "pos": {"AAA": {
                "name": "AAA", "ccy": "USD", "q": 5, "q0": 10,
                "avg": 100, "entry": 100, "stop": 115, "stop0": 90,
                "target": 120, "half_done": True, "opened": "2026-06-01",
                "plan": {}, "ctx": {}, "atr0": 2, "hw": 130,
                "risk0": 1000, "realized": 0, "exits": [],
                **_state(29),
            }},
        }
        AP._save(state)
        out = AP.update(
            [{"code": "AAA", "name": "AAA", "ccy": "USD",
              "sr": {"price": 130}}],
            {"now": []}, out_dir=tmp)
        assert out["positions"] == []
        assert "정체 청산" in out["closed"][0]["exits"][-1]["note"]
    print("[PASS] autopaper도 같은 30거래일 잔량청산 적용")


def test_autopaper_legacy_half_without_initial_stop_keeps_three_atr_trail():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(AP, "_update_fx"), \
            mock.patch.object(AP, "_state_branch_snapshot", return_value=None), \
            mock.patch.object(AP, "_trading_lock_status", return_value="off"), \
            mock.patch.object(AP, "_market_open", return_value=True), \
            mock.patch.object(AP, "_earnings_d", return_value=None), \
            mock.patch.object(AP, "_price_age_min", return_value=None), \
            mock.patch.object(AP, "_today", return_value="2026-07-02"), \
            mock.patch.object(AP, "STALL_MODE", "live"):
        AP.STATE_PATH = os.path.join(tmp, "autopaper.json")
        state = {
            "v": AP.VERSION, "cash": AP.START - 500, "start": AP.START,
            "pending": {}, "log": [], "closed": [],
            "pos": {"AAA": {
                "name": "AAA", "ccy": "USD", "q": 5, "q0": 10,
                "avg": 100, "entry": 100, "stop": 115,
                "target": 140, "half_done": True, "opened": "2026-06-01",
                "plan": {}, "ctx": {}, "atr0": 2, "hw": 130,
                "risk0": 1000, "realized": 0, "exits": [],
            }},
        }
        AP._save(state)
        AP.update(
            [{"code": "AAA", "name": "AAA", "ccy": "USD",
              "sr": {"price": 130}}],
            {"now": []}, out_dir=tmp)
        pos = AP._load()["pos"]["AAA"]
        assert pos["stop"] == 124.0 and pos["stop"] <= pos["hw"]
    print("[PASS] stop0 없는 legacy half는 고가 위 점프 없이 기존 3ATR 유지")


def test_autopaper_legacy_without_initial_stop_does_not_count_stall_days():
    # P3 이월: stop0 미증명 legacy는 래칫만 막고 정체일은 가짜 R(risk=entry)로
    # 계속 쌓였다 — shadow 통계 오염. 카운트 자체가 진행되면 안 된다.
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(AP, "_update_fx"), \
            mock.patch.object(AP, "_state_branch_snapshot", return_value=None), \
            mock.patch.object(AP, "_trading_lock_status", return_value="off"), \
            mock.patch.object(AP, "_market_open", return_value=True), \
            mock.patch.object(AP, "_earnings_d", return_value=None), \
            mock.patch.object(AP, "_price_age_min", return_value=None), \
            mock.patch.object(AP, "STALL_MODE", "shadow"):
        AP.STATE_PATH = os.path.join(tmp, "autopaper.json")
        state = {
            "v": AP.VERSION, "cash": AP.START - 500, "start": AP.START,
            "pending": {}, "log": [], "closed": [],
            "pos": {"AAA": {
                "name": "AAA", "ccy": "USD", "q": 5, "q0": 10,
                "avg": 100, "entry": 100, "stop": 115,
                "target": 140, "half_done": True, "opened": "2026-06-01",
                "plan": {}, "ctx": {}, "atr0": 2, "hw": 130,
                "risk0": 1000, "realized": 0, "exits": [],
            }},
        }
        AP._save(state)
        rows = [{"code": "AAA", "name": "AAA", "ccy": "USD",
                 "sr": {"price": 130}}]
        for day in ("2026-07-02", "2026-07-03"):
            with mock.patch.object(AP, "_today", return_value=day):
                AP.update(rows, {"now": []}, out_dir=tmp)
        pos = AP._load()["pos"]["AAA"]
        assert "stall_anchor_high" not in pos
        assert not pos.get("stall_days")
        assert pos["hw"] >= 130               # 기존 고가 추적은 계속 유지
    print("[PASS] stop0 없는 legacy는 정체일 카운트 자체를 시작하지 않음")


def test_durable_half_done_reapplies_lost_breakeven_ratchet_once():
    # P3 이월: mark_half_done 영속 직후·raise_stop 전 크래시 → 재시작 시
    # half_stop_raised=True로만 복구돼 본전 래칫이 영구 유실되던 창.
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(X.settings, "market_open", return_value=True), \
            mock.patch.object(X.notify, "send") as alert:
        P.PATH = os.path.join(tmp, "positions.jsonl")
        L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
        X.STATE_PATH = os.path.join(tmp, "exits.json")
        P.record(
            "AAA", stop=90, ccy="USD", entry=100, qty=5,
            opened="2026-06-01", sleeve="A", pos_key="pos:aaa")
        P.mark_half_done("AAA", event_id="half:pos:aaa")   # 크래시 직전까지 영속됨
        assert P.load()["AAA"]["stop"] == 90               # 래칫은 유실된 상태
        held = {"AAA": {"code": "AAA", "q": 5, "ccy": "USD", "stop": 90}}
        broker = _Broker(price=101)
        with mock.patch.object(X, "STALL_MODE", "shadow"):
            X.manage(broker, held, "2026-07-02")
            X.manage(broker, held, "2026-07-02")
        assert P.load()["AAA"]["stop"] == 100              # 본전 복구
        raises = [
            json.loads(line) for line in open(P.PATH, encoding="utf-8")
            if line.strip() and json.loads(line).get("ev") == "raise"
        ]
        assert len(raises) == 1 and raises[0]["stop"] == 100   # 멱등(중복 0)
        assert broker.calls == []
        assert any("본전 복구" in str(call.args[0])
                   for call in alert.call_args_list)
    print("[PASS] durable half의 유실된 본전 래칫을 1회만 멱등 재적용")


def test_valid_json_wrong_schema_state_is_quarantined_like_corruption():
    # P3 이월: {"AAA": "…"}처럼 유효한 JSON dict지만 스키마가 틀린 손상이
    # _load_with_status를 그대로 통과해 격리·경보 경로를 우회했다.
    for broken in (
            {"AAA": "garbage"},
            {"AAA": {"half": "yes", "stall_days": "banana"}},
            {"AAA": {"high": True}},
            {"AAA": {"half_keys": [1, 2]}},
    ):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(X.settings, "market_open",
                                  return_value=True), \
                mock.patch.object(X.notify, "send") as alert:
            held = _paths(tmp)
            with open(X.STATE_PATH, "w", encoding="utf-8") as fp:
                json.dump(broken, fp)
            broker = _Broker(price=120)
            with mock.patch.object(X, "STALL_MODE", "live"):
                X.manage(broker, held, "2026-07-30")
                X.manage(broker, held, "2026-07-30")
            state = X._load()["AAA"]
            assert state["state_recovery_quarantine"] is True, broken
            assert broker.calls == [], broken
            messages = [str(call.args[0]) for call in alert.call_args_list]
            assert sum("정체청산 상태 손상" in text
                       for text in messages) == 1, broken
    print("[PASS] 스키마 손상 dict도 격리+경보 1회+매도 0(우회 봉합)")


def test_backtest_three_parameter_arms_report_required_metrics():
    import pandas as pd
    index = pd.date_range("2026-01-01", periods=50, freq="B")
    frame = pd.DataFrame({
        "Open": [100.0] * 50,
        "High": [110.0] + [111.0] * 49,
        "Low": [102.0] * 50,
        "Close": [105.0] * 50,
        "Volume": [1_000] * 50,
    }, index=index)
    events = []
    event = {
        "code": "AAA", "entry_date": str(index[0].date()),
        "seed_fraction": .2,
    }
    for tighten, exit_after in BT.STALL_VARIANTS:
        r, xj, reason, sessions = BT._stall_ladder(
            frame, 0, 100, 90, tighten_days=tighten,
            exit_days=exit_after, max_hold=50)
        event[f"{tighten}_{exit_after}"] = {
            "r": r, "reason": reason, "hold_sessions": sessions,
            "exit_date": str(index[xj].date()),
        }
        assert reason == "정체청산"
    events.append(event)
    report = BT.summarize_stall_variants(events)
    assert set(report["variants"]) == {"10_20", "15_30", "20_40"}
    assert report["variants"]["10_20"]["average_hold_sessions"] \
        < report["variants"]["20_40"]["average_hold_sessions"]
    assert report["variants"]["15_30"]["max_seed_occupancy_pct"] == 20.0
    assert all("total_r" in row for row in report["variants"].values())
    print("[PASS] 백테스트 3조합 총R·평균보유일·최대시드점유 표 생성")


def main():
    test_pure_boundaries_and_no_duplicate_days()
    test_meaningful_high_exact_boundary_resets_without_lowering_stop()
    test_closed_market_missing_quote_and_ack_do_not_count()
    test_kis_shadow_and_live_use_existing_sell_path()
    test_closed_market_does_not_prune_other_market_stall_state()
    test_corrupt_state_recovers_half_and_alerts_once_without_selling()
    test_corrupt_state_without_durable_half_proof_stays_quarantined()
    test_one_share_half_done_is_durable_across_state_corruption()
    test_b_sleeve_is_unchanged()
    test_autopaper_live_matches_30_day_exit()
    test_autopaper_legacy_half_without_initial_stop_keeps_three_atr_trail()
    test_autopaper_legacy_without_initial_stop_does_not_count_stall_days()
    test_durable_half_done_reapplies_lost_breakeven_ratchet_once()
    test_valid_json_wrong_schema_state_is_quarantined_like_corruption()
    test_backtest_three_parameter_arms_report_required_metrics()
    print("\n정체청산 공용 규칙 검증 통과.")


if __name__ == "__main__":
    main()
