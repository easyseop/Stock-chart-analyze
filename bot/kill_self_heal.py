"""오인으로 판명 가능한 자동 L1만 좁게 L0로 복구하는 watchdog 상태기계."""
from __future__ import annotations

import datetime
import json
import math
import os
import time

DEFAULT_STATE_PATH = "/var/lib/stock-watchdog/self_heal.json"
DEFAULT_OBSERVE_S = 1800.0
MAX_CONTINUITY_GAP_S = 60.0


def _path() -> str:
    return os.environ.get("SELF_HEAL_STATE_PATH", DEFAULT_STATE_PATH)


def _observe_s() -> float:
    try: value = float(os.environ.get("SELF_HEAL_OBSERVE_S", DEFAULT_OBSERVE_S))
    except (TypeError, ValueError): value = DEFAULT_OBSERVE_S
    return value if math.isfinite(value) and value >= 0 else DEFAULT_OBSERVE_S


def _day_kst(now: float) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(now, tz=tz).date().isoformat()


def _empty(day: str) -> dict:
    return {"v": 1, "day_kst": day, "used": False, "event": "",
            "healthy_since": 0.0, "last_healthy_at": 0.0,
            "repeat_alerted_event": "", "pending_notice": "",
            "observer_pid": os.getpid()}


def _load(now: float) -> tuple[dict | None, bool]:
    try:
        with open(_path(), encoding="utf-8") as fp: state = json.load(fp)
    except FileNotFoundError:
        return _empty(_day_kst(now)), False
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, True
    required = {"v", "day_kst", "used", "event", "healthy_since",
                "last_healthy_at", "repeat_alerted_event", "pending_notice",
                "observer_pid"}
    if not isinstance(state, dict) or not required.issubset(state):
        return None, True
    try:
        if int(state["v"]) != 1: return None, True
        for key in ("healthy_since", "last_healthy_at"):
            value = float(state[key])
            if not math.isfinite(value) or value < 0: return None, True
            state[key] = value
        state["used"] = bool(state["used"])
        state["observer_pid"] = int(state["observer_pid"])
        for key in ("day_kst", "event", "repeat_alerted_event", "pending_notice"):
            state[key] = str(state[key])
    except (TypeError, ValueError):
        return None, True
    day = _day_kst(now)
    if state["day_kst"] != day:
        pending = state["pending_notice"]
        state = _empty(day); state["pending_notice"] = pending
    elif state["observer_pid"] != os.getpid():
        state["observer_pid"] = os.getpid()
        state["event"] = ""; state["healthy_since"] = 0.0; state["last_healthy_at"] = 0.0
    return state, False


def _save(state: dict) -> bool:
    path = _path(); parent = os.path.dirname(path) or "."; tmp = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, separators=(",", ":"))
            fp.flush(); os.fsync(fp.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, path); return True
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass
        return False


def _delivered(text: str) -> bool:
    from bot import notify
    try: return bool(notify.send(text, critical=True, category="trade"))
    except Exception: return False


def _readiness_go() -> tuple[bool, str]:
    from bot import l1_readiness
    evidence = l1_readiness.load_evidence()
    snapshot = l1_readiness.collect_runtime(fetch_broker=True, evidence=evidence)
    report = l1_readiness.evaluate(snapshot, evidence, scope="l0")
    blockers = report.get("blockers")
    if not isinstance(blockers, list) or blockers:
        return False, f"blockers={len(blockers) if isinstance(blockers, list) else '?'}"
    if report.get("ready_for_operator_review") is not True:
        return False, "ready=false"
    return True, "scope=l0 blockers=0"


