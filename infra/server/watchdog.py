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

from bot import deploy_grace, heartbeat, kill, kill_self_heal, notify  # noqa: E402
from bot.watchdog_policy import HEARTBEAT_EXHAUSTED_REASON, WATCHDOG_WHO  # noqa: E402

CHECK_SEC = 15
RESTART_AFTER_S = 90.0
MAX_RESTARTS = 3
RESTART_WINDOW_S = 600.0
ALERT_CONFIRM_SAMPLES = 2
SELF_HEAL_LOG_INTERVAL_S = 600.0
UNIT = os.environ.get("SENTINEL_UNIT", "sentinel.service")


def _restart_sentinel() -> bool:
    try:
        r = subprocess.run(["systemctl", "restart", UNIT],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _update_alert_hysteresis(state: dict, sla: str, age: float | None) -> None:
    """Debounce notifications only; restart and kill decisions stay immediate."""
    if sla != heartbeat.OK:
        state["bad_samples"] = int(state.get("bad_samples", 0)) + 1
        state["good_samples"] = 0
        if state["bad_samples"] >= ALERT_CONFIRM_SAMPLES and not state.get("alerted"):
            delivered = notify.send(
                f"🚨 watchdog: 파수꾼 heartbeat "
                f"{'없음' if age is None else f'{age:.0f}s'} ({sla})", critical=True)
            if delivered is not False:
                state["alerted"] = True
        return
    state["good_samples"] = int(state.get("good_samples", 0)) + 1
    state["bad_samples"] = 0
    if state.get("alerted") and state["good_samples"] >= ALERT_CONFIRM_SAMPLES:
        delivered = notify.send("✅ watchdog: 파수꾼 heartbeat 회복", critical=True)
        if delivered is not False:
            state["alerted"] = False


def _log_self_heal(state: dict, result: dict, stamp: float) -> bool:
    signature = (str(result.get("action") or ""), str(result.get("why") or ""))
    changed = signature != tuple(state.get("self_heal_log_signature", ()))
    periodic = stamp - float(state.get("self_heal_log_at") or 0) >= SELF_HEAL_LOG_INTERVAL_S
    if not changed and not periodic:
        return False
    observed = max(0.0, float(result.get("observed_s") or 0))
    remaining = max(0.0, float(result.get("remaining_s") or 0))
    print("watchdog self-heal: "
          f"action={signature[0] or '-'} why={signature[1] or '-'} "
          f"observed={observed:.0f}s remaining={remaining:.0f}s "
          f"used_today={bool(result.get('used_today'))}", flush=True)
    state["self_heal_log_signature"] = signature
    state["self_heal_log_at"] = stamp
    return True


def check_cycle(state: dict, *, now: float | None = None) -> None:
    stamp = time.time() if now is None else float(now)
    age = heartbeat.age_s()
    sla = heartbeat.sla_status(age, has_positions=True)
    grace = deploy_grace.active(now=stamp)
    if grace and not state.get("grace"): print("watchdog: deploy grace 시작", flush=True)
    elif not grace and state.get("grace"): print("watchdog: deploy grace 종료", flush=True)
    state["grace"] = grace
    _update_alert_hysteresis(state, sla, age)
    if sla != heartbeat.OK:
        if grace:
            print("watchdog: deploy grace 중 — heartbeat age "
                  f"{'없음' if age is None else f'{age:.0f}s'} 무시", flush=True)
            return
        if age is None or age > RESTART_AFTER_S:
            restarts = [t for t in state.get("restarts", []) if stamp - t < RESTART_WINDOW_S]
            if len(restarts) < MAX_RESTARTS:
                ok = _restart_sentinel(); restarts.append(stamp)
                notify.send(f"🔄 watchdog: {UNIT} 재기동 {'성공' if ok else '실패'} "
                            f"({len(restarts)}/{MAX_RESTARTS})", critical=True)
            elif sla == heartbeat.HARD_DISABLE:
                kill.raise_level(1, WATCHDOG_WHO, HEARTBEAT_EXHAUSTED_REASON)
            state["restarts"] = restarts
    try:
        result = kill_self_heal.cycle(heartbeat_age_s=age, now=stamp)
        _log_self_heal(state, result, stamp)
    except Exception as exc:
        print(f"[watchdog self-heal 오류] {type(exc).__name__}: {exc}", flush=True)


def _log_alert_channels() -> None:
    """부팅 첫 줄에 알림 채널 구성을 남긴다 — 조용한 드라이런 재발 방지.

    실측(2026-08-17): watchdog 유닛만 `set -a` 없이 kis.env를 source해
    텔레그램 자격증명이 파이썬에 주입되지 않았고, P0 경보가 전부
    `[드라이런]`으로 저널에만 찍혔다. 경보 채널이 죽으면 그 사실을 알릴
    채널도 같이 죽으므로, 부팅 로그에 드러내는 것이 유일한 조기 발견 수단이다."""
    try:
        st = notify.channel_status()
    except Exception as exc:      # 진단용 부가 로그가 감시를 막으면 안 된다
        print(f"[watchdog 알림채널 확인 실패] {type(exc).__name__}: {exc}",
              flush=True)
        return
    tg = "OK" if st.get("telegram") else "미설정 ⚠️ P0 경보가 도달하지 않음"
    nt = "OK" if st.get("ntfy") else "미설정(이중화 없음)"
    print(f"watchdog: 알림 채널 telegram={tg} · ntfy={nt}", flush=True)


def main() -> None:
    print(f"watchdog 시작 — unit={UNIT} · 점검 {CHECK_SEC}s", flush=True)
    _log_alert_channels()
    state = {"restarts": [], "alerted": False, "grace": False,
             "bad_samples": 0, "good_samples": 0,
             "self_heal_log_signature": (), "self_heal_log_at": 0.0}
    while True:
        try:
            check_cycle(state)
        except Exception as e:
            print(f"[watchdog 오류] {type(e).__name__}: {e}", flush=True)
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
