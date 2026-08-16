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
HEALTHY_AGE_S = 60.0
DEFAULT_RESET_AGE_S = 90.0
# A lone 60-90s excursion is tolerated, but an alternating chronic slowdown
# must not accumulate enough wall time to authorize an automatic recovery.
DEFAULT_MAX_SOFT_SAMPLES = 4


def _path() -> str:
    return os.environ.get("SELF_HEAL_STATE_PATH", DEFAULT_STATE_PATH)


def _observe_s() -> float:
    try: value = float(os.environ.get("SELF_HEAL_OBSERVE_S", DEFAULT_OBSERVE_S))
    except (TypeError, ValueError): value = DEFAULT_OBSERVE_S
    return value if math.isfinite(value) and value >= 0 else DEFAULT_OBSERVE_S


def _reset_age_s() -> float:
    try: value = float(os.environ.get("SELF_HEAL_RESET_AGE_S", DEFAULT_RESET_AGE_S))
    except (TypeError, ValueError): value = DEFAULT_RESET_AGE_S
    return value if math.isfinite(value) and value > HEALTHY_AGE_S else DEFAULT_RESET_AGE_S


def _max_soft_samples() -> int:
    try: value = int(os.environ.get("SELF_HEAL_MAX_SOFT_SAMPLES", DEFAULT_MAX_SOFT_SAMPLES))
    except (TypeError, ValueError): value = DEFAULT_MAX_SOFT_SAMPLES
    return value if value >= 1 else DEFAULT_MAX_SOFT_SAMPLES


def _day_kst(now: float) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(now, tz=tz).date().isoformat()


def _empty(day: str) -> dict:
    return {"v": 1, "day_kst": day, "used": False, "event": "",
            "healthy_since": 0.0, "last_healthy_at": 0.0,
            "soft_over_streak": 0, "soft_over_total": 0,
            "repeat_alerted_event": "", "pending_notice": "",
            "observer_pid": os.getpid(), "last_action": "idle",
            "last_why": "", "last_observed_s": 0.0,
            "last_remaining_s": _observe_s()}


def _load(now: float, *, observe_pid: bool = True) -> tuple[dict | None, bool]:
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
    # v1 files written by the previous release remain valid. New fields are
    # optional on read and receive conservative defaults.
    try:
        state["soft_over_streak"] = int(state.get("soft_over_streak", 0))
        state["soft_over_total"] = int(state.get("soft_over_total", 0))
        state["last_observed_s"] = float(state.get("last_observed_s", 0.0))
        state["last_remaining_s"] = float(state.get("last_remaining_s", _observe_s()))
        if state["soft_over_streak"] < 0 or state["soft_over_total"] < 0:
            return None, True
        if not all(math.isfinite(state[key]) and state[key] >= 0
                   for key in ("last_observed_s", "last_remaining_s")):
            return None, True
        state["last_action"] = str(state.get("last_action", "idle"))
        state["last_why"] = str(state.get("last_why", ""))
    except (TypeError, ValueError):
        return None, True
    day = _day_kst(now)
    if state["day_kst"] != day:
        pending = state["pending_notice"]
        state = _empty(day); state["pending_notice"] = pending
    elif observe_pid and state["observer_pid"] != os.getpid():
        state["observer_pid"] = os.getpid()
        state["event"] = ""; state["healthy_since"] = 0.0; state["last_healthy_at"] = 0.0
        state["soft_over_streak"] = 0; state["soft_over_total"] = 0
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


def _result(action: str, why: str = "", *, observed_s: float = 0.0,
            remaining_s: float | None = None, used_today: bool = False,
            **extra) -> dict:
    observed = max(0.0, float(observed_s))
    remaining = max(0.0, _observe_s() - observed) if remaining_s is None else max(0.0, float(remaining_s))
    out = {"action": str(action), "why": str(why),
           "observed_s": observed, "remaining_s": remaining,
           "used_today": bool(used_today)}
    out.update(extra)
    return out


def _finish(state: dict, action: str, why: str = "", *,
            observed_s: float = 0.0, remaining_s: float | None = None,
            persist: bool = True, **extra) -> dict:
    result = _result(action, why, observed_s=observed_s,
                     remaining_s=remaining_s, used_today=state["used"], **extra)
    state["last_action"] = result["action"]
    state["last_why"] = result["why"]
    state["last_observed_s"] = result["observed_s"]
    state["last_remaining_s"] = result["remaining_s"]
    if persist and not _save(state):
        return _result("blocked", "state_write", observed_s=observed_s,
                       remaining_s=remaining_s, used_today=state["used"])
    return result


def _clear_observation(state: dict) -> None:
    state["healthy_since"] = 0.0
    state["last_healthy_at"] = 0.0
    state["soft_over_streak"] = 0
    state["soft_over_total"] = 0


def status(*, now: float | None = None) -> dict:
    """Return the persisted decision only; never advances the state machine."""
    stamp = time.time() if now is None else float(now)
    state, corrupted = _load(stamp, observe_pid=False)
    if corrupted or state is None:
        return _result("blocked", "state_corrupt")
    return _result(state["last_action"], state["last_why"],
                   observed_s=state["last_observed_s"],
                   remaining_s=state["last_remaining_s"],
                   used_today=state["used"])


