"""Oracle 독립 소형 신호 분석기.

최근 후보와 소수 순환 탐색 종목만 다시 계산한다. 웹 렌더·모의매매·KIS·주문은
호출하지 않는다. 결과는 기본 그림자 데이터이며 별도 게이트가 켜져야 매수루프가
장애 폴백으로 채택한다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import json
import os
import tempfile

import config
from bot import settings, signal_feed
from scanner import cache, data, gates, screener, universe
from scanner.analyze import analyze

DEFAULT_ROOT = "/var/lib/stock-oracle-brain"
DEFAULT_MAX_SYMBOLS = 40
DEFAULT_DISCOVERY_SYMBOLS = 4
POOL_MAX_AGE_MIN = signal_feed.LOCAL_BASIS_MAX_AGE_MIN
_GROUP_PRIORITY = {"now": 0, "shelf": 1, "watch": 2, "shelf_watch": 3}


def _path(env_name: str, filename: str) -> str:
    return os.environ.get(env_name, os.path.join(DEFAULT_ROOT, filename))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_min(value, *, now: datetime) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    age = (now - stamp).total_seconds() / 60
    if age < -signal_feed.FUTURE_SKEW_MIN:
        return None
    return max(0.0, age)


def _atomic_json(path: str, value: dict, mode: int = 0o644) -> None:
    """fsync 뒤 원자 교체해 독자가 반쪽 JSON을 보지 않게 한다."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".oracle-brain-", dir=parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fp:
            fd = -1
            json.dump(value, fp, ensure_ascii=False, separators=(",", ":"))
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


@contextmanager
def _single_instance(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _candidate_rows(signals: list[dict]) -> list[dict]:
    ranked = sorted(
        signals,
        key=lambda row: (
            _GROUP_PRIORITY.get(str(row.get("group")), 9),
            not bool(row.get("fresh")),
            -float(row.get("stage") or 0),
            -float(row.get("norm") or 0),
        ))
    out, seen = [], set()
    for row in ranked:
        code = str(row.get("code") or "").upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code,
            "name": str(row.get("name") or code),
            "ccy": "KRW" if row.get("ccy") == "KRW" else "USD",
            "from_group": str(
                row.get("group") or row.get("from_group") or ""),
        })
    return out


def _fresh_remote_basis(*, now: datetime) -> dict | None:
    valid = []
    for url in settings.SIGNALS_SOURCES:
        doc = signal_feed._fetch_url(url)
        checked = signal_feed._validated(doc, source="remote", now=now)
        if (checked is not None
                and float(checked["_age_min"]) <= POOL_MAX_AGE_MIN):
            valid.append(checked)
    valid.sort(key=lambda doc: float(doc["_age_min"]))
    return valid[0] if valid else None


def _candidate_pool(*, now: datetime, pool_path: str) -> tuple[list[dict], str]:
    """원격 장애 때도 24시간 이내 마지막 후보 사본으로 계속 분석한다."""
    remote = _fresh_remote_basis(now=now)
    if remote is not None:
        payload = {
            "version": 1,
            "saved_at": now.isoformat(),
            "basis_generated_at": str(remote["generated_at"]),
            "candidates": _candidate_rows(remote["signals"]),
        }
        _atomic_json(pool_path, payload)
        return payload["candidates"], payload["basis_generated_at"]
    cached = _read_json(pool_path) or {}
    age = _age_min(cached.get("basis_generated_at"), now=now)
    rows = cached.get("candidates")
    if age is None or age > POOL_MAX_AGE_MIN or not isinstance(rows, list):
        return [], ""
    return _candidate_rows(rows), str(cached["basis_generated_at"])


def _discovery_rows(*, count: int, state_path: str) -> list[dict]:
    """매회 소수 종목을 순환해 GitHub 없이도 느린 신규 발굴을 이어간다."""
    rows = [row for row in universe.load() if row.get("code")]
    if count <= 0 or not rows:
        return []
    state = _read_json(state_path) or {}
    try:
        cursor = max(0, int(state.get("cursor") or 0)) % len(rows)
    except (TypeError, ValueError):
        cursor = 0
    picked = [rows[(cursor + i) % len(rows)]
              for i in range(min(count, len(rows)))]
    _atomic_json(state_path, {
        "version": 1,
        "cursor": (cursor + len(picked)) % len(rows),
        "updated_at": _now().isoformat(),
    })
    return picked


