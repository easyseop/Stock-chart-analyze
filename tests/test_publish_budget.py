"""발행 예산 검증 — ntfy 무료 한도(일 250건) 안에서 진단 창구를 살린다.

실측(2026-08-21): 전 발행처 합계가 하루 800건을 넘어 오후부터 429로 조용히
실패했고, SSH 없는 원격 진단의 유일한 창구인 ops 스냅샷이 죽었다.
소비자(사이트 빌드)는 15분마다 ntfy 최신 1건만 읽으므로 그보다 잦은 발행은
전부 버려지는 낭비였다.

  1) ops: 상태가 바뀌면 최소 간격 후 즉시 발행(사고 때 더 빠름)
  2) ops: 무변화면 IDLE 간격까지 억제 — 시각·나이 변화는 '변화'가 아님
  3) ops: 최소 간격 안에서는 상태가 바뀌어도 발행하지 않는다(폭주 방지)
  4) ops: 스냅샷 실패는 기존 주기 발행으로 강등(진단 창구를 잃지 않음)
  5) alpha: 소비 주기(15분) 미만 발행 억제 · 마감/리베이스는 force로 통과
  6) trade_stats: 기본 간격이 소비 주기보다 길다
  7) 예산 총합이 무료 한도 안에 든다(설정값 산술)

실행: python -m tests.test_publish_budget
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha, ops_status, trade_stats   # noqa: E402


def _snap(level=0, action="idle", now="10:00"):
    return {"generated_at": f"2026-08-21T{now}:00", "v": 1,
            "kill_level": level,
            "self_heal": {"action": action, "observed_s": 12.0}}


def _seed(elapsed: float) -> None:
    """`elapsed`초 전에 마지막으로 발행한 상태로 모듈 전역을 맞춘다.

    0.0을 쓰면 경과 시간이 epoch 이후 전체가 되어 IDLE 분기까지 함께 열린다 —
    '최소 간격만 지난' 상황을 만들려면 반드시 현재 시각에서 빼야 한다."""
    ops_status._last_publish = time.time() - elapsed


def _min_gap() -> float:
    return max(60, ops_status.PUBLISH_INTERVAL_S) + 1


def test_ops_publishes_immediately_on_state_change():
    _seed(_min_gap())
    ops_status._last_digest = ""
    sent = []
    with mock.patch.object(ops_status, "snapshot", lambda: _snap()), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: sent.append(snap) or True):
        assert ops_status.maybe_publish() is True          # 최초
    # 최소 간격만 지난 시점 + 상태 변화(kill 0→1) → IDLE을 기다리지 않고 발행
    _seed(_min_gap())
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(level=1)), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: sent.append(snap) or True):
        assert ops_status.maybe_publish() is True
    assert len(sent) == 2
    assert sent[1]["kill_level"] == 1
    print("[PASS] ops: 상태 변화 → 즉시 발행(사고 시 더 빠름)")


def test_ops_suppresses_when_only_time_changed():
    _seed(_min_gap())
    ops_status._last_digest = ""
    calls = []
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(now="10:00")), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is True
    _seed(_min_gap())                                       # 최소 간격만 경과
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(now="10:10")), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is False          # 시각만 달라짐
    assert len(calls) == 1
    # IDLE 간격이 지나면 무변화여도 1회 발행(생존 신호)
    _seed(ops_status.IDLE_INTERVAL_S + 1)
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(now="10:40")), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is True
    assert len(calls) == 2
    print("[PASS] ops: 시각만 변하면 억제 · IDLE 경과 시 생존 발행")


def test_ops_holds_minimum_interval_even_on_state_change():
    """변화 감지가 최소 간격을 무력화하면 안 된다 — 진동 상태에서 폭주한다."""
    _seed(_min_gap())
    ops_status._last_digest = ""
    calls = []
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(level=0)), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is True
    _seed(5.0)                                              # 최소 간격 이내
    with mock.patch.object(ops_status, "snapshot", lambda: _snap(level=4)), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is False
    assert len(calls) == 1
    print("[PASS] ops: 최소 간격 안에서는 상태가 바뀌어도 억제")


def test_ops_snapshot_failure_falls_back_to_periodic():
    _seed(_min_gap())
    ops_status._last_digest = ""

    def boom():
        raise RuntimeError("snapshot down")

    calls = []
    with mock.patch.object(ops_status, "snapshot", boom), \
            mock.patch.object(ops_status, "publish",
                              lambda snap=None: calls.append(1) or True):
        assert ops_status.maybe_publish() is True
    assert len(calls) == 1
    print("[PASS] ops: 스냅샷 실패도 발행 시도(진단 창구 유지)")


def test_alpha_dash_throttled_but_force_passes():
    posts = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        posts.append(req)
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake_open):
        alpha._last_dash_publish = 0.0
        alpha.publish_dash({"days": []})                    # 첫 발행
        assert len(posts) == 1, posts
        alpha.publish_dash({"days": []})                    # 즉시 재호출 → 억제
        assert len(posts) == 1, posts
        alpha.publish_dash({"days": []}, force=True)        # 마감/리베이스
        assert len(posts) == 2, posts
    print("[PASS] alpha: 15분 미만 억제 · force는 통과")


def test_trade_stats_interval_exceeds_consumer_cadence():
    assert trade_stats.PUBLISH_INTERVAL_S >= 1800
    print(f"[PASS] trade_stats 기본 간격 {trade_stats.PUBLISH_INTERVAL_S}s")


FREE_QUOTA_PER_DAY = 250
# ntfy 무료 한도는 **토픽이 아니라 발신 IP** 기준이라 P0 경보도 같은 지갑을
# 쓴다. 정기 발행이 한도를 꽉 채우면 정작 손절 경보가 429로 떨어진다 —
# 경보·수동 /진단 몫으로 최소 이만큼은 남겨 둔다.
P0_RESERVE_PER_DAY = 50


def test_daily_budget_fits_free_quota():
    """설정값만으로 하루 발행 상한을 계산해 무료 한도 안인지 본다."""
    per_day = {
        "health_beacon": 24 * 60 // 60,                      # 60분 타이머
        "alpha": 24 * 60 * 60 // alpha.PUBLISH_MIN_INTERVAL_S,
        "ops_idle": 24 * 60 * 60 // ops_status.IDLE_INTERVAL_S,
        "trade_stats": 24 * 60 * 60 // trade_stats.PUBLISH_INTERVAL_S,
    }
    total = sum(per_day.values())
    budget = FREE_QUOTA_PER_DAY - P0_RESERVE_PER_DAY
    assert total <= budget, (per_day, total, budget)
    print(f"[PASS] 정기 발행 상한 {total}건 ≤ {budget}건"
          f"(한도 {FREE_QUOTA_PER_DAY} − P0 예비 {P0_RESERVE_PER_DAY}) · {per_day}")


def main():
    test_ops_publishes_immediately_on_state_change()
    test_ops_suppresses_when_only_time_changed()
    test_ops_holds_minimum_interval_even_on_state_change()
    test_ops_snapshot_failure_falls_back_to_periodic()
    test_alpha_dash_throttled_but_force_passes()
    test_trade_stats_interval_exceeds_consumer_cadence()
    test_daily_budget_fits_free_quota()
    print("\n발행 예산 검증 통과 — 무료 한도 안 · 사고 시 즉시 발행.")


if __name__ == "__main__":
    main()
