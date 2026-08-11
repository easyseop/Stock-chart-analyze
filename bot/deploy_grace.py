"""자동배포 재시작 동안 watchdog 오인을 막는 짧은 TTL 마커.

마커가 조금이라도 의심스러우면 유예하지 않는다. 이 모듈은 주문이나
kill-switch를 변경하지 않으며, autodeploy가 쓰고 watchdog/ops가 읽기만 한다.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

DEFAULT_PATH = "/opt/stock/deploy_grace.json"
DEFAULT_TTL_S = 300.0
MAX_TTL_S = 600.0
MAX_FUTURE_S = 60.0


def path() -> str:
    return os.environ.get("DEPLOY_GRACE_PATH", DEFAULT_PATH)


def ttl_s() -> float:
    try:
        value = float(os.environ.get("DEPLOY_GRACE_S", DEFAULT_TTL_S))
    except (TypeError, ValueError):
        value = DEFAULT_TTL_S
    if not math.isfinite(value):
        value = DEFAULT_TTL_S
    return max(0.0, min(MAX_TTL_S, value))


def status(*, now: float | None = None) -> dict:
    stamp = time.time() if now is None else float(now)
    try:
        with open(path(), encoding="utf-8") as fp:
            raw = json.load(fp)
        ts = float(raw.get("ts"))
        sha = str(raw.get("sha") or "")
        if not math.isfinite(ts) or not sha:
            raise ValueError("invalid marker")
        age = stamp - ts
        return {"active": bool(-MAX_FUTURE_S <= age <= ttl_s()),
                "age_s": age, "sha": sha}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"active": False, "age_s": None, "sha": ""}


def active(*, now: float | None = None) -> bool:
    return bool(status(now=now)["active"])


def write_marker(sha: str, *, now: float | None = None) -> str:
    clean_sha = str(sha or "").strip()
    if not clean_sha or any(ch.isspace() for ch in clean_sha):
        raise ValueError("deploy sha required")
    target = path()
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"ts": time.time() if now is None else float(now),
                       "sha": clean_sha}, fp, separators=(",", ":"))
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        return target
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m bot.deploy_grace DEPLOY_SHA", file=sys.stderr)
        return 2
    write_marker(args[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
