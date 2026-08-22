"""보호매도 차단·설명되지 않는 매도가능 고갈 감시(주문 변경 0).

이 모듈은 파수꾼이 이미 중복매도를 막기 위해 손절 판단을 건너뛰는 상태와,
브로커 총보유/매도가능/열린 SELL 사이의 설명되지 않는 차이만 관측한다.
조회 실패는 0건으로 바꾸지 않고 판정 전체를 보류한다. 알림 래치는 원자 파일로
영속하며 텔레그램 전송 성공 뒤에만 전이한다.
"""
from __future__ import annotations

import fcntl
import json
import os
import time

from bot import ledger

_US_EXCGS = ("NASD", "NYSE", "AMEX")
def _alert_after_s() -> int:
    try:
        value = int(os.environ.get("PROTECTION_BLOCKED_ALERT_S", "1800") or 1800)
    except (TypeError, ValueError):
        value = 1800
    return max(60, min(86400, value))


def _gap_interval_s() -> int:
    try:
        value = int(os.environ.get("SELLABLE_GAP_AUDIT_S", "600") or 600)
    except (TypeError, ValueError):
        value = 600
    return max(60, min(3600, value))


def _latch_path() -> str:
    return os.environ.get(
        "PROTECTION_ALERT_LATCH_PATH",
        os.path.join(os.path.dirname(ledger.LEDGER_PATH),
                     "protection_alerts.json"))


def _load_unlocked(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fp:
            raw = json.load(fp)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    counts: dict[str, dict] = {}
    raw_counts = raw.get("sellable_gap_counts", {})
    if isinstance(raw_counts, dict):
        for symbol, row in raw_counts.items():
            if not isinstance(row, dict):
                continue
            clean = str(symbol).upper()
            signature = str(row.get("signature") or "")
            try:
                count = int(row.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if clean and signature and count > 0:
                counts[clean] = {"signature": signature, "count": count}
    return {
        "blocked": {str(x).upper() for x in raw.get("blocked", []) if str(x)},
        "sellable_gap": {
            str(x).upper() for x in raw.get("sellable_gap", []) if str(x)},
        "sellable_gap_counts": counts,
    }


def _read_latches() -> dict:
    path = _latch_path()
    try:
        with open(path + ".lock", "a+", encoding="utf-8") as lock:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return _load_unlocked(path)
    except OSError:
        return {"blocked": set(), "sellable_gap": set(),
                "sellable_gap_counts": {}}


def _write_unlocked(path: str, state: dict) -> None:
    """잠금 보유자가 래치와 연속 관찰 카운터를 한 파일에 원자 저장한다."""
    parent = os.path.dirname(path) or "."
    payload = {
        "blocked": sorted(state.get("blocked", set())),
        "sellable_gap": sorted(state.get("sellable_gap", set())),
        "sellable_gap_counts": state.get("sellable_gap_counts", {}),
    }
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False,
                  separators=(",", ":"), sort_keys=True)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    dfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _update_latch(kind: str, *, add: set[str], remove: set[str]) -> bool:
    """한 범주의 최신 상태만 병합해 원자 저장한다. 실패면 래치 전이 0."""
    path = _latch_path()
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        with open(path + ".lock", "a+", encoding="utf-8") as lock:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = _load_unlocked(path)
            state[kind] = (state.get(kind, set()) | set(add)) - set(remove)
            _write_unlocked(path, state)
        return True
    except OSError:
        try:
            os.unlink(f"{path}.tmp.{os.getpid()}")
        except OSError:
            pass
        return False


def _gap_confirmations() -> int:
    try:
        value = int(os.environ.get("SELLABLE_GAP_CONFIRMATIONS", "2") or 2)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(10, value))


