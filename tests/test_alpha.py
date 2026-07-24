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
    assert "positions" not in payload and "amount" not in payload
    print("[PASS] 개인 웹 성과 스냅샷 — A/B·4대 지수·퍼센트 전용")


def main():
    test_aggregate()
    test_session_and_flow_neutral()
    test_capture_stats()
    test_state_roundtrip()
    test_dashboard_snapshot_is_percentage_only()
    print("\n알파 추적 검증 통과 — 집계·세션기준·캡처통계.")


if __name__ == "__main__":
    main()
