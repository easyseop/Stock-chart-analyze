#!/usr/bin/env python3
"""Audit independent GitHub workflow kick layers without triggering anything.

The audit is deliberately read-only toward GitHub: API failure is unknown, never
"zero runs". Telegram alerts are latched once per UTC day and verdict.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

WINDOW_HOURS = 24
PER_PAGE = 100
MAX_PAGES = 5
BOT_ACTOR = "github-actions[bot]"


def _parse_time(value: object) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_runs(*, token: str, repository: str, workflow: str = "daily.yml",
               now: datetime.datetime | None = None,
               opener=urllib.request.urlopen) -> list[dict] | None:
    """Return a complete recent-run list, or None on any API/contract failure."""
    if not token or not repository or "/" not in repository:
        return None
    current = now or datetime.datetime.now(datetime.timezone.utc)
    current = current.astimezone(datetime.timezone.utc)
    since = current - datetime.timedelta(hours=WINDOW_HOURS)
    created = ">=" + since.isoformat().replace("+00:00", "Z")
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"per_page": PER_PAGE, "page": page, "created": created})
        url = (f"https://api.github.com/repos/{repository}/actions/workflows/"
               f"{urllib.parse.quote(workflow, safe='')}/runs?{query}")
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stock-kick-audit/1",
        })
        try:
            with opener(req, timeout=15) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            return None
        page_rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
            return None
        for row in page_rows:
            created_at = _parse_time(row.get("created_at"))
            if created_at is not None and since <= created_at <= current:
                rows.append(row)
        if len(page_rows) < PER_PAGE:
            return rows
    # Reaching the page ceiling means the audit did not prove completeness.
    return None


def classify(runs: list[dict]) -> dict:
    external = 0
    scheduled = 0
    bot_dispatch = 0
    for row in runs:
        event = str(row.get("event") or "")
        actor = row.get("triggering_actor")
        login = str(actor.get("login") or "") if isinstance(actor, dict) else ""
        if event == "schedule":
            scheduled += 1
        elif event == "workflow_dispatch" and login == BOT_ACTOR:
            bot_dispatch += 1
        elif event == "workflow_dispatch" and login:
            external += 1
    return {"external": external, "schedule": scheduled,
            "bot_dispatch": bot_dispatch,
            "internal": scheduled + bot_dispatch}


def _verdict(counts: dict) -> tuple[str, str] | None:
    external = int(counts.get("external") or 0)
    scheduled = int(counts.get("schedule") or 0)
    if external == 0 and scheduled == 0:
        return ("all_layers_zero",
                "🚨 P0 외부 킥·GitHub 크론 모두 24시간 무발사 — 자동매매 갱신 정지 임박")
    if external == 0:
        return ("external_zero",
                "🚨 외부 킥 계층 24시간 무발사 — PAT 만료/크론 중단 의심. GitHub 크론만 남음")
    if scheduled == 0:
        return ("schedule_zero",
                "⚠️ GitHub 크론 24시간 무발화 — 외부 킥만 남음")
    return None


def _load_latch(path: str, day: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as fp:
            raw = json.load(fp)
        if not isinstance(raw, dict) or raw.get("day") != day or not isinstance(raw.get("sent"), list):
            return set()
        return {str(item) for item in raw["sent"] if str(item)}
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return set()


def _save_latch(path: str, day: str, sent: set[str]) -> bool:
    parent = os.path.dirname(path) or "."
    tmp = ""
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="kick-audit.", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump({"day": day, "sent": sorted(sent)}, fp,
                      ensure_ascii=False, separators=(",", ":"))
            fp.flush(); os.fsync(fp.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, path)
        return True
    except OSError:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass
        return False


def _telegram(text: str, *, token: str, chat_id: str,
              opener=urllib.request.urlopen) -> bool:
    if not token or not chat_id:
        return False
    body = json.dumps({"chat_id": chat_id, "text": text},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=15) as response:
            payload = json.load(response)
        return isinstance(payload, dict) and payload.get("ok") is True
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def evaluate(runs: list[dict] | None, *, state_path: str,
             now: datetime.datetime | None = None, sender=None,
             logger=print) -> dict:
    if runs is None:
        logger("kick-audit: GitHub API 조회 실패 — 무발사로 판정하지 않음")
        return {"status": "unknown", "alerted": False, "state_changed": False}
    current = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        datetime.timezone.utc)
    counts = classify(runs)
    problem = _verdict(counts)
    logger("kick-audit: "
           f"external={counts['external']} schedule={counts['schedule']} "
           f"bot_dispatch={counts['bot_dispatch']}")
    if problem is None:
        return {"status": "healthy", "alerted": False,
                "state_changed": False, **counts}
    key, message = problem
    day = current.date().isoformat()
    sent = _load_latch(state_path, day)
    if key in sent:
        return {"status": key, "alerted": False,
                "state_changed": False, **counts}
    delivered = bool(sender(message)) if sender is not None else False
    changed = False
    if delivered:
        sent.add(key)
        changed = _save_latch(state_path, day, sent)
    return {"status": key, "alerted": delivered,
            "state_changed": changed, **counts}


def _set_output(changed: bool) -> None:
    path = os.environ.get("GITHUB_OUTPUT") or ""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(f"state_changed={'true' if changed else 'false'}\n")
    except OSError:
        pass


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or ""
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    runs = fetch_runs(token=token, repository=repository)
    state_path = os.environ.get("KICK_AUDIT_STATE_PATH", ".kick-audit/state.json")
    result = evaluate(
        runs, state_path=state_path,
        sender=lambda text: _telegram(
            text, token=os.environ.get("TELEGRAM_BOT_TOKEN") or "",
            chat_id=os.environ.get("TELEGRAM_CHAT_ID") or ""))
    _set_output(bool(result.get("state_changed")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
