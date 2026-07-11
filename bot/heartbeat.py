"""R4 — 파수꾼 생존성 SLA: heartbeat 기록 + 나이 판정.

KIS 미국주는 서버측 스톱이 없어 **파수꾼 다운 = 손절 무방비**. 그래서 생존성을
수치 SLA로 강제한다(리뷰 R4 확정):

  age ≤ 30s   → ok           (정상)
  age > 60s   → p0           (P0 경보 — 즉시 조사)
  age > 120s  → hard_disable (보유 포지션 있으면 신규 진입 hard-disable)

기록은 원자적 파일(temp→rename) — CF Worker dead-man·다른 프로세스가 읽는다.
파수꾼 루프가 매 사이클 write(), 감시자(서버 launcher/CF)가 age_s()+sla_status().
"""
from __future__ import annotations

import json
import os
import tempfile
import time

_DEFAULT = os.path.join(tempfile.gettempdir(), "sentinel_heartbeat.json")

OK = "ok"
P0 = "p0"
HARD_DISABLE = "hard_disable"

AGE_OK_S = 30.0
AGE_P0_S = 60.0
AGE_HARD_S = 120.0


def path() -> str:
    return os.environ.get("SENTINEL_HEARTBEAT_PATH", _DEFAULT)


def write(extra: dict | None = None) -> None:
    """heartbeat 기록(원자적). 실패해도 예외를 밖으로 안 던짐(파수꾼은 안 죽는다)."""
    p = path()
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "pid": os.getpid(),
                       **(extra or {})}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def age_s(now: float | None = None) -> float | None:
    """마지막 heartbeat 이후 경과 초. 파일 없음/손상이면 None(=미기록, 최악 취급)."""
    try:
        with open(path(), encoding="utf-8") as f:
            ts = float(json.load(f).get("ts", 0))
    except Exception:
        return None
    return max(0.0, (time.time() if now is None else now) - ts)


def sla_status(age: float | None, has_positions: bool) -> str:
    """SLA 판정. age=None(기록 없음)은 최악(hard/p0)으로 취급(fail-closed).

    hard_disable은 '보유 포지션 있는데 파수꾼이 오래 죽어있음' — 신규 진입을
    코드로 막아야 하는 상태. 포지션이 없으면 P0까지만(잃을 게 없음).
    """
    if age is None:
        return HARD_DISABLE if has_positions else P0
    if age > AGE_HARD_S:
        return HARD_DISABLE if has_positions else P0
    if age > AGE_P0_S:
        return P0
    return OK


def entry_allowed(has_positions: bool) -> bool:
    """신규 진입 허용? — SLA가 hard_disable이면 False(매수 실행기 X1이 사용)."""
    return sla_status(age_s(), has_positions) != HARD_DISABLE
