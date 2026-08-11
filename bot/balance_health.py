"""KIS 잔고 실패 사건의 경보 억제와 프로세스-수명 원인 통계."""
from __future__ import annotations

from collections import Counter, deque
import json
import math
import os
import time

DEFAULT_SUPPRESS_S = 1800.0
ESCALATE_S = 3600.0
WINDOW_S = 86400.0
_events: deque[tuple[float, str]] = deque()
_incident: dict | None = None


def _status_path() -> str:
    return os.environ.get("BALANCE_HEALTH_PATH",
                          os.path.join(os.path.dirname(__file__), "balance_health_status.json"))


def _suppress_s() -> float:
    try:
        value = float(os.environ.get("BALANCE_ALERT_SUPPRESS_S", DEFAULT_SUPPRESS_S))
    except (TypeError, ValueError):
        value = DEFAULT_SUPPRESS_S
    return value if math.isfinite(value) and value >= 1 else DEFAULT_SUPPRESS_S


def cause_label(detail: object = None) -> str:
    if isinstance(detail, BaseException):
        return f"exception:{type(detail).__name__}"
    if isinstance(detail, dict):
        msg = str(detail.get("msg_cd") or "").strip()
        rt = str(detail.get("rt_cd") or "").strip()
        exc = str(detail.get("exception") or "").strip()
        http = detail.get("http_status")
        if msg == "EGW00201":
            return "rate_limit:EGW00201"
        if detail.get("rate_limit") is True:
            return f"rate_limit:{exc or 'local'}"
        if msg:
            return f"msg_cd:{msg}"
        if rt:
            return f"rt_cd:{rt}"
        if exc:
            return f"exception:{exc}"
        if http not in (None, ""):
            return f"http:{http}"
    return str(detail or "unknown").strip() or "unknown"


def _prune(now: float) -> None:
    while _events and now - _events[0][0] > WINDOW_S:
        _events.popleft()


def _local_summary(stamp: float) -> dict:
    _prune(stamp)
    counts = Counter(cause for _ts, cause in _events)
    top = counts.most_common(1)[0] if counts else ("없음", 0)
    return {"count": len(_events), "top_cause": top[0], "top_count": top[1],
            "rate_limit_count": sum(n for cause, n in counts.items()
                                    if cause.startswith("rate_limit:")),
            "since_process_start": True}


def _write_status(stamp: float) -> None:
    data = {**_local_summary(stamp), "updated_at": stamp}
    path = _status_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, separators=(",", ":"))
            fp.flush(); os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass


def summary(*, now: float | None = None) -> dict:
    stamp = time.time() if now is None else float(now)
    local = _local_summary(stamp)
    if local["count"]:
        return local
    try:
        with open(_status_path(), encoding="utf-8") as fp:
            shared = json.load(fp)
        updated = float(shared.get("updated_at"))
        if not math.isfinite(updated) or stamp - updated > WINDOW_S or updated > stamp + 60:
            return local
        return {"count": max(0, int(shared.get("count") or 0)),
                "top_cause": str(shared.get("top_cause") or "없음"),
                "top_count": max(0, int(shared.get("top_count") or 0)),
                "rate_limit_count": max(0, int(shared.get("rate_limit_count") or 0)),
                "since_process_start": True}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return local


def reset_for_tests() -> None:
    global _incident
    _events.clear(); _incident = None


def _incident_cause_summary(incident: dict) -> str:
    counts = incident["causes"]
    top_cause, top_count = counts.most_common(1)[0]
    return f"최다 원인 {top_cause} {top_count}회 · 원인 {len(counts)}종"


def _send(text: str) -> bool:
    try:
        from bot import notify
        return bool(notify.send(text, critical=True, category="trade"))
    except Exception:
        return False


def record_failure(detail: object = None, *, now: float | None = None,
                   sender=None) -> bool:
    global _incident
    stamp = time.time() if now is None else float(now)
    cause = cause_label(detail)
    _events.append((stamp, cause)); _prune(stamp); _write_status(stamp)
    # HTTP/타임아웃/레이트리밋이 번갈아도 모두 하나의 "잔고 조회 실패"
    # 사건이다. 원인 라벨 변경으로 억제창을 초기화하지 않고 사건 내 통계로만
    # 보존한다.
    if _incident is None:
        _incident = {"first_at": stamp, "count": 0,
                     "last_sent_at": None, "escalated": False,
                     "causes": Counter()}
    inc = _incident; inc["count"] += 1; inc["causes"][cause] += 1
    duration = max(0.0, stamp - inc["first_at"])
    escalation_due = duration >= ESCALATE_S and not inc["escalated"]
    first_due = inc["last_sent_at"] is None
    periodic_due = (not escalation_due and not first_due
                    and stamp - inc["last_sent_at"] >= _suppress_s())
    if not (first_due or periodic_due or escalation_due):
        return False
    cause_summary = _incident_cause_summary(inc)
    if escalation_due:
        text = (f"🚨 KIS 잔고 조회 60분째 간헐 실패 — 누적 {inc['count']}회 · "
                f"{cause_summary}")
    elif first_due:
        text = f"🚨 KIS 잔고 조회 실패 — {cause_summary} · fail-closed 감시 유지"
    else:
        text = (f"⚠️ KIS 잔고 조회 실패 지속 — 누적 {inc['count']}회 · "
                f"{cause_summary}")
    delivered = _send(text) if sender is None else bool(sender(text))
    if not delivered:
        return False
    inc["last_sent_at"] = stamp
    if escalation_due: inc["escalated"] = True
    print(f"balance-health cause={cause} count={inc['count']} rate_limit_24h={summary(now=stamp)['rate_limit_count']}")
    return True


def record_success(*, now: float | None = None, sender=None) -> bool:
    global _incident
    if _incident is None:
        return False
    stamp = time.time() if now is None else float(now)
    inc = _incident
    minutes = max(0, int(round((stamp - inc["first_at"]) / 60.0)))
    text = (f"✅ KIS 잔고 조회 회복 — 실패 누적 {inc['count']}회 · "
            f"총 지속 {minutes}분 · {_incident_cause_summary(inc)}")
    delivered = _send(text) if sender is None else bool(sender(text))
    if not delivered:
        return False
    _incident = None; _write_status(stamp)
    return True