def cycle(*, heartbeat_age_s: float | None, now: float | None = None) -> dict:
    from bot import kill
    from bot.watchdog_policy import self_heal_allowed
    stamp = time.time() if now is None else float(now)
    state, corrupted = _load(stamp)
    if corrupted or state is None: return {"action": "blocked", "why": "state_corrupt"}
    if state["pending_notice"] and kill.level() == 0:
        lowered_by = str(kill.status().get("who") or "")
        if lowered_by != "self-heal":
            state["pending_notice"] = ""
            saved = _save(state)
            print("kill-self-heal: pending notice discarded — "
                  f"L0 owner={lowered_by or 'unknown'}")
            if not saved:
                return {"action": "blocked", "why": "state_write"}
            return {"action": "notice_discarded",
                    "why": "l0_owner_not_self_heal"}
        if _delivered(state["pending_notice"]):
            state["pending_notice"] = ""; _save(state)
            return {"action": "notice_delivered"}
        return {"action": "notice_retry"}
    status = kill.status(); level = kill.level()
    if level != 1 or int(status.get("level") or level) != 1:
        return {"action": "ineligible", "why": f"level={level}"}
    who, why = str(status.get("who") or ""), str(status.get("why") or "")
    if not self_heal_allowed(who, why):
        return {"action": "ineligible", "why": "source_or_reason"}
    try: raised_at = float(status.get("ts") or 0)
    except (TypeError, ValueError): return {"action": "ineligible", "why": "raise_ts"}
    if not math.isfinite(raised_at) or raised_at <= 0 or raised_at > stamp:
        return {"action": "ineligible", "why": "raise_ts"}
    event = f"{raised_at:.6f}|{who}|{why}"
    if state["used"]:
        if state["repeat_alerted_event"] != event:
            msg = "🚨 kill-switch 반복 자동 상향 — 오늘 자동 복구 1회 소진, 수동 확인 필요"
            if _delivered(msg):
                state["repeat_alerted_event"] = event; _save(state)
                return {"action": "manual_alert"}
        return {"action": "blocked", "why": "daily_limit"}
    try: age = float(heartbeat_age_s) if heartbeat_age_s is not None else math.inf
    except (TypeError, ValueError): age = math.inf
    healthy = math.isfinite(age) and 0 <= age < 60.0
    if state["event"] != event:
        state["event"] = event
        state["healthy_since"] = stamp if healthy else 0.0
        state["last_healthy_at"] = stamp if healthy else 0.0
        _save(state); return {"action": "observing" if healthy else "reset"}
    if not healthy:
        state["healthy_since"] = 0.0; state["last_healthy_at"] = 0.0; _save(state)
        return {"action": "reset"}
    if state["last_healthy_at"] <= 0 or stamp - state["last_healthy_at"] > MAX_CONTINUITY_GAP_S:
        state["healthy_since"] = stamp
    state["last_healthy_at"] = stamp
    if state["healthy_since"] <= 0: state["healthy_since"] = stamp
    _save(state)
    observed = stamp - max(raised_at, state["healthy_since"])
    if observed < _observe_s(): return {"action": "observing", "observed_s": observed}
    try: go, summary = _readiness_go()
    except Exception as exc: return {"action": "blocked", "why": f"readiness:{type(exc).__name__}"}
    if not go: return {"action": "blocked", "why": summary}
    latest = kill.status()
    latest_event = f"{float(latest.get('ts') or 0):.6f}|{latest.get('who') or ''}|{latest.get('why') or ''}"
    if kill.level() != 1 or int(latest.get("level") or 0) != 1 or latest_event != event:
        return {"action": "blocked", "why": "kill_changed_during_readiness"}
    notice = (f"✅ kill-switch 자동 L0 복구 — 원인: {why} · 연속 관찰 {int(observed)}초 · "
              f"readiness {summary}. 오늘 자동 복구 1회 소진, 재상향 시 수동 확인")
    state["used"] = True; state["pending_notice"] = notice
    if not _save(state): return {"action": "blocked", "why": "state_write"}
    ack = f"self-heal: {why} · observed={int(observed)}s · {summary}"
    try: lowered = kill.lower_level(0, ack=ack)
    except Exception as exc: return {"action": "blocked", "why": f"lower:{type(exc).__name__}"}
    if lowered != 0: return {"action": "blocked", "why": f"lowered={lowered}"}
    if _delivered(notice):
        state["pending_notice"] = ""; _save(state)
        return {"action": "recovered", "notified": True}
    return {"action": "recovered", "notified": False}
