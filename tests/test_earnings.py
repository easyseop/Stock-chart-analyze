"""어닝 캐시 갱신 시간 예산 검증 — 2026-08-07 피드 109분 정체 재발 방지.

  1) 예산 초과 시 루프 중단 + 그때까지 진행분 저장(다음 실행이 이어받음)
  2) 주기 저장: 강제종료돼도 _SAVE_EVERY 단위 진행분이 디스크에 남음
  3) TTL 신선한 종목은 네트워크 0으로 건너뜀(예산 소모 없음)
  4) 예산 안이면 전부 갱신(기존 동작 불변)

실행: python -m tests.test_earnings
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import earnings as E


def _sandbox():
    """캐시 파일을 임시 경로로 격리."""
    tmp = tempfile.mkdtemp()
    E.CACHE_PATH = os.path.join(tmp, "earnings_cache.json")
    return E.CACHE_PATH


def test_budget_stops_and_saves():
    path = _sandbox()
    calls = []

    def slow_fetch(code):
        calls.append(code)
        E.time.sleep(0.05)                 # 종목당 50ms — 예산 0.12s면 2~3개
        return "2026-09-01"

    orig = E._fetch_one
    E._fetch_one = slow_fetch
    try:
        n = E.refresh([f"T{i}" for i in range(50)], budget_s=0.12)
    finally:
        E._fetch_one = orig
    assert len(calls) < 50, f"예산 초과에도 전량 조회: {len(calls)}"
    saved = json.load(open(path, encoding="utf-8"))
    assert len(saved) == len(calls) and n == len(calls)   # 진행분 저장·집계 일치
    print(f"[PASS] 예산 소진 → {len(calls)}종목만 조회 후 중단·저장")


def test_periodic_save_survives_kill():
    path = _sandbox()
    boom = E._SAVE_EVERY + 3               # 주기 저장 직후 강제 예외(=SIGKILL 근사)

    def dying_fetch(code):
        idx = int(code[1:])
        if idx == boom:
            raise KeyboardInterrupt("hang killer")
        return None

    orig = E._fetch_one
    E._fetch_one = dying_fetch
    try:
        try:
            E.refresh([f"T{i}" for i in range(boom + 10)], budget_s=999)
        except KeyboardInterrupt:
            pass
    finally:
        E._fetch_one = orig
    saved = json.load(open(path, encoding="utf-8"))
    assert len(saved) >= E._SAVE_EVERY, f"주기 저장 없음: {len(saved)}"
    print(f"[PASS] 도중 강제종료에도 {len(saved)}종목 진행분 보존")


def test_fresh_ttl_skipped_without_budget():
    path = _sandbox()
    import pandas as pd
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"AAA": {"date": "2026-09-01", "fetched": now}}, fp)

    def must_not_call(code):
        raise AssertionError(f"신선한 캐시인데 네트워크 조회: {code}")

    orig = E._fetch_one
    E._fetch_one = must_not_call
    try:
        n = E.refresh(["AAA", "005930"], budget_s=999)   # 한국코드도 생략 경로
    finally:
        E._fetch_one = orig
    assert n == 0
    print("[PASS] TTL 신선·한국코드 → 네트워크 0")


def test_within_budget_updates_all():
    path = _sandbox()
    orig = E._fetch_one
    E._fetch_one = lambda code: "2026-09-01"
    try:
        n = E.refresh([f"T{i}" for i in range(30)], budget_s=999)
    finally:
        E._fetch_one = orig
    assert n == 30
    saved = json.load(open(path, encoding="utf-8"))
    assert len(saved) == 30
    print("[PASS] 예산 안이면 전량 갱신(기존 동작 불변)")


def main():
    test_budget_stops_and_saves()
    test_periodic_save_survives_kill()
    test_fresh_ttl_skipped_without_budget()
    test_within_budget_updates_all()
    print("\n어닝 갱신 시간 예산 검증 통과 — 행/저속 크롤링이 빌드를 못 잡아먹음.")


if __name__ == "__main__":
    main()
