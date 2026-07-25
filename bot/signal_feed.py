"""GitHub 주 신호와 Oracle 로컬 신호를 안전하게 선택하는 읽기 전용 경계.

기본값은 그림자 모드다. 로컬 분석기가 정상 파일을 만들어도
``ORACLE_SIGNAL_FALLBACK_ENABLED=1``을 명시하기 전에는 주문 입력으로 채택하지
않는다. 어느 소스든 시각·스키마·후보 기준이 불명확하면 신규매수를 fail-closed로
막는다. 이 모듈은 KIS·주문·원장을 import하거나 호출하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import urllib.request

from bot import settings

CONTRACT = "stock-scanner-v1"
LOCAL_PATH = os.environ.get(
    "ORACLE_SIGNALS_PATH", "/var/lib/stock-oracle-brain/signals.json")
REMOTE_MAX_AGE_MIN = 45.0
LOCAL_MAX_AGE_MIN = 12.0
LOCAL_BASIS_MAX_AGE_MIN = 24.0 * 60.0
FALLBACK_AFTER_MIN = 20.0
FUTURE_SKEW_MIN = 5.0
_SYMBOL = re.compile(r"^(?:[A-Z][A-Z0-9.-]{0,11}|[0-9]{6})$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age_minutes(value, *, now: datetime) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    age = (now - stamp).total_seconds() / 60.0
    if not math.isfinite(age) or age < -FUTURE_SKEW_MIN:
        return None
    return max(0.0, age)


def _validated(doc, *, source: str, now: datetime) -> dict | None:
    """공통 신호 계약을 검증하고 원본을 수정하지 않은 사본을 반환한다."""
    if not isinstance(doc, dict):
        return None
    age = _age_minutes(doc.get("generated_at"), now=now)
    signals = doc.get("signals")
    if age is None or not isinstance(signals, list):
        return None
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in signals:
        if not isinstance(row, dict):
            return None
        code = str(row.get("code") or "").strip().upper()
        identity = (code, str(row.get("group") or row.get("id") or ""))
        if not _SYMBOL.fullmatch(code) or identity in seen:
            return None
        seen.add(identity)
        clean.append({**row, "code": code})
    if source == "oracle":
        if (doc.get("source") != "oracle-local-brain"
                or doc.get("contract") != CONTRACT):
            return None
        basis_age = _age_minutes(doc.get("basis_generated_at"), now=now)
        if basis_age is None or basis_age > LOCAL_BASIS_MAX_AGE_MIN:
            return None
    return {**doc, "signals": clean, "_age_min": age}


def _read_local(path: str = LOCAL_PATH) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


def _fetch_url(url: str) -> dict | None:
    separator = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{separator}cb={os.getpid()}",
        headers={"User-Agent": "stock-oracle-signal-selector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            value = json.load(response)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def select(*, remote_docs: list[dict] | None = None,
           local_doc: dict | None = None,
           fallback_enabled: bool = False,
           now: datetime | None = None) -> dict:
    """가장 안전한 신호 하나를 선택한다.

    원격이 20분 안이면 언제나 원격을 우선한다. 그보다 늦었을 때만 명시적으로
    활성화된 Oracle 로컬 신호를 쓴다. 로컬이 없으면 45분까지는 원격을 유지하고,
    둘 다 한도를 넘으면 빈 신호를 반환한다.
    """
    current = now or _utcnow()
    remotes = [
        checked for checked in (
            _validated(doc, source="remote", now=current)
            for doc in (remote_docs or []))
        if checked is not None
    ]
    remotes.sort(key=lambda doc: float(doc["_age_min"]))
    remote = remotes[0] if remotes else None
    local = _validated(local_doc, source="oracle", now=current)

    if remote and float(remote["_age_min"]) <= FALLBACK_AFTER_MIN:
        chosen, source, why = remote, "github", "github-primary-fresh"
    elif (fallback_enabled and local
          and float(local["_age_min"]) <= LOCAL_MAX_AGE_MIN):
        chosen, source, why = local, "oracle", "github-delayed-local-fallback"
    elif remote and float(remote["_age_min"]) <= REMOTE_MAX_AGE_MIN:
        chosen, source, why = remote, "github", (
            "local-shadow-disabled" if not fallback_enabled
            else "local-unavailable-remote-within-hard-limit")
    else:
        return {
            "signals": [], "source": "none", "age_min": None,
            "why": "no-valid-fresh-signal-feed",
            "fallback_enabled": bool(fallback_enabled),
        }
    return {
        "signals": chosen["signals"],
        "source": source,
        "age_min": round(float(chosen["_age_min"]), 2),
        "generated_at": chosen.get("generated_at"),
        "why": why,
        "fallback_enabled": bool(fallback_enabled),
    }


def load_selected() -> dict:
    """설정된 원격 소스와 로컬 파일을 읽어 주문 루프용 선택 결과를 반환한다."""
    remotes: list[dict] = []
    for url in settings.SIGNALS_SOURCES:
        value = _fetch_url(url)
        if value is not None:
            remotes.append(value)
    enabled = os.environ.get("ORACLE_SIGNAL_FALLBACK_ENABLED", "0") == "1"
    return select(
        remote_docs=remotes,
        local_doc=_read_local(),
        fallback_enabled=enabled,
    )
