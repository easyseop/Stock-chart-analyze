from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest import mock

from bot import heartbeat
from infra.server import watchdog


def test_self_heal_log_on_change_and_every_ten_minutes():
    state = {}
    out = io.StringIO()
    observing = {"action": "observing", "why": "heartbeat_healthy",
                 "observed_s": 120, "remaining_s": 1680, "used_today": False}
    with contextlib.redirect_stdout(out):
        assert watchdog._log_self_heal(state, observing, 1000)
        assert not watchdog._log_self_heal(state, observing, 1599)
        assert watchdog._log_self_heal(state, observing, 1600)
        changed = dict(observing, action="blocked", why="readiness:TimeoutError")
        assert watchdog._log_self_heal(state, changed, 1601)
    lines = [line for line in out.getvalue().splitlines() if line]
    assert len(lines) == 3
    assert "observed=120s" in lines[0] and "remaining=1680s" in lines[0]
    assert "readiness:TimeoutError" in lines[-1]


def test_heartbeat_alert_and_recovery_require_two_consecutive_samples():
    sent = []
    state = {"alerted": False, "bad_samples": 0, "good_samples": 0}
    with mock.patch.object(watchdog.notify, "send",
                           side_effect=lambda text, **kw: sent.append(text) or True):
        watchdog._update_alert_hysteresis(state, heartbeat.P0, 62)
        assert sent == [] and state["alerted"] is False
        watchdog._update_alert_hysteresis(state, heartbeat.P0, 71)
        assert len(sent) == 1 and state["alerted"] is True
        for age in (62, 71, 65, 73):
            watchdog._update_alert_hysteresis(state, heartbeat.P0, age)
        assert len(sent) == 1
        watchdog._update_alert_hysteresis(state, heartbeat.OK, 59)
        assert len(sent) == 1 and state["alerted"] is True
        watchdog._update_alert_hysteresis(state, heartbeat.OK, 55)
        assert len(sent) == 2 and "회복" in sent[-1] and state["alerted"] is False


def test_alert_delivery_failure_is_retried_not_latched():
    state = {"alerted": False, "bad_samples": 1, "good_samples": 0}
    delivery = mock.Mock(side_effect=[False, True])
    with mock.patch.object(watchdog.notify, "send", delivery):
        watchdog._update_alert_hysteresis(state, heartbeat.P0, 70)
        assert state["alerted"] is False
        watchdog._update_alert_hysteresis(state, heartbeat.P0, 72)
        assert state["alerted"] is True
    assert delivery.call_count == 2


def test_long_running_python_units_are_unbuffered():
    for name in ("watchdog.service", "sentinel.service", "buyloop.service",
                 "telegram.service", "portfolio-web.service"):
        text = Path("infra/server", name).read_text(encoding="utf-8")
        assert "Environment=PYTHONUNBUFFERED=1" in text, name


def main():
    test_self_heal_log_on_change_and_every_ten_minutes()
    test_heartbeat_alert_and_recovery_require_two_consecutive_samples()
    test_alert_delivery_failure_is_retried_not_latched()
    test_long_running_python_units_are_unbuffered()
    print("watchdog observability 4/4 PASS")


if __name__ == "__main__":
    main()
