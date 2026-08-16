from __future__ import annotations

import datetime
import io
import json
import tempfile
from pathlib import Path

from scripts import kick_audit

NOW = datetime.datetime(2026, 8, 16, 12, tzinfo=datetime.timezone.utc)


def _run(event: str, actor: str) -> dict:
    return {"event": event, "triggering_actor": {"login": actor},
            "created_at": "2026-08-16T11:00:00Z"}


def _case(runs):
    tmp = tempfile.TemporaryDirectory()
    sent = []
    logs = []
    result = kick_audit.evaluate(
        runs, state_path=f"{tmp.name}/state.json", now=NOW,
        sender=lambda text: sent.append(text) or True, logger=logs.append)
    return tmp, sent, logs, result


def test_external_zero_schedule_normal_alerts_once():
    tmp, sent, _logs, result = _case([_run("schedule", "github-actions[bot]")])
    try:
        assert result["status"] == "external_zero" and len(sent) == 1
        assert "외부 킥" in sent[0] and "P0" not in sent[0]
    finally: tmp.cleanup()


def test_schedule_zero_external_normal_alerts_once():
    tmp, sent, _logs, result = _case([_run("workflow_dispatch", "easyseop")])
    try:
        assert result["status"] == "schedule_zero" and len(sent) == 1
        assert "GitHub 크론" in sent[0]
    finally: tmp.cleanup()


def test_both_zero_is_single_p0():
    tmp, sent, _logs, result = _case([])
    try:
        assert result["status"] == "all_layers_zero" and len(sent) == 1
        assert "P0" in sent[0]
    finally: tmp.cleanup()


def test_both_normal_is_silent():
    runs = [_run("schedule", "github-actions[bot]"),
            _run("workflow_dispatch", "easyseop")]
    tmp, sent, _logs, result = _case(runs)
    try:
        assert result["status"] == "healthy" and sent == []
    finally: tmp.cleanup()


def test_api_failure_is_unknown_and_never_alerts():
    tmp, sent, logs, result = _case(None)
    try:
        assert result["status"] == "unknown" and sent == []
        assert any("조회 실패" in line and "무발사로 판정하지 않음" in line for line in logs)
    finally: tmp.cleanup()


def test_same_verdict_is_latched_once_per_day():
    with tempfile.TemporaryDirectory() as tmp:
        sent = []
        kwargs = {"state_path": f"{tmp}/state.json", "now": NOW,
                  "sender": lambda text: sent.append(text) or True,
                  "logger": lambda _text: None}
        first = kick_audit.evaluate([], **kwargs)
        second = kick_audit.evaluate([], **kwargs)
        assert first["alerted"] is True and first["state_changed"] is True
        assert second["alerted"] is False and second["state_changed"] is False
        assert len(sent) == 1


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


def test_fetch_paginates_and_partial_failure_is_unknown():
    page = [_run("schedule", "github-actions[bot]") for _ in range(100)]
    calls = []
    def opener(req, timeout=0):
        calls.append(req)
        if len(calls) == 1:
            return _Resp(json.dumps({"workflow_runs": page}).encode())
        raise OSError("page 2 down")
    assert kick_audit.fetch_runs(token="secret-token", repository="o/r",
                                 now=NOW, opener=opener) is None
    assert len(calls) == 2 and "per_page=100" in calls[0].full_url
    assert "secret-token" not in calls[0].full_url


def test_workflow_integrates_audit_without_new_cron():
    text = Path(".github/workflows/watchdog.yml").read_text(encoding="utf-8")
    assert "python3 scripts/kick_audit.py" in text
    assert "continue-on-error: true" in text
    assert "actions/cache/restore@v4" in text and "actions/cache/save@v4" in text
    assert Path(".github/workflows").joinpath("kick_audit.yml").exists() is False


def main():
    test_external_zero_schedule_normal_alerts_once()
    test_schedule_zero_external_normal_alerts_once()
    test_both_zero_is_single_p0()
    test_both_normal_is_silent()
    test_api_failure_is_unknown_and_never_alerts()
    test_same_verdict_is_latched_once_per_day()
    test_fetch_paginates_and_partial_failure_is_unknown()
    test_workflow_integrates_audit_without_new_cron()
    print("kick audit 8/8 PASS")


if __name__ == "__main__":
    main()
