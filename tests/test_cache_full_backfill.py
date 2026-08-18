"""즉석수집 전체 백필 검증 — 짧은 캐시가 영영 짧게 남지 않도록.

실측: `/수집 <티커>`가 한 달치만 보였다. `cache.update()`가 캐시가 있으면
마지막 날짜 이후만 받기 때문에, 처음에 짧게 담긴 캐시는 계속 짧다.
B(매물대)는 2년 이력을 요구하므로 짧은 캐시는 신호 자체가 안 난다.

  1) full=True면 캐시가 있어도 FETCH_START부터 다시 받아 **병합**
  2) 병합이라 소스가 더 이상 주지 않는 과거 구간도 보존
  3) 수집 실패 시 기존 캐시 유지(fail-safe)
  4) 기본(full=False)은 기존 증분 동작 그대로

실행: python -m tests.test_cache_full_backfill
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import cache          # noqa: E402


def _df(start, periods, price=10.0):
    idx = pd.date_range(start, periods=periods, freq="B")
    return pd.DataFrame({"Open": price, "High": price, "Low": price,
                         "Close": price, "Volume": 1.0}, index=idx)


def test_full_refetches_and_merges():
    short = _df("2026-07-01", 20)                    # 한 달치만 있는 캐시
    long = _df("2019-01-01", 1800, price=11.0)       # 소스가 주는 전체 이력
    saved = {}
    with mock.patch.object(cache, "load", lambda c: short), \
            mock.patch.object(cache.datamod, "fetch_daily",
                              lambda c, **k: long), \
            mock.patch.object(cache, "save", lambda c, d: saved.update(d=d)):
        out = cache.update("TEST", full=True)
    assert len(out) > len(short), (len(out), len(short))
    assert out.index[0] == long.index[0]             # 과거까지 확장
    assert saved["d"] is not None
    print(f"[PASS] full=True → {len(short)}행 → {len(out)}행 재백필")


def test_full_preserves_rows_source_no_longer_returns():
    old_only = _df("2015-01-01", 30)                 # 소스가 안 주는 옛 구간
    recent = _df("2026-01-01", 100, price=12.0)
    with mock.patch.object(cache, "load", lambda c: old_only), \
            mock.patch.object(cache.datamod, "fetch_daily", lambda c, **k: recent), \
            mock.patch.object(cache, "save", lambda c, d: None):
        out = cache.update("TEST", full=True)
    assert out.index[0] == old_only.index[0], out.index[0]
    assert len(out) == len(old_only) + len(recent)
    print("[PASS] 병합 — 소스가 더는 안 주는 과거 구간 보존")


def test_full_keeps_cache_on_fetch_failure():
    short = _df("2026-07-01", 20)
    def boom(*a, **k):
        raise RuntimeError("network")
    with mock.patch.object(cache, "load", lambda c: short), \
            mock.patch.object(cache.datamod, "fetch_daily", boom), \
            mock.patch.object(cache, "save", lambda c, d: None):
        out = cache.update("TEST", full=True)
    assert out.equals(short)
    print("[PASS] 수집 실패 → 기존 캐시 유지(fail-safe)")


def test_default_is_incremental():
    short = _df("2026-07-01", 20)
    calls = {}
    def fetch(code, start=None):
        calls["start"] = start
        return _df("2026-07-25", 5)
    with mock.patch.object(cache, "load", lambda c: short), \
            mock.patch.object(cache.datamod, "fetch_daily", fetch), \
            mock.patch.object(cache, "save", lambda c, d: None):
        cache.update("TEST")
    assert calls["start"] is not None, "기본 경로가 전체 백필로 바뀌면 안 됨"
    print("[PASS] 기본은 기존 증분 동작 유지")


def main():
    test_full_refetches_and_merges()
    test_full_preserves_rows_source_no_longer_returns()
    test_full_keeps_cache_on_fetch_failure()
    test_default_is_incremental()
    print("\n즉석수집 전체 백필 검증 통과 — 병합·보존·실패무해·기본 불변.")


if __name__ == "__main__":
    main()
