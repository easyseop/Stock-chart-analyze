"""성과 vs 지수(알파) 추적 검증 — 네트워크·KIS 없이 순수 계산만.

  1) 집계: 시장×슬리브 분리 + 사용자 기보유(baseline) 제외
  2) 세션 기준점: 첫 틱=기준, 이후 계좌%/지수%가 세션시작 대비로 계산
  3) 장중 신규 매수(플로우)가 계좌%를 왜곡하지 않음
  4) 캡처 통계: 상승/하락 캡처·지수 이긴 날, 표본<5면 미표시

실행: python -m tests.test_alpha
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha


def _row(code, mkt, cost, pl, ccy=None):
    return {"code": code, "market": mkt, "ccy": ccy or ("KRW" if mkt == "KR" else "USD"),
            "buy_amt": cost, "pl_amt": pl}


def test_aggregate():
    rows = [_row("AAA", "US", 1000, 10), _row("BBB", "US", 500, -5),
            _row("CCC", "KR", 2000, 20), _row("BASE", "KR", 999, 99)]
    agg = alpha.aggregate(rows, b_codes={"BBB"}, baseline={"BASE"})
    assert agg["US"]["A"] == {"cost": 1000, "pl": 10}
    assert agg["US"]["B"] == {"cost": 500, "pl": -5}
    assert agg["KR"]["A"] == {"cost": 2000, "pl": 20}
    assert agg["KR"]["B"]["cost"] == 0          # baseline은 어디에도 안 들어감
    print("[PASS] 집계: 시장×슬리브 분리 + 기보유 제외")


def test_session_and_flow_neutral():
    st = {}
    agg1 = {"US": {"A": {"cost": 1000.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}},
            "KR": {"A": {"cost": 0.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}}}
    r1 = alpha.session_update(
        st, "US", agg1, {"나스닥": 20000.0, "S&P500": 6000.0},
        "22:30", "2026-07-24")
    assert r1["acct"] == 0.0 and r1["idx"]["나스닥"] == 0.0     # 기준점
    # 지수 +1%, 계좌 pl +20 (2%)
    agg2 = {"US": {"A": {"cost": 1000.0, "pl": 20.0}, "B": {"cost": 0.0, "pl": 0.0}},
            "KR": agg1["KR"]}
    r2 = alpha.session_update(
        st, "US", agg2, {"나스닥": 20200.0, "S&P500": 6030.0},
        "23:30", "2026-07-24")
    assert abs(r2["acct"] - 2.0) < 1e-9 and abs(r2["idx"]["나스닥"] - 1.0) < 1e-9
    assert abs(r2["idx"]["S&P500"] - .5) < 1e-9
    # 장중 신규 매수(B에 cost 500, pl 0 추가) → 계좌%가 크게 안 튐(분모만 증가)
    agg3 = {"US": {"A": {"cost": 1000.0, "pl": 20.0}, "B": {"cost": 500.0, "pl": 0.0}},
            "KR": agg1["KR"]}
    r3 = alpha.session_update(
        st, "US", agg3, {"나스닥": 20200.0, "S&P500": 6030.0},
        "23:35", "2026-07-24")
    assert abs(r3["acct"] - (20.0 / 1500.0 * 100)) < 1e-9      # 왜곡 없음(희석만)
    assert len(st["day"]["US"]["series"]) == 3
    rich = st["day"]["US"]["series_v2"][-1]
    assert rich["A"] == 2.0 and rich["B"] == 0.0
    assert set(rich["indices"]) == {"나스닥", "S&P500"}
    print("[PASS] 세션 기준점 + A/B·복수지수 + 플로우 중립")


def test_nav_twr_uses_previous_close_and_removes_trade_flows():
    st = {"carry": {"US": {"date": "2026-07-23", "nav_last": {
        "account": {"value": 1000.0, "flow": 1000.0},
        "A": {"value": 1000.0, "flow": 1000.0},
        "B": {"value": 0.0, "flow": 0.0},
    }}}}
    agg = {"US": {"A": {"cost": 0.0, "pl": 0.0},
                  "B": {"cost": 0.0, "pl": 0.0}},
           "KR": {"A": {"cost": 0.0, "pl": 0.0},
                  "B": {"cost": 0.0, "pl": 0.0}}}
    nav1 = {
        "account": {"value": 1120.0, "flow": 1100.0},
        "A": {"value": 1120.0, "flow": 1100.0},
        "B": {"value": 0.0, "flow": 0.0},
    }
    first = alpha.session_update(
        st, "US", agg, {"나스닥": 20200.0}, "22:35", "2026-07-24",
        nav=nav1, idx_previous_close={"나스닥": 20000.0})
    assert abs(first["acct"] - 2.0) < 1e-9
    assert abs(first["idx"]["나스닥"] - 1.0) < 1e-9
    assert st["day"]["US"]["basis"] == "previous_close"
    # 500원어치를 같은 가격에 매도: 보유평가 -500, 순유입 flow -500 → 수익률 불변.
    nav2 = {
        "account": {"value": 620.0, "flow": 600.0},
        "A": {"value": 620.0, "flow": 600.0},
        "B": {"value": 0.0, "flow": 0.0},
    }
    second = alpha.session_update(
        st, "US", agg, {"나스닥": 20200.0}, "22:40", "2026-07-24",
        nav=nav2, idx_previous_close={"나스닥": 20000.0})
    assert abs(second["acct"] - 2.0) < 1e-9
    print("[PASS] NAV/TWR: 전일종가 기준 + 매수·매도 현금흐름 제거")


def test_holdings_equal_weight_uses_starting_positions_only():
    import pandas as pd
    rows = [
        {**_row("AAA", "US", 100, 10), "cur": 110},
        {**_row("BBB", "US", 100, -10), "cur": 90},
        {**_row("NEW", "US", 100, 20), "cur": 120},
        {**_row("MANUAL_NEW", "US", 100, 30), "cur": 130},
    ]
    frames = {
        code: pd.DataFrame(
            {"Close": [100.0]},
            index=pd.to_datetime(["2026-07-23"]))
        for code in ("AAA", "BBB", "NEW", "MANUAL_NEW")
    }
    recs = {
        "AAA": {"opened": "2026-07-22", "sleeve": "A"},
        "BBB": {"opened": "2026-07-22", "sleeve": "B"},
        "NEW": {"opened": "2026-07-24", "sleeve": "A"},
    }
    with mock.patch("scanner.cache.load", side_effect=lambda code: frames.get(code)):
        out = alpha.holdings_equal_weight(
            rows, "US", recs, set(), "2026-07-24",
            # 첫 틱 스냅샷에 수동매수가 섞여도 opened 추적일이 없으면 제외.
            start_codes={"AAA", "BBB", "MANUAL_NEW"})
    assert abs(out["account"]) < 1e-9              # +10%와 -10% 동일가중=0%
    assert abs(out["A"] - 10.0) < 1e-9 and abs(out["B"] + 10.0) < 1e-9
    assert out["covered"] == 2 and out["eligible"] == 2
    print("[PASS] 장시작 보유만 전일종가 대비 동일가중(봇·수동 신규 제외)")


def test_capture_stats():
    days = [{"d": f"d{i}", "mkt": "US", "acct": a, "idx": ix}
            for i, (a, ix) in enumerate(
                [(0.5, 1.0), (1.2, 1.0), (-0.3, -1.0), (-0.5, -1.0), (0.2, 0.5)])]
    s = alpha.capture_stats(days, "US")
    assert "상승일 캡처" in s and "하락일 캡처" in s and "지수 이긴 날" in s
    assert alpha.capture_stats(days[:3], "US") == ""            # 표본<5 → 미표시
    print("[PASS] 캡처 통계(상승/하락/승률) + 표본 최소 5일")


def test_state_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        alpha.STATE_PATH = os.path.join(tmp, "alpha_state.json")
        alpha._save({"x": 1})
        loaded = alpha._load()
        assert loaded["x"] == 1 and loaded.get("updated_at")
    print("[PASS] 상태 저장/복원")


def test_accounting_migration_rebase_is_atomic_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        alpha.STATE_PATH = os.path.join(tmp, "alpha_state.json")
        alpha._save({
            "day": {"US": {"date": "2026-07-28", "series": [
                ["23:30", -16.4, -.2]], "series_v2": [{
                    "t": "23:30", "account": -16.4,
                    "indices": {"나스닥": -.2},
                }]}},
            "days": [{"d": "2026-07-28", "mkt": "US",
                      "acct": -16.4, "idx": -.2}],
            "carry": {"US": {"nav_last": {"account": {
                "value": 100, "flow": 200}}}},
        })
        with mock.patch.object(alpha, "publish_dash") as publish:
            result = alpha.rebase_after_accounting_migration(
                "plan-sha", started_at=1785250800, archived=True)
        publish.assert_called_once()
        assert result["rebased"] is True
        state = alpha._load()
        assert state["day"] == {} and state["days"] == [] and state["carry"] == {}
        assert state["performance_epoch"]["id"] == "plan-sha"
        assert state["performance_epoch"]["archived_previous_state"] is True

        # 같은 이관 plan 재실행은 새로 쌓인 성과를 다시 지우지 않는다.
        state["days"].append({"d": "new-valid-day"})
        alpha._save(state)
        with mock.patch.object(alpha, "publish_dash") as republish:
            again = alpha.rebase_after_accounting_migration(
                "plan-sha", started_at=1785250900, archived=True)
        republish.assert_not_called()
        assert again["already_applied"] is True
        assert alpha._load()["days"] == [{"d": "new-valid-day"}]

        # 첫 새 표본은 계좌와 지수를 같은 시각의 0%로 시작한다.
        fresh = alpha._load()
        agg = {
            "US": {"A": {"cost": 100.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}},
            "KR": {"A": {"cost": 0.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}},
        }
        out = alpha.session_update(
            fresh, "US", agg, {"나스닥": 20000.0},
            "22:30", "2026-07-29",
            nav={
                "account": {"value": 100.0, "flow": 100.0},
                "A": {"value": 100.0, "flow": 100.0},
                "B": {"value": 0.0, "flow": 0.0},
            },
            idx_previous_close={"나스닥": 19900.0})
        assert out["acct"] == 0.0 and out["idx"]["나스닥"] == 0.0
        assert fresh["day"]["US"]["basis"] == "first_sample"
        first_point = fresh["day"]["US"]["series_v2"][0]
        assert first_point["daily_indices"]["나스닥"] == 0.0
    print("[PASS] 레거시 이관 후 계좌·지수 동시 0% 리베이스 + 재실행 멱등")


def test_dashboard_snapshot_is_percentage_only():
    st = {
        "updated_at": "2026-07-24T00:00:00+00:00",
        "day": {"US": {
            "date": "2026-07-24",
            "series_v2": [{
                "t": "23:00", "account": 1.2, "A": 1.5, "B": -.2,
                "indices": {"나스닥": .8, "S&P500": .6},
            }],
        }},
        "days": [{
            "d": "2026-07-23", "mkt": "KR", "acct": .5, "idx": .2,
            "a": .4, "b": .7, "indices": {"코스피": .2, "코스닥": .3},
        }],
    }
    payload = alpha.dashboard_snapshot(st)
    assert payload["markets"]["US"]["series"][0]["A"] == 1.5
    assert payload["markets"]["US"]["indices"] == ["나스닥", "S&P500"]
    assert payload["markets"]["KR"]["indices"] == ["코스피", "코스닥"]
    assert payload["days"][0]["indices"]["코스닥"] == .3
    assert payload["version"] == alpha.SNAPSHOT_VERSION
    assert "positions" not in payload and "amount" not in payload
    print("[PASS] 개인 웹 성과 스냅샷 — A/B·4대 지수·퍼센트 전용")


def main():
    test_aggregate()
    test_session_and_flow_neutral()
    test_nav_twr_uses_previous_close_and_removes_trade_flows()
    test_holdings_equal_weight_uses_starting_positions_only()
    test_capture_stats()
    test_state_roundtrip()
    test_accounting_migration_rebase_is_atomic_and_idempotent()
    test_dashboard_snapshot_is_percentage_only()
    print("\n알파 추적 검증 통과 — 집계·세션기준·캡처통계.")


if __name__ == "__main__":
    main()