def _advance_gap_counts(observed: dict[str, str], *,
                        scope_markets: set[str]) -> dict[str, int] | None:
    """같은 갭의 연속 관찰 횟수를 영속한다. 저장 실패면 판정 전체를 보류한다.

    닫힌 시장은 이번 감사 범위가 아니므로 기존 카운터를 유지한다. 열린 시장에서
    갭이 사라지거나 서명이 바뀌면 각각 삭제/1회부터 다시 시작한다.
    """
    path = _latch_path()
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        with open(path + ".lock", "a+", encoding="utf-8") as lock:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = _load_unlocked(path)
            previous = state.get("sellable_gap_counts", {})
            current = {
                symbol: dict(row) for symbol, row in previous.items()
                if _market(symbol) not in scope_markets
            }
            for symbol, signature in observed.items():
                old = previous.get(symbol, {})
                count = (int(old.get("count") or 0) + 1
                         if old.get("signature") == signature else 1)
                current[symbol] = {"signature": signature, "count": count}
            state["sellable_gap_counts"] = current
            _write_unlocked(path, state)
            return {symbol: int(row["count"]) for symbol, row in current.items()}
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(f"{path}.tmp.{os.getpid()}")
        except OSError:
            pass
        return None


def _market(symbol: str) -> str:
    from bot import kis
    return kis.market_of_symbol(str(symbol).upper())


def _notify_transitions(kind: str, current: dict[str, str], *,
                        scope_markets: set[str], recovery_label: str) -> bool:
    """새 사고/해소를 1회씩 전송한다. 닫힌 시장 래치는 건드리지 않는다."""
    from bot import notify
    previous = _read_latches().get(kind, set())
    delivered: set[str] = set()
    recovered: set[str] = set()
    sent = False
    for symbol in sorted(set(current) - previous):
        if notify.send(current[symbol], critical=True, category="trade"):
            delivered.add(symbol)
            sent = True
    resolved = {
        symbol for symbol in previous - set(current)
        if _market(symbol) in scope_markets
    }
    for symbol in sorted(resolved):
        if notify.send(f"✅ {symbol} {recovery_label} 해소",
                       critical=True, category="trade"):
            recovered.add(symbol)
            sent = True
    if delivered or recovered:
        _update_latch(kind, add=delivered, remove=recovered)
    return sent


def audit_blocked_protection(held: dict, *, scope_markets: set[str],
                             now_ts: float | None = None) -> bool:
    """보유 종목의 오래된 열린 SELL/CANCEL 때문에 손절이 스킵되면 P0."""
    if not ledger.ledger_healthy():
        return False
    stamp = time.time() if now_ts is None else float(now_ts)
    threshold = _alert_after_s()
    held_symbols = {
        str(symbol).upper() for symbol, row in (held or {}).items()
        if isinstance(row, dict) and int(row.get("q") or 0) > 0
    }
    oldest: dict[str, float] = {}
    for order in ledger.open_orders():
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        if symbol not in held_symbols or side not in ("SELL", "CANCEL"):
            continue
        try:
            age = max(0.0, stamp - float(order.get("submitted_at") or 0))
        except (TypeError, ValueError):
            continue
        oldest[symbol] = max(oldest.get(symbol, 0.0), age)
    current = {
        symbol: (f"🚨 {symbol} 보호매도 판단 {max(1, int(age // 60))}분 차단 — "
                 "열린 SELL/CANCEL 대사 필요")
        for symbol, age in oldest.items() if age >= threshold
    }
    return _notify_transitions(
        "blocked", current, scope_markets=scope_markets,
        recovery_label="보호매도 판단 차단")


def _merge_quantities(dst: dict[str, int], src: dict) -> bool:
    try:
        for symbol, qty in src.items():
            clean = str(symbol).upper()
            value = int(qty)
            if not clean or value < 0:
                return False
            dst[clean] = dst.get(clean, 0) + value
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _remaining_sell(rows: list[dict]) -> dict[str, int] | None:
    """완전한 nccs 정규행에서 열린 SELL 잔량을 합산한다."""
    remaining: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        if not row.get("open"):
            continue
        symbol = str(row.get("pdno") or "").upper()
        side = str(row.get("side") or "").upper()
        try:
            ordered = int(row.get("ord_qty"))
            filled = int(row.get("filled"))
        except (TypeError, ValueError):
            return None
        if not symbol or side not in ("BUY", "SELL") \
                or ordered < 0 or filled < 0 or filled > ordered:
            return None
        if side == "SELL":
            remaining[symbol] = remaining.get(symbol, 0) + ordered - filled
    return remaining