def _targets(candidates: list[dict], discovery: list[dict],
             *, max_symbols: int, force: bool = False) -> list[dict]:
    open_markets = (
        {"USD", "KRW"} if force else
        {ccy for ccy in ("USD", "KRW") if settings.market_open(ccy)}
    )
    combined = (
        candidates
        + [{**row, "from_group": "independent-discovery"}
           for row in discovery]
        + [{**row, "from_group": "configured-watch"}
           for row in config.STOCKS if row.get("code")]
    )
    out, seen = [], set()
    for row in combined:
        code = str(row.get("code") or "").upper()
        ccy = "KRW" if row.get("ccy") == "KRW" else "USD"
        if not code or code in seen or ccy not in open_markets:
            continue
        seen.add(code)
        out.append({
            "code": code, "name": str(row.get("name") or code),
            "ccy": ccy, "from_group": row.get("from_group", ""),
        })
        if len(out) >= max_symbols:
            break
    return out


def run(*, force: bool = False, max_symbols: int | None = None,
        discovery_symbols: int | None = None,
        output_path: str | None = None, pool_path: str | None = None,
        state_path: str | None = None, lock_path: str | None = None) -> dict:
    """한 사이클을 실행한다. 주문·KIS 호출은 없다."""
    now = _now()
    if not force and not (
            settings.market_open("USD") or settings.market_open("KRW")):
        return {"ok": True, "status": "market-closed", "analyzed": 0}
    maximum = max(1, min(80, int(
        max_symbols if max_symbols is not None
        else os.environ.get("ORACLE_BRAIN_MAX_SYMBOLS", DEFAULT_MAX_SYMBOLS))))
    discovery_n = max(0, min(20, int(
        discovery_symbols if discovery_symbols is not None
        else os.environ.get(
            "ORACLE_BRAIN_DISCOVERY_SYMBOLS", DEFAULT_DISCOVERY_SYMBOLS))))
    output = output_path or _path("ORACLE_SIGNALS_PATH", "signals.json")
    pool = pool_path or _path("ORACLE_CANDIDATE_POOL_PATH", "candidates.json")
    state = state_path or _path("ORACLE_DISCOVERY_STATE_PATH", "discovery.json")
    lock = lock_path or (
        os.path.join(os.path.dirname(output), "brain.lock")
        if output_path else _path("ORACLE_BRAIN_LOCK_PATH", "brain.lock"))

    with _single_instance(lock) as acquired:
        if not acquired:
            return {"ok": True, "status": "already-running", "analyzed": 0}
        candidates, basis = _candidate_pool(now=now, pool_path=pool)
        discovery = _discovery_rows(count=discovery_n, state_path=state)
        targets = _targets(
            candidates, discovery, max_symbols=maximum, force=force)
        # discovery/configured-watch는 최근 GitHub 후보군을 보완할 뿐, 후보 기준
        # 자체를 만들 수 없다. 만료된 basis를 실행시각으로 바꾸면 24시간 안전
        # 게이트를 우회해 GitHub이 검증하지 않은 종목이 주문 입력이 될 수 있다.
        if not basis:
            return {
                "ok": False, "status": "no-safe-candidate-basis",
                "analyzed": 0,
            }
        if not targets:
            return {
                "ok": False, "status": "no-analysis-targets",
                "analyzed": 0,
            }

        benchmarks = {}
        for ccy in {row["ccy"] for row in targets}:
            try:
                benchmarks[ccy] = data.fetch_benchmark(ccy)
            except Exception:
                benchmarks[ccy] = None
        results, failed = [], []
        for meta in targets:
            try:
                cache.update(meta["code"])
                frames = cache.frames(meta["code"], refresh=False)
                results.append(analyze(
                    frames, meta, bench=benchmarks.get(meta["ccy"])))
            except Exception as exc:
                failed.append(f"{meta['code']}:{type(exc).__name__}")
        if not results:
            return {
                "ok": False, "status": "analysis-empty",
                "analyzed": 0, "failed": failed,
            }

        picks = screener._paper_picks(results)
        gates.audit(results, picks)
        payload = json.loads(screener._signals_json(results))
        payload.update({
            "source": "oracle-local-brain",
            "contract": signal_feed.CONTRACT,
            "lane": "oracle-shadow",
            "basis_generated_at": basis,
            "analyzed_symbols": len(results),
            "failed_symbols": len(failed),
            "activation": "external-fallback-gate",
        })
        _atomic_json(output, payload)
        return {
            "ok": True, "status": "written",
            "analyzed": len(results), "signals": len(payload["signals"]),
            "failed": failed, "output": output,
            "basis_generated_at": basis,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Oracle 독립 소형 신호 분석기(기본 그림자 모드)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-symbols", type=int)
    args = parser.parse_args()
    result = run(force=args.force, max_symbols=args.max_symbols)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
