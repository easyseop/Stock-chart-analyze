#!/usr/bin/env python3
"""R4 — 동일 서버 watchdog 프로세스: 파수꾼 heartbeat 감시·재기동·P0.

파수꾼(sentinel)이 죽으면 KIS 미국주는 서버측 스톱이 없어 손절이 안 나간다.
이 watchdog은 파수꾼과 **별도 systemd 유닛**으로 돌며:
  · heartbeat age > 60s  → P0 알림(ntfy/텔레그램)
  · heartbeat age > 90s  → `systemctl restart sentinel.service` 시도(최대 3회/10분)
  · age > 120s(+보유)    → kill-switch L1 상향(신규 진입 hard-disable — R4)
  · 복구되면 회복 알림 1회.

표준 라이브러리만. 실패해도 스스로는 죽지 않는다(감시자의 감시는 systemd Restart=).
사용: systemd unit(watchdog.service)이 이 스크립트를 상시 실행.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get(
    "BOT_REPO_DIR", os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

from bot import heartbeat, kill, notify  # noqa: E402

CHECK_SEC = 15
RESTART_AFTER_S = 90.0
MAX_RESTARTS = 3
RESTART_WINDOW_S = 600.0
UNIT = os.environ.get("SENTINEL_UNIT", "sentinel.service")


def _restart_sentinel() -> bool:
    try:
        r = subprocess.run(["systemctl", "restart", UNIT],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def main() -> None:
    print(f"watchdog 시작 — unit={UNIT} · 점검 {CHECK_SEC}s")
    restarts: list[float] = []
    alerted = False
    while True:
        try:
            age = heartbeat.age_s()
            sla = heartbeat.sla_status(age, has_positions=True)  # 보수적: 보유 가정
            if sla != heartbeat.OK:
                if not alerted:
                    notify.send(f"🚨 watchdog: 파수꾼 heartbeat "
                                f"{'없음' if age is None else f'{age:.0f}s'} "
                                f"({sla})", critical=True)
                    alerted = True
                if (age is None or age > RESTART_AFTER_S):
                    now = time.time()
                    restarts = [t for t in restarts if now - t < RESTART_WINDOW_S]
                    if len(restarts) < MAX_RESTARTS:
                        ok = _restart_sentinel()
                        restarts.append(now)
                        notify.send(f"🔄 watchdog: {UNIT} 재기동 "
                                    f"{'성공' if ok else '실패'} "
                                    f"({len(restarts)}/{MAX_RESTARTS})",
                                    critical=True)
                    elif sla == heartbeat.HARD_DISABLE:
                        kill.raise_level(1, "watchdog",
                                         "파수꾼 120s+ 다운·재기동 소진 — 신규 금지")
            elif alerted:
                notify.send("✅ watchdog: 파수꾼 heartbeat 회복", critical=True)
                alerted = False
        except Exception as e:
            print(f"[watchdog 오류] {type(e).__name__}: {e}")
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