def _collect_sellable_snapshot(scope_markets: set[str]) -> dict | None:
    """같은 완전 응답들의 총보유·매도가능·열린 SELL 잔량. 실패는 None."""
    from bot import kis, kis_reconcile
    total: dict[str, int] = {}
    sellable: dict[str, int] = {}
    open_sell: dict[str, int] = {}

    def add_quantities(result) -> bool:
        return (isinstance(result, dict)
                and isinstance(result.get("total"), dict)
                and isinstance(result.get("sellable"), dict)
                and _merge_quantities(total, result["total"])
                and _merge_quantities(sellable, result["sellable"]))

    def add_open(raw, *, domestic: bool) -> bool:
        rows = kis_reconcile.trusted_response_rows(raw, domestic=domestic)
        if rows is None:
            return False
        wrapped = ({"rt_cd": "0", "output1": rows} if domestic
                   else {"rt_cd": "0", "output": rows})
        normalized = (kis_reconcile.normalize_domestic_rows(wrapped, None)
                      if domestic else kis_reconcile.normalize_rows(wrapped, None))
        rem = _remaining_sell(normalized)
        return rem is not None and _merge_quantities(open_sell, rem)

    if "KR" in scope_markets:
        if not add_quantities(kis.holding_quantities("KR")):
            return None
        n_raw = kis.domestic_open_orders()
        if kis_reconcile.trusted_response_rows(n_raw, domestic=True) is None \
                and kis.IS_MOCK:
            n_raw = kis.domestic_unfilled_orders()
        if not add_open(n_raw, domestic=True):
            return None
    if "US" in scope_markets:
        for excg in _US_EXCGS:
            if not add_quantities(kis.holding_quantities("US", excg=excg)):
                return None
            if not add_open(kis.open_orders(excg=excg), domestic=False):
                return None
    return {"total": total, "sellable": sellable, "open_sell": open_sell}


def audit_sellable_gaps(held: dict, *, scope_markets: set[str],
                        snapshot: dict | None = None) -> bool:
    """열린 SELL로 설명되지 않는 매도가능 부족만 P0로 알린다."""
    proof = _collect_sellable_snapshot(scope_markets) if snapshot is None else snapshot
    if not isinstance(proof, dict):
        return False                              # 조회 실패 != 부재; 래치도 유지
    if any(not isinstance(proof.get(key), dict)
           for key in ("total", "sellable", "open_sell")):
        return False
    observed: dict[str, str] = {}
    signatures: dict[str, str] = {}
    try:
        for symbol, row in (held or {}).items():
            symbol = str(symbol).upper()
            if not isinstance(row, dict) or int(row.get("q") or 0) <= 0:
                continue
            total = int(proof["total"].get(symbol, 0))
            available = int(proof["sellable"].get(symbol, 0))
            explained = int(proof["open_sell"].get(symbol, 0))
            if min(total, available, explained) < 0 or available > total:
                return False
            unexplained = (total - available) - explained
            if total > 0 and unexplained > 0:
                ratio = 100.0 * unexplained / total
                observed[symbol] = (
                    f"🚨 {symbol} 설명되지 않는 매도가능 부족 {ratio:.1f}% — "
                    "브로커 예약/체결 상태 수동 확인")
                signatures[symbol] = f"{total}:{available}:{explained}"
    except (AttributeError, TypeError, ValueError):
        return False
    counts = _advance_gap_counts(signatures, scope_markets=scope_markets)
    if counts is None:
        return False                              # 상태 저장 실패 != 확인 완료
    previous = _read_latches().get("sellable_gap", set())
    threshold = _gap_confirmations()
    current = {
        symbol: message for symbol, message in observed.items()
        if counts.get(symbol, 0) >= threshold or symbol in previous
    }
    return _notify_transitions(
        "sellable_gap", current, scope_markets=scope_markets,
        recovery_label="매도가능 부족")


def check(held: dict, *, scope_markets: set[str],
          now_ts: float | None = None) -> bool:
    """파수꾼 사이클 배선. 원장만 읽는 F3은 매번 수행한다.

    KIS 6회 조회가 필요한 F4는 ops_status 주기 루프로 분리했다.
    """
    stamp = time.time() if now_ts is None else float(now_ts)
    return audit_blocked_protection(
        held, scope_markets=scope_markets, now_ts=stamp)
