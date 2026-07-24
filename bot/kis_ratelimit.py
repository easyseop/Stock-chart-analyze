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

동시성: 같은 프로세스 스레드는 threading.Lock, 여러 프로세스는 flock으로 보호한
호스트 공용 상태 파일을 쓴다. buyloop·sentinel·웹 프로세스가 따로 떠도 합산 호출이
환경별 한도를 넘지 않는다. [대조필요] 실제 KIS 한도 단위(앱키/계좌).
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
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

    def __init__(self, limit_per_s: int, order_reserve: int = 1,
                 shared_path: str | None = None):
        self.limit = max(1, int(limit_per_s))
        self.reserve = min(max(0, int(order_reserve)), self.limit - 1) \
            if self.limit > 1 else 0
        self.shared_path = shared_path
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
        if self.shared_path:
            return self._try_shared(plane, now)
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            if len(self._calls) < self._capacity_for(plane):
                self._calls.append(now)
                return True
            return False

    def _try_shared(self, plane: str, now: float | None) -> bool:
        """flock 아래 공용 윈도우를 읽고 한 슬롯을 원자적으로 예약한다.

        상태가 손상되면 호출을 허용하지 않는다. 손상 상태를 0건으로 간주하면 여러
        프로세스가 동시에 fail-open 되어 KIS 계좌 한도를 넘길 수 있기 때문이다.
        """
        path = str(self.shared_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._lock:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.chmod(path, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                # 대기 전에 wall clock을 잡으면, 먼저 잠금을 얻은 프로세스의 기록이
                # '미래 시각'처럼 보여 필터에서 빠지는 수백µs 경합 창이 생긴다.
                stamp = time.time() if now is None else float(now)
                raw = os.read(fd, 1_000_000).decode("utf-8")
                if raw.strip():
                    try:
                        saved = json.loads(raw)
                        calls = [float(v) for v in saved.get("calls", [])]
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return False
                else:
                    calls = []
                calls = [v for v in calls if 0 <= stamp - v < 1.0]
                if len(calls) >= self._capacity_for(plane):
                    return False
                calls.append(stamp)
                payload = json.dumps({"calls": calls}, separators=(",", ":")).encode()
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, payload)
                return True
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def acquire(self, plane: str = "data", timeout: float = 5.0) -> bool:
        """차단 획득 — 슬롯이 날 때까지(최대 timeout) 짧게 대기.
        order-plane 손절 경로는 timeout을 짧게(기본 5초) — 61초류 대기 금지."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            now = time.monotonic()
            # 공용 파일은 프로세스 간 비교 가능한 wall clock을, 로컬 버킷은
            # 시계 조정에 영향 없는 monotonic을 쓴다.
            got = (self.try_acquire(plane) if self.shared_path
                   else self.try_acquire(plane, now=now))
            if got:
                return True
            if now >= deadline:
                return False
            with self._lock:
                self._prune(now)
                wait = (self._calls[0] + 1.0 - now) if self._calls else 0.05
            time.sleep(min(max(wait, 0.02), deadline - now if deadline > now else 0.02))

    def used(self, now: float | None = None) -> int:
        if self.shared_path:
            stamp = time.time() if now is None else now
            path = str(self.shared_path)
            try:
                with open(path, encoding="utf-8") as fp:
                    calls = [float(v) for v in json.load(fp).get("calls", [])]
                return len([v for v in calls if 0 <= stamp - v < 1.0])
            except Exception:
                return self.limit                       # 손상/읽기실패도 fail-closed
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return len(self._calls)


def for_env(is_mock: bool) -> SecondBucket:
    """환경별 기본 리미터 — 모의 2/s(reserve 1), 실전 20/s(reserve 2).
    한도 '단위'(앱키/계좌)는 [대조필요] — 보수적으로 호스트 전 프로세스가 공유."""
    env = "mock" if is_mock else "live"
    path = os.environ.get(
        "KIS_RATE_STATE_PATH",
        os.path.join(tempfile.gettempdir(), f"stock-kis-rate-{env}.json"))
    return SecondBucket(2, order_reserve=1, shared_path=path) if is_mock \
        else SecondBucket(20, order_reserve=2, shared_path=path)
