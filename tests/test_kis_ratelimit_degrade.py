"""공용 유량 파일을 못 열 때의 강등 검증 — 자가복구 12시간 정지 재발 방지.

실측(2026-08-18): 유량 상태 파일이 `/tmp`에 있고 watchdog만 root로 돌아
`fs.protected_regular`에 걸려 O_CREAT가 EACCES였다. 리미터가 그대로 예외를
던져 readiness → 자가복구가 통째로 죽었다.

  1) 파일이 이미 있으면 O_CREAT 없이 연다(남의 소유여도 열림)
  2) 파일을 아예 못 열면 프로세스 지역 한도로 강등(예외 전파 금지)
  3) 강등해도 한도는 걸린다 — 무제한 fail-open이 아니다
  4) 강등 로그는 프로세스당 1회
  5) 손상된 공용 상태는 기존대로 fail-closed(강등 아님)

실행: python -m tests.test_kis_ratelimit_degrade
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.kis_ratelimit import SecondBucket   # noqa: E402


def test_existing_file_opened_without_o_creat():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rate.json")
        open(path, "w").close()
        flags = []
        real_open = os.open

        def spy(p, fl, *a):
            if p == path:
                flags.append(fl)
            return real_open(p, fl, *a)

        bucket = SecondBucket(2, order_reserve=1, shared_path=path)
        with mock.patch.object(os, "open", spy):
            assert bucket.try_acquire("data") is True
        assert flags and not (flags[0] & os.O_CREAT), flags
    print("[PASS] 기존 파일은 O_CREAT 없이 연다(남의 소유 파일도 열림)")


def _denied(*a, **k):
    raise PermissionError(13, "Permission denied", "/tmp/stock-kis-rate-mock.json")


def test_unopenable_file_degrades_instead_of_raising():
    bucket = SecondBucket(2, order_reserve=1,
                          shared_path="/tmp/stock-kis-rate-mock.json")
    with mock.patch.object(os, "open", _denied):
        assert bucket.try_acquire("data") is True      # 예외 전파 금지
    assert bucket._degraded is True
    print("[PASS] 공용 파일 사용 불가 → 예외 대신 지역 버킷으로 강등")


def test_degraded_bucket_still_enforces_limit():
    bucket = SecondBucket(2, order_reserve=1, shared_path="/nope/rate.json")
    with mock.patch.object(os, "open", _denied):
        bucket.try_acquire("data", now=98.0)      # 강등 유발(창 밖 시각)
    # 강등 후: data 용량 = limit(2) - reserve(1) = 1
    assert bucket.try_acquire("data", now=100.0) is True
    assert bucket.try_acquire("data", now=100.1) is False     # 한도 유지
    assert bucket.try_acquire("order", now=100.2) is True     # 예약 슬롯은 살아있음
    assert bucket.try_acquire("order", now=100.3) is False
    assert bucket.try_acquire("data", now=101.5) is True      # 1초 뒤 창 회전
    print("[PASS] 강등해도 한도·예약 슬롯 유지 — 무제한 fail-open 아님")


def test_degrade_logs_once_per_process():
    bucket = SecondBucket(2, order_reserve=1, shared_path="/nope/rate.json")
    with mock.patch.object(os, "open", _denied), mock.patch("builtins.print") as p:
        for _ in range(5):
            bucket.try_acquire("data")
    assert p.call_count == 1, p.call_args_list
    assert "강등" in str(p.call_args_list[0])
    print("[PASS] 강등 로그는 프로세스당 1회(15초 루프에서 폭주 금지)")


def test_corrupt_shared_state_still_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rate.json")
        with open(path, "w") as fp:
            fp.write("{not json")
        bucket = SecondBucket(2, order_reserve=1, shared_path=path)
        assert bucket.try_acquire("data") is False     # 강등 아님 — 기존 fail-closed
        assert bucket._degraded is False
    print("[PASS] 손상 상태는 기존대로 fail-closed(강등과 구분)")


def main():
    test_existing_file_opened_without_o_creat()
    test_unopenable_file_degrades_instead_of_raising()
    test_degraded_bucket_still_enforces_limit()
    test_degrade_logs_once_per_process()
    test_corrupt_shared_state_still_fails_closed()
    print("\n유량 리미터 강등 검증 통과 — 열기 실패는 강등, 손상은 fail-closed.")


if __name__ == "__main__":
    main()
