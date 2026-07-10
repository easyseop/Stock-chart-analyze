"""KIS 초당 유량 리미터 — 실전 20/s·모의 2/s, plane(data/order) 분리.

왜(리뷰 R2): KIS는 계좌 단위 초당 카운트(실전 20, 모의 2 — 단위는 실측 대조 예정)이고
초과 시 HTTP 500 + EGW00201. 공식 백테스터의 '61초 대기 후 재시도'는 배치 조회(data-
plane)엔 적절하지만 **손절 주문/손절 상태조회(order-plane)에서 61초 멈춤은 손실 확대**.
그래서:
  · 사전 억제: 슬라이딩 윈도우(최근 1초 호출 수)로 한도 초과 전에 잠깐 대기.
  · plane 분리: order-plane은 예약 슬롯(reserve)을 남겨 data-plane이 유량을 다
    먹어도 손절 주문이 즉시 나갈 수 있게 한다.
  · 사후 대응 규칙(호출부 계약): EGW00201을 받으면 data는 백오프 재시도 OK,
    order는 **61초 대기 금지** — 짧은 백오프 1회 후 실패 시 P0(손절 집행 불능 경보).

동시성: 같은 프로세스 안 스레드는 threading.Lock으로 직렬화. 다중 프로세스 합산
한도는 이 리미터로 못 막는다(각자 자기 몫만 세므로) — Stage 2 상시서버는 단일
프로세스(asyncio/스레드) 구성이 전제(README/설계 04 I3). [대조필요] 한도 단위.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class SecondBucket:
    """최근 1초 슬라이딩 윈도우 리미터 + order-plane 예약 슬롯.

    limit_per_s: 초당 총 한도(실전 20, 모의 2).
    order_reserve: 총 한도 중 order-plane 전용으로 남겨두는 슬롯 수.
      · 모의(2/s): reserve 1 → data는 초당 1, order는 언제나 1슬롯 확보.
      · 실전(20/s): reserve 2 정도 권장.
    """

    def __init__(self, limit_per_s: int, order_reserve: int = 1):
        self.limit = max(1, int(limit_per_s))
        self.reserve = min(max(0, int(order_reserve)), self.limit - 1) \
            if self.limit > 1 else 0
        self._calls: deque[float] = deque()      # 최근 호출 시각들
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= 1.0:
            self._calls.popleft()

    def _capacity_for(self, plane: str) -> int:
        # data-plane은 예약 슬롯을 못 쓴다 — order가 굶지 않게.
        return self.limit if plane == "order" else self.limit - self.reserve

    def try_acquire(self, plane: str = "data", now: float | None = None) -> bool:
        """비차단 획득. True면 즉시 호출 가능(호출로 카운트됨)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            if len(self._calls) < self._capacity_for(plane):
                self._calls.append(now)
                return True
            return False

    def acquire(self, plane: str = "data", timeout: float = 5.0) -> bool:
        """차단 획득 — 슬롯이 날 때까지(최대 timeout) 짧게 대기.
        order-plane 손절 경로는 timeout을 짧게(기본 5초) — 61초류 대기 금지."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            now = time.monotonic()
            if self.try_acquire(plane, now=now):
                return True
            if now >= deadline:
                return False
            with self._lock:
                self._prune(now)
                wait = (self._calls[0] + 1.0 - now) if self._calls else 0.05
            time.sleep(min(max(wait, 0.02), deadline - now if deadline > now else 0.02))

    def used(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return len(self._calls)


def for_env(is_mock: bool) -> SecondBucket:
    """환경별 기본 리미터 — 모의 2/s(reserve 1), 실전 20/s(reserve 2).
    한도 '단위'(앱키/계좌)는 [대조필요] — 보수적으로 프로세스 전역 1개를 공유."""
    return SecondBucket(2, order_reserve=1) if is_mock \
        else SecondBucket(20, order_reserve=2)