def cycle(*, heartbeat_age_s: float | None, now: float | None = None) -> dict:
    from bot import kill
    from bot.watchdog_policy import self_heal_allowed
    stamp = time.time() if now is None else float(now)
    state, corrupted = _load(stamp)
    if corrupted or state is None: return _result("blocked", "state_corrupt")
    if state["pending_notice"] and kill.level() == 0:
        lowered_by = str(kill.status().get("who") or "")
        if lowered_by != "self-heal":
            state["pending_notice"] = ""
            print("kill-self-heal: pending notice discarded — "
                  f"L0 owner={lowered_by or 'unknown'}", flush=True)
            return _finish(state, "notice_discarded", "l0_owner_not_self_heal")
        if _delivered(state["pending_notice"]):
            state["pending_notice"] = ""
            return _finish(state, "notice_delivered", "pending_notice")
        return _finish(state, "notice_retry", "notify_failed")
    status = kill.status(); level = kill.level()
    if level != 1 or int(status.get("level") or level) != 1:
        return _result("ineligible", f"level={level}", used_today=state["used"])
    who, why = str(status.get("who") or ""), str(status.get("why") or "")
    if not self_heal_allowed(who, why):
        return _finish(state, "ineligible", "source_or_reason")
    try: raised_at = float(status.get("ts") or 0)
    except (TypeError, ValueError): return _finish(state, "ineligible", "raise_ts")
    if not math.isfinite(raised_at) or raised_at <= 0 or raised_at > stamp:
        return _finish(state, "ineligible", "raise_ts")
    event = f"{raised_at:.6f}|{who}|{why}"
    if state["used"]:
        if state["repeat_alerted_event"] != event:
            msg = "🚨 kill-switch 반복 자동 상향 — 오늘 자동 복구 1회 소진, 수동 확인 필요"
            if _delivered(msg):
                state["repeat_alerted_event"] = event; _save(state)
                return _finish(state, "manual_alert", "daily_limit")
        return _finish(state, "blocked", "daily_limit")
    try: age = float(heartbeat_age_s) if heartbeat_age_s is not None else math.inf
    except (TypeError, ValueError): age = math.inf
    healthy = math.isfinite(age) and 0 <= age <= HEALTHY_AGE_S
    soft = math.isfinite(age) and HEALTHY_AGE_S < age <= _reset_age_s()
    if state["event"] != event:
        state["event"] = event
        _clear_observation(state)
        if not healthy:
            return _finish(state, "reset", "soft_without_baseline" if soft else "heartbeat_hard")
        state["healthy_since"] = stamp
        state["last_healthy_at"] = stamp
    elif not healthy:
        if not soft:
            _clear_observation(state)
            return _finish(state, "reset", "heartbeat_hard")
        if state["healthy_since"] <= 0:
            _clear_observation(state)
            return _finish(state, "reset", "soft_without_baseline")
        state["soft_over_streak"] += 1
        state["soft_over_total"] += 1
        observed = stamp - max(raised_at, state["healthy_since"])
        if state["soft_over_streak"] >= 2:
            _clear_observation(state)
            return _finish(state, "reset", "heartbeat_soft_consecutive")
        if state["soft_over_total"] > _max_soft_samples():
            _clear_observation(state)
            return _finish(state, "reset", "heartbeat_soft_budget")
        # A single soft sample neither advances nor resets the healthy anchor.
        return _finish(state, "degraded", "heartbeat_soft_single", observed_s=observed)
    if state["last_healthy_at"] <= 0 or stamp - state["last_healthy_at"] > MAX_CONTINUITY_GAP_S:
        state["healthy_since"] = stamp
    state["last_healthy_at"] = stamp
    state["soft_over_streak"] = 0
    if state["healthy_since"] <= 0: state["healthy_since"] = stamp
    observed = stamp - max(raised_at, state["healthy_since"])
    if observed < _observe_s():
        return _finish(state, "observing", "heartbeat_healthy", observed_s=observed)
    # The observation must be durable before readiness can authorize a lower.
    observed_result = _finish(state, "checking", "readiness", observed_s=observed)
    if observed_result["action"] == "blocked": return observed_result
    try: go, summary = _readiness_go()
    except Exception as exc:
        return _finish(state, "blocked", f"readiness:{type(exc).__name__}", observed_s=observed)
    if not go: return _finish(state, "blocked", summary, observed_s=observed)
    latest = kill.status()
    try: latest_event = f"{float(latest.get('ts') or 0):.6f}|{latest.get('who') or ''}|{latest.get('why') or ''}"
    except (TypeError, ValueError): latest_event = ""
    if kill.level() != 1 or int(latest.get("level") or 0) != 1 or latest_event != event:
        return _finish(state, "blocked", "kill_changed_during_readiness", observed_s=observed)
    notice = (f"✅ kill-switch 자동 L0 복구 — 원인: {why} · 연속 관찰 {int(observed)}초 · "
              f"readiness {summary}. 오늘 자동 복구 1회 소진, 재상향 시 수동 확인")
    state["used"] = True; state["pending_notice"] = notice
    if not _save(state): return _result("blocked", "state_write", observed_s=observed, used_today=True)
    ack = f"self-heal: {why} · observed={int(observed)}s · {summary}"
    try: lowered = kill.lower_level(0, ack=ack)
    except Exception as exc:
        return _finish(state, "blocked", f"lower:{type(exc).__name__}", observed_s=observed)
    if lowered != 0: return _finish(state, "blocked", f"lowered={lowered}", observed_s=observed)
    if _delivered(notice):
        state["pending_notice"] = ""
        return _finish(state, "recovered", "readiness_go", observed_s=observed,
                       remaining_s=0, notified=True)
    return _finish(state, "recovered", "notify_failed", observed_s=observed,
                   remaining_s=0, notified=False)
