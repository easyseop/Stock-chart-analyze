"""KIS outage classification, per-reason self-heal limits, and timeout damping."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from unittest import mock

from bot import balance_health, heartbeat, kill, kill_self_heal, kis
from bot.watchdog_policy import (BALANCE_FAILURE_REASON,
                                 HEARTBEAT_EXHAUSTED_REASON, WATCHDOG_WHO)
from infra.server import watchdog


def _watchdog_state(restarts=None) -> dict:
    return {"restarts": list(restarts or []), "alerted": False,
            "grace": False, "bad_samples": 0, "good_samples": 0,
            "self_heal_log_signature": (), "self_heal_log_at": 0.0}


def test_balance_failure_evidence_is_persisted_and_success_resets_it():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"BALANCE_HEALTH_PATH": f"{tmp}/health.json"}):
        balance_health.reset_for_tests()
        with mock.patch.object(balance_health, "_send", return_value=True):
            for stamp in (100.0, 160.0, 220.0):
                balance_health.record_failure(TimeoutError(), now=stamp)
            evidence = balance_health.outage_evidence(now=221.0)
            assert evidence and evidence["outage"] is True
            assert evidence["consecutive"] == 3
            assert evidence["last_cause"] == "exception:TimeoutError"
            balance_health.record_success(now=230.0)
        recovered = balance_health.outage_evidence(now=231.0)
        assert recovered and recovered["outage"] is False
        assert recovered["consecutive"] == 0


def test_kis_outage_skips_restart_and_raises_balance_reason():
    state = _watchdog_state()
    evidence = {"outage": True, "consecutive": 4,
                "last_cause": "exception:TimeoutError",
                "last_failure_age_s": 8.0}
    sent = []
    healed = {"action": "reset", "why": "heartbeat_hard",
              "observed_s": 0, "remaining_s": 1800, "used_today": False}
    output = io.StringIO()
    with mock.patch.object(watchdog.heartbeat, "age_s", return_value=130.0), \
         mock.patch.object(watchdog.heartbeat, "sla_status",
                           return_value=heartbeat.HARD_DISABLE), \
         mock.patch.object(watchdog.deploy_grace, "active", return_value=False), \
         mock.patch.object(watchdog.balance_health, "outage_evidence",
                           return_value=evidence), \
         mock.patch.object(watchdog, "_restart_sentinel") as restart, \
         mock.patch.object(watchdog.kill, "level", return_value=0), \
         mock.patch.object(watchdog.kill, "raise_level", return_value=1) as raised, \
         mock.patch.object(watchdog.kill_self_heal, "cycle", return_value=healed), \
         mock.patch.object(watchdog.notify, "send",
                           side_effect=lambda text, **kw: sent.append(text) or True), \
         contextlib.redirect_stdout(output):
        watchdog.check_cycle(state, now=1000.0)
    restart.assert_not_called()
    raised.assert_called_once_with(1, WATCHDOG_WHO, BALANCE_FAILURE_REASON)
    assert any("최근 4회" in text and "TimeoutError" in text for text in sent)
    assert "outage_cause=exception:TimeoutError" in output.getvalue()


def test_missing_or_negative_classifier_preserves_legacy_watchdog_path():
    # Missing/corrupt classifier evidence is not an outage: restart budget and
    # HEARTBEAT reason remain unchanged.
    state = _watchdog_state()
    with mock.patch.object(watchdog.heartbeat, "age_s", return_value=130.0), \
         mock.patch.object(watchdog.heartbeat, "sla_status",
                           return_value=heartbeat.HARD_DISABLE), \
         mock.patch.object(watchdog.deploy_grace, "active", return_value=False), \
         mock.patch.object(watchdog.balance_health, "outage_evidence",
                           return_value=None), \
         mock.patch.object(watchdog, "_restart_sentinel", return_value=True) as restart, \
         mock.patch.object(watchdog.kill, "level", return_value=0), \
         mock.patch.object(watchdog.kill, "raise_level") as raised, \
         mock.patch.object(watchdog.kill_self_heal, "cycle",
                           return_value={"action": "ineligible"}), \
         mock.patch.object(watchdog.notify, "send", return_value=True):
        watchdog.check_cycle(state, now=1000.0)
    assert restart.call_count == 1 and raised.call_count == 0

    state = _watchdog_state([501.0, 502.0, 503.0])
    with mock.patch.object(watchdog.heartbeat, "age_s", return_value=130.0), \
         mock.patch.object(watchdog.heartbeat, "sla_status",
                           return_value=heartbeat.HARD_DISABLE), \
         mock.patch.object(watchdog.deploy_grace, "active", return_value=False), \
         mock.patch.object(watchdog.balance_health, "outage_evidence",
                           return_value={"outage": False}), \
         mock.patch.object(watchdog, "_restart_sentinel") as restart, \
         mock.patch.object(watchdog.kill, "level", return_value=0), \
         mock.patch.object(watchdog.kill, "raise_level", return_value=1) as raised, \
         mock.patch.object(watchdog.kill_self_heal, "cycle",
                           return_value={"action": "ineligible"}), \
         mock.patch.object(watchdog.notify, "send", return_value=True):
        watchdog.check_cycle(state, now=1000.0)
    restart.assert_not_called()
    raised.assert_called_once_with(1, WATCHDOG_WHO, HEARTBEAT_EXHAUSTED_REASON)


def test_corrupt_balance_evidence_is_unavailable_not_zero_or_outage():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"BALANCE_HEALTH_PATH": f"{tmp}/health.json"}):
        balance_health.reset_for_tests()
        with open(f"{tmp}/health.json", "w", encoding="utf-8") as fp:
            fp.write("{broken")
        assert balance_health.outage_evidence(now=1000.0) is None


class _Resp(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_get_timeout_uses_five_seconds_until_one_success_restores_fifteen():
    timeouts = []
    outcomes = [TimeoutError("burst"),
                _Resp(json.dumps({"rt_cd": "0"}).encode()),
                _Resp(json.dumps({"rt_cd": "0"}).encode())]

    def opener(_req, timeout=None):
        timeouts.append(timeout)
        value = outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    token = kis._GET_TIMEOUT_BACKOFF.set(False)
    try:
        with mock.patch.object(kis, "_token", return_value="tok"), \
             mock.patch.object(kis, "_cred", return_value=("key", "secret")), \
             mock.patch.object(kis._LIMITER, "acquire", return_value=True), \
             mock.patch.object(kis.urllib.request, "urlopen", side_effect=opener):
            assert kis._get("/one", "TR", {}) is None
            assert kis._get("/two", "TR", {})["rt_cd"] == "0"
            assert kis._get("/three", "TR", {})["rt_cd"] == "0"
    finally:
        kis._GET_TIMEOUT_BACKOFF.reset(token)
    assert timeouts == [15, 5, 15], timeouts


def _self_heal_env(tmp: str) -> dict[str, str]:
    return {"KILL_STATE_PATH": f"{tmp}/kill.json",
            "KILL_LOG_PATH": f"{tmp}/kill.jsonl",
            "SELF_HEAL_STATE_PATH": f"{tmp}/heal.json",
            "SELF_HEAL_OBSERVE_S": "0", "KILL_LEVEL": "0"}


def _raise_at(reason: str, stamp: float) -> None:
    with mock.patch.object(kill.time, "time", return_value=stamp):
        assert kill._write_file(1, WATCHDOG_WHO, reason)


def test_reason_specific_daily_limits_and_unchanged_safety_gates():
    fixed_day = "2099-01-01"
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.dict(os.environ, _self_heal_env(tmp)), \
         mock.patch.object(kill_self_heal, "_day_kst", return_value=fixed_day), \
         mock.patch.object(kill_self_heal, "_readiness_go",
                           return_value=(True, "scope=l0 blockers=0")), \
         mock.patch.object(kill_self_heal, "_delivered", return_value=True):
        # BALANCE may recover twice, never a third time.
        for index in range(2):
            stamp = 2_000_000_000.0 + index * 100
            _raise_at(BALANCE_FAILURE_REASON, stamp)
            out = kill_self_heal.cycle(heartbeat_age_s=1, now=stamp)
            assert out["action"] == "recovered", out
            assert kill.level() == 0
        stamp = 2_000_000_200.0
        _raise_at(BALANCE_FAILURE_REASON, stamp)
        out = kill_self_heal.cycle(heartbeat_age_s=1, now=stamp)
        assert out["action"] in ("manual_alert", "blocked") and kill.level() == 1

    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.dict(os.environ, _self_heal_env(tmp)), \
         mock.patch.object(kill_self_heal, "_day_kst", return_value=fixed_day), \
         mock.patch.object(kill_self_heal, "_readiness_go",
                           return_value=(True, "scope=l0 blockers=0")), \
         mock.patch.object(kill_self_heal, "_delivered", return_value=True):
        # HEARTBEAT remains one per day.
        _raise_at(HEARTBEAT_EXHAUSTED_REASON, 2_100_000_000.0)
        assert kill_self_heal.cycle(
            heartbeat_age_s=1, now=2_100_000_000.0)["action"] == "recovered"
        _raise_at(HEARTBEAT_EXHAUSTED_REASON, 2_100_000_100.0)
        assert kill_self_heal.cycle(
            heartbeat_age_s=1, now=2_100_000_100.0)["action"] in (
                "manual_alert", "blocked")
        assert kill.level() == 1

        # Operator/L2 remain ineligible and readiness is never consulted.
        with mock.patch.object(kill_self_heal, "_readiness_go") as readiness:
            with mock.patch.object(kill.time, "time", return_value=2_100_000_200.0):
                kill._write_file(1, "operator", BALANCE_FAILURE_REASON)
            assert kill_self_heal.cycle(
                heartbeat_age_s=1, now=2_100_000_200.0)["action"] == "ineligible"
            with mock.patch.object(kill.time, "time", return_value=2_100_000_300.0):
                kill._write_file(2, WATCHDOG_WHO, BALANCE_FAILURE_REASON)
            assert kill_self_heal.cycle(
                heartbeat_age_s=1, now=2_100_000_300.0)["action"] == "ineligible"
            readiness.assert_not_called()


def main():
    tests = (
        test_balance_failure_evidence_is_persisted_and_success_resets_it,
        test_kis_outage_skips_restart_and_raises_balance_reason,
        test_missing_or_negative_classifier_preserves_legacy_watchdog_path,
        test_corrupt_balance_evidence_is_unavailable_not_zero_or_outage,
        test_get_timeout_uses_five_seconds_until_one_success_restores_fifteen,
        test_reason_specific_daily_limits_and_unchanged_safety_gates,
    )
    for test in tests:
        test()
    print(f"KIS outage classification {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()
