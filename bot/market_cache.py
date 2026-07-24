"""KIS 보유·시세의 프로세스 간 읽기 전용 공유 캐시.

왜:
  파수꾼은 장중 보유 잔고와 현재가를 이미 주기적으로 조회한다. 포트폴리오 웹이
  같은 KIS API를 다시 호출하면 모의계좌의 낮은 초당 한도에서 손절 주문과 경합한다.
  파수꾼이 본 값을 원자적으로 남기고 웹·성과 추적이 읽으면 추가 KIS 호출 없이
  준실시간 화면을 제공할 수 있다.

보안:
  파일은 기본 /tmp에 0600으로 저장하며 Git에 포함하지 않는다. API 키·계좌번호·
  토큰은 받지도 저장하지도 않는다. 보유 수량·금액은 Oracle 내부 파일에만 남는다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import tempfile
import threading
import time


_DEFAULT = os.path.join(tempfile.gettempdir(), "kis_market_snapshot.json")
_LOCAL_LOCK = threading.Lock()
_FIELDS = (
    "code", "name", "market", "ccy", "qty", "avg", "cur", "eval_amt",
    "buy_amt", "pl_amt", "pl_rt", "entry", "stop", "target", "sleeve",
    "opened",
)


def path() -> str:
    return os.environ.get("KIS_MARKET_CACHE_PATH", _DEFAULT)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_row(raw: dict, market: str, ts: float) -> dict:
    row = {key: raw.get(key) for key in _FIELDS if raw.get(key) is not None}
    row["code"] = str(row.get("code") or "").upper()
    row["name"] = str(row.get("name") or row["code"])
    row["market"] = market
    row["ccy"] = "KRW" if market == "KR" else "USD"
    row["qty"] = max(0, int(_f(row.get("qty"))))
    for key in ("avg", "cur", "eval_amt", "buy_amt", "pl_amt", "pl_rt",
                "entry", "stop", "target"):
        row[key] = _f(row.get(key))
    row["sleeve"] = "B" if str(row.get("sleeve")).upper() == "B" else "A"
    row["quote_ts"] = float(raw.get("quote_ts") or ts)
    return row


def _empty() -> dict:
    return {
        "version": 1,
        "generated_at": None,
        "market_updated_at": {},
        "positions": [],
        "quotes": {},
    }


def _read_unlocked() -> dict:
    try:
        with open(path(), encoding="utf-8") as fp:
            doc = json.load(fp)
        return doc if isinstance(doc, dict) else _empty()
    except Exception:
        return _empty()


def read() -> dict | None:
    """원자적 파일의 현재 상태. 파일 없음·손상은 None."""
    doc = _read_unlocked()
    return doc if doc.get("generated_at") else None


@contextmanager
def _file_lock():
    """다중 프로세스 writer를 호스트 단일 락으로 직렬화."""
    lock_path = path() + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fp = open(lock_path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        fp.close()


def _write_unlocked(doc: dict) -> None:
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(doc, fp, ensure_ascii=False, separators=(",", ":"))
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _append_quote(doc: dict, code: str, price: float, ts: float) -> None:
    if price <= 0:
        return
    limit = max(60, min(1800, int(os.environ.get(
        "KIS_QUOTE_HISTORY_POINTS", "900"))))
    quotes = doc.setdefault("quotes", {}).setdefault(code, [])
    point = [round(float(ts), 3), round(float(price), 6)]
    if quotes and ts - _f(quotes[-1][0]) < 2.0:
        quotes[-1] = point
    else:
        quotes.append(point)
    if len(quotes) > limit:
        del quotes[:-limit]


def update_market(market: str, rows: list[dict],
                  *, now: float | None = None) -> None:
    """한 시장의 완전한 보유 스냅샷을 교체하고 현재가 이력을 덧붙인다."""
    market = str(market).upper()
    if market not in ("KR", "US"):
        raise ValueError("market must be KR or US")
    ts = time.time() if now is None else float(now)
    cleaned = [_clean_row(row, market, ts) for row in rows
               if str(row.get("code") or "").strip()]
    with _LOCAL_LOCK, _file_lock():
        doc = _read_unlocked()
        old_codes = {
            str(row.get("code") or "").upper()
            for row in doc.get("positions", [])
            if row.get("market") == market
        }
        new_codes = {row["code"] for row in cleaned}
        kept = [row for row in doc.get("positions", [])
                if row.get("market") != market]
        doc["positions"] = kept + cleaned
        doc.setdefault("market_updated_at", {})[market] = ts
        doc["generated_at"] = _iso(ts)
        for row in cleaned:
            _append_quote(doc, row["code"], row["cur"], ts)
        for code in old_codes - new_codes:
            doc.setdefault("quotes", {}).pop(code, None)
        _write_unlocked(doc)


def update_quote(code: str, price: float, *, now: float | None = None) -> bool:
    """보유 종목의 최신가·평가손익과 짧은 장중 이력을 갱신."""
    symbol = str(code).strip().upper()
    px = _f(price)
    if not symbol or px <= 0:
        return False
    ts = time.time() if now is None else float(now)
    changed = False
    with _LOCAL_LOCK, _file_lock():
        doc = _read_unlocked()
        for row in doc.get("positions", []):
            if str(row.get("code") or "").upper() != symbol:
                continue
            row["cur"] = px
            row["quote_ts"] = ts
            qty, avg = max(0, int(_f(row.get("qty")))), _f(row.get("avg"))
            row["eval_amt"] = px * qty
            row["buy_amt"] = avg * qty if avg > 0 else _f(row.get("buy_amt"))
            row["pl_amt"] = (px - avg) * qty if avg > 0 else _f(row.get("pl_amt"))
            row["pl_rt"] = (px / avg - 1) * 100 if avg > 0 else 0.0
            changed = True
            break
        if not changed:
            return False
        _append_quote(doc, symbol, px, ts)
        doc["generated_at"] = _iso(ts)
        _write_unlocked(doc)
    return True


def _age(ts, now: float) -> float:
    return max(0.0, now - _f(ts))


def portfolio(*, open_max_age: float = 90.0,
              closed_max_age: float = 86400.0,
              market_open=None, now: float | None = None) -> dict | None:
    """웹에서 안전하게 쓸 완전 스냅샷.

    KR·US가 모두 한 번은 성공적으로 조회돼야 한다. 열린 시장의 캐시가 낡으면
    None으로 돌려 포트폴리오 서버가 자체 60초 폴백을 사용하게 한다.
    """
    doc = read()
    if not doc:
        return None
    ts_now = time.time() if now is None else float(now)
    stamps = doc.get("market_updated_at") or {}
    if not all(market in stamps for market in ("KR", "US")):
        return None
    market_open = market_open or (lambda _market: False)
    for market in ("KR", "US"):
        limit = open_max_age if market_open(market) else closed_max_age
        if _age(stamps.get(market), ts_now) > limit:
            return None
    rows = [dict(row) for row in doc.get("positions", [])]
    rows.sort(key=lambda row: (row.get("market") != "KR",
                               -_f(row.get("eval_amt"))))
    quote_times = [_f(row.get("quote_ts")) for row in rows
                   if _f(row.get("quote_ts")) > 0]
    oldest_quote = min(quote_times, default=0.0)
    return {
        "generated_at": doc.get("generated_at"),
        "positions": rows,
        "partial": False,
        "failed_markets": [],
        "read_only": True,
        "source": "sentinel_shared_cache",
        "price_age_seconds": (
            round(_age(oldest_quote, ts_now), 1) if oldest_quote else None),
    }


def positions_for_market(market: str, *, max_age: float = 90.0,
                         now: float | None = None) -> list[dict] | None:
    """성과 추적용 한 시장 스냅샷. 열린 시장만 호출하므로 짧은 신선도만 허용."""
    doc = read()
    if not doc:
        return None
    ts_now = time.time() if now is None else float(now)
    stamp = (doc.get("market_updated_at") or {}).get(str(market).upper())
    if stamp is None or _age(stamp, ts_now) > float(max_age):
        return None
    return [dict(row) for row in doc.get("positions", [])
            if row.get("market") == str(market).upper()]


def quote_history(code: str, *, limit: int = 900) -> dict:
    """브라우저 실시간 선 차트용 보유종목 가격 이력."""
    symbol = str(code).strip().upper()
    doc = read() or _empty()
    points = (doc.get("quotes") or {}).get(symbol, [])
    limit = max(10, min(1800, int(limit)))
    return {
        "code": symbol,
        "generated_at": doc.get("generated_at"),
        "points": [
            {"ts": _f(point[0]), "price": _f(point[1])}
            for point in points[-limit:]
            if isinstance(point, list) and len(point) >= 2
        ],
        "source": "sentinel_shared_cache",
        "read_only": True,
    }
