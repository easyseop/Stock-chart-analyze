"""KIS 초당 리미터 검증 — 모의 2/s·order 예약슬롯·윈도우 슬라이드.

  1) 모의(2/s, reserve1): data는 초당 1건만, 2번째 data는 거부
  2) data가 꽉 차도 order는 예약 슬롯으로 즉시 통과
  3) 1초 경과 후 윈도우 슬라이드 → 다시 허용
  4) acquire(차단)가 슬롯 나면 곧 풀림(61초류 대기 없음 — 짧은 timeout)
  5) 실전(20/s, reserve2): data 18까지, order는 20까지

실행: python -m tests.test_kis_ratelimit
"""
from __future__ import annotations

import os
import multiprocessing
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.kis_ratelimit import SecondBucket, for_env


def _shared_worker(path, ready, start, result):
    bucket = SecondBucket(2, order_reserve=1, shared_path=path)
    ready.put(True)
    start.wait(5)
    result.put(bucket.try_acquire("data"))


def test_mock_data_capacity_and_order_reserve():
    b = SecondBucket(2, order_reserve=1)
    t0 = 1000.0
    assert b.try_acquire("data", now=t0) is True       # data 1/2 (cap 1)
    assert b.try_acquire("data", now=t0 + 0.1) is False  # data cap(1) 초과
    assert b.try_acquire("order", now=t0 + 0.2) is True  # order는 예약슬롯으로 통과
    assert b.try_acquire("order", now=t0 + 0.3) is False  # 총한도 2 소진
    print("[PASS] 모의 2/s: data 1건 제한·order 예약슬롯 통과·총한도 준수")


def test_window_slides():
    b = SecondBucket(2, order_reserve=1)
    t0 = 2000.0
    assert b.try_acquire("data", now=t0)
    assert b.try_acquire("order", now=t0 + 0.1)
    assert not b.try_acquire("order", now=t0 + 0.9)     # 아직 1초 안
    assert b.try_acquire("order", now=t0 + 1.05)        # 첫 호출 만료 → 슬롯
    print("[PASS] 1초 슬라이딩 윈도우 — 경과 후 재허용")


def test_blocking_acquire_short():
    b = SecondBucket(2, order_reserve=1)
    b.try_acquire("data")
    b.try_acquire("order")
    t0 = time.monotonic()
    ok = b.acquire("order", timeout=2.0)                # 1초 내 슬롯 나야 함
    dt = time.monotonic() - t0
    assert ok and dt < 1.5, f"차단 획득 지연: {dt:.2f}s"
    print(f"[PASS] 차단 acquire — {dt:.2f}s 내 해소(장대기 없음)")


def test_live_capacities():
    b = SecondBucket(20, order_reserve=2)               # 20/s, reserve 2
    t0 = 3000.0
    got_data = sum(b.try_acquire("data", now=t0 + i * 0.001) for i in range(30))
    assert got_data == 18, f"data 용량 오류: {got_data}(기대 18)"
    got_order = sum(b.try_acquire("order", now=t0 + 0.5 + i * 0.001) for i in range(5))
    assert got_order == 2, f"order 예약 오류: {got_order}(기대 2 — 총 20 도달)"
    print("[PASS] 실전 20/s: data 18·order 예약 2(총 20)")


def test_shared_bucket_combines_process_instances():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rate.json")
        a = SecondBucket(2, order_reserve=1, shared_path=path)
        b = SecondBucket(2, order_reserve=1, shared_path=path)
        t0 = 4000.0
        assert a.try_acquire("data", now=t0)
        assert not b.try_acquire("data", now=t0 + 0.1)
        assert b.try_acquire("order", now=t0 + 0.2)
        assert not a.try_acquire("order", now=t0 + 0.3)
    print("[PASS] 서로 다른 인스턴스가 호스트 공용 버킷 한도를 합산")


def test_shared_bucket_combines_processes():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rate.json")
        ctx = multiprocessing.get_context("fork")
        ready, result, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        ps = [ctx.Process(target=_shared_worker,
                          args=(path, ready, start, result)) for _ in range(2)]
        for p in ps:
            p.start()
        ready.get(timeout=5); ready.get(timeout=5)
        start.set()
        got = [result.get(timeout=5), result.get(timeout=5)]
        for p in ps:
            p.join(5)
        assert sorted(got) == [False, True], got
    print("[PASS] 두 프로세스 data 호출 합계도 모의 예약한도(1/s) 준수")


def main():
    test_mock_data_capacity_and_order_reserve()
    test_window_slides()
    test_blocking_acquire_short()
    test_live_capacities()
    test_shared_bucket_combines_process_instances()
    test_shared_bucket_combines_processes()
    print("\n모든 리미터 테스트 통과 — 사전 억제·order 예약·짧은 대기.")


if __name__ == "__main__":
    main()
