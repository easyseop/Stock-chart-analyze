"""원가장부(IS3 원가축 + C 정산회계 최소구현) — lot 단위 cost_krw 회계.

봉투(envelope)의 분모·게이트가 요구하는 값을 공급한다:
  · bot_open_cost        = Σ(열린 lot cost_krw)      → 총량 게이트 분자
  · open_cost(symbol)    = 종목별 열린 원가           → symbol_cap 차감
  · totals()             = Σ매수원가·Σ매도실현액       → bot_cash 계산

규칙(IS3):
  · 매수 확정 체결 시 add_lot(cost_krw = fill_price × qty × fx + commission) —
    **fill 시점 fx로 원가 고정**(이후 환율은 평가액만 바꿈, footprint 불변).
  · 매도 확정 체결 시 close_lot(proceeds_krw = 실현액 − 수수료·세금) —
    cost 반환이 아니라 **proceeds 환입**(손실 후 과대계상 방지).
  · append-only JSONL(원장과 동일 철학) — 재시작 시 fold로 재구성, 크래시 안전.
  · 부분 청산은 lot 원가를 수량 비례로 차감.

호출 시점: 체결이 '확정'됐을 때만(ccnl 대사·reconcile HIGH·fill 폴링) —
ack(접수)에서 기록 금지(미체결 주문을 원가로 잡으면 예산 과소·이중계상 위험.
단, UNKNOWN 구간의 보수적 예약 차감은 envelope 호출부(X1)가 intended로 처리).
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import threading
import time
import datetime

BOOK_PATH = os.path.join(os.path.dirname(__file__), "costbook.jsonl")
_THREAD_LOCK = threading.RLock()


def _path() -> str:
    return os.environ.get("COSTBOOK_PATH", BOOK_PATH)


@contextmanager
def _file_lock(path: str, exclusive: bool):
    """원가장부 reader/writer의 호스트 프로세스 공용 잠금."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with _THREAD_LOCK:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _append(ev: dict) -> None:
    """한 이벤트를 잠금+O_APPEND+fsync로 durable하게 기록."""
    path = _path()
    ev.setdefault("ts", time.time())
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    payload = (json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")
    with _file_lock(path, True):
        existed = os.path.exists(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(path, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("costbook append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            dfd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)


def _day_kst(ts: float | None = None) -> str:
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(ts or time.time(), kst).date().isoformat()


def _fold_unlocked(path: str) -> dict:
    """잠금 보유 상태에서 열린 lot·현금흐름·손익을 재구성."""
    lots: dict = {}
    totals = {"buy_cost": 0.0, "sell_proceeds": 0.0}
    by_sleeve: dict[str, dict] = {}
    by_market_sleeve: dict[str, dict] = {}
    daily: dict[str, float] = {}
    event_results: dict[str, dict] = {}
    corrupt: list[int] = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    corrupt.append(lineno)
                    continue
                if not isinstance(ev, dict):
                    corrupt.append(lineno)
                    continue
                k = ev.get("key")
                event_id = str(ev.get("event_id") or "")
                if event_id and event_id in event_results:
                    continue                              # crash 재시도 멱등
                if ev.get("ev") == "add":
                    if not k:
                        corrupt.append(lineno)
                        continue
                    cur = lots.setdefault(k, {"symbol": ev.get("symbol"),
                                              "qty": 0, "cost_krw": 0.0,
                                              "sleeve": ev.get("sleeve", "A"),
                                              "fx": ev.get("fx", 1.0)})
                    cur["sleeve"] = ev.get("sleeve") or cur.get("sleeve", "A")
                    cur["fx"] = float(ev.get("fx") or cur.get("fx") or 1.0)
                    cur["qty"] += int(ev.get("qty", 0))
                    cur["cost_krw"] += float(ev.get("cost_krw", 0.0))
                    totals["buy_cost"] += float(ev.get("cost_krw", 0.0))
                    sl = cur["sleeve"]
                    sb = by_sleeve.setdefault(sl, {"buy_cost": 0.0,
                                                   "sell_proceeds": 0.0})
                    sb["buy_cost"] += float(ev.get("cost_krw", 0.0))
                    market = ("KR" if len(str(cur.get("symbol") or "")) == 6
                              and str(cur.get("symbol") or "")[:5].isdigit()
                              else "US")
                    mb = by_market_sleeve.setdefault(market, {}).setdefault(
                        sl, {"buy_cost": 0.0, "sell_proceeds": 0.0})
                    mb["buy_cost"] += float(ev.get("cost_krw", 0.0))
                    if event_id:
                        event_results[event_id] = {
                            "cost_krw": float(ev.get("cost_krw", 0.0))}
                elif ev.get("ev") == "close" and k in lots:
                    cur = lots[k]
                    q = min(int(ev.get("qty", 0)), cur["qty"])
                    cost_closed = 0.0
                    if cur["qty"] > 0:
                        cost_closed = cur["cost_krw"] * (q / cur["qty"])
                        cur["cost_krw"] -= cost_closed
                    cur["qty"] -= q
                    proceeds = float(ev.get("proceeds_krw", 0.0))
                    totals["sell_proceeds"] += proceeds
                    sl = ev.get("sleeve") or cur.get("sleeve", "A")
                    sb = by_sleeve.setdefault(sl, {"buy_cost": 0.0,
                                                   "sell_proceeds": 0.0})
                    sb["sell_proceeds"] += proceeds
                    market = ("KR" if len(str(cur.get("symbol") or "")) == 6
                              and str(cur.get("symbol") or "")[:5].isdigit()
                              else "US")
                    mb = by_market_sleeve.setdefault(market, {}).setdefault(
                        sl, {"buy_cost": 0.0, "sell_proceeds": 0.0})
                    mb["sell_proceeds"] += proceeds
                    pnl = float(ev.get("realized_pnl_krw",
                                       proceeds - cost_closed))
                    day = ev.get("day_kst") or _day_kst(float(ev.get("ts") or 0))
                    daily[day] = daily.get(day, 0.0) + pnl
                    if event_id:
                        event_results[event_id] = {"pnl": pnl}
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        corrupt.append(-1)
    return {"lots": lots, "totals": totals, "by_sleeve": by_sleeve,
            "by_market_sleeve": by_market_sleeve,
            "daily_realized": daily, "healthy": not corrupt,
            "corrupt_lines": corrupt, "event_results": event_results}


def _fold() -> dict:
    path = _path()
    with _file_lock(path, False):
        return _fold_unlocked(path)


def budget_snapshot() -> dict | None:
    """원자 총시드 게이트용 durable 원가 스냅샷. 손상/읽기오류는 None."""
    try:
        folded = _fold()
    except Exception:
        return None
    if not folded.get("healthy"):
        return None
    by_sleeve = {
        sleeve: sum(float(lot.get("cost_krw") or 0)
                    for lot in folded["lots"].values()
                    if int(lot.get("qty") or 0) > 0
                    and str(lot.get("sleeve") or "A").upper() == sleeve)
        for sleeve in ("A", "B")
    }
    return {"total": sum(by_sleeve.values()), "by_sleeve": by_sleeve}


def add_lot(pos_key: str, symbol: str, qty: int, fill_price: float,
            fx: float = 1.0, commission_krw: float = 0.0,
            sleeve: str = "A", event_id: str = "") -> float:
    """매수 확정 체결 기록. 반환: 이번 lot의 cost_krw(fx로 고정)."""
    cost = float(fill_price) * int(qty) * float(fx) + float(commission_krw)
    if event_id:
        previous = _fold().get("event_results", {}).get(event_id)
        if previous is not None:
            return float(previous.get("cost_krw") or 0)
    _append({"ev": "add", "key": pos_key, "symbol": symbol, "qty": int(qty),
             "cost_krw": cost, "fill_price": float(fill_price), "fx": float(fx),
             "commission_krw": float(commission_krw), "sleeve": sleeve,
             "event_id": str(event_id or "")})
    return cost


def close_lot(pos_key: str, qty: int, proceeds_krw: float,
              *, sleeve: str | None = None, day_kst: str | None = None,
              event_id: str = "") -> float:
    """매도 확정 체결 기록. 반환은 이번 체결의 실현손익(원)."""
    folded = _fold()
    if event_id:
        previous = folded.get("event_results", {}).get(event_id)
        if previous is not None:
            return float(previous.get("pnl") or 0)
    cur = folded["lots"].get(pos_key) or {"qty": 0, "cost_krw": 0.0,
                                          "sleeve": sleeve or "A"}
    q = min(max(0, int(qty)), int(cur.get("qty", 0)))
    cost_closed = (float(cur.get("cost_krw", 0.0)) * q / int(cur["qty"])
                   if int(cur.get("qty", 0)) > 0 else 0.0)
    pnl = float(proceeds_krw) - cost_closed
    _append({"ev": "close", "key": pos_key, "qty": int(qty),
             "proceeds_krw": float(proceeds_krw),
             "cost_closed_krw": cost_closed, "realized_pnl_krw": pnl,
             "sleeve": sleeve or cur.get("sleeve", "A"),
             "day_kst": day_kst or _day_kst(),
             "event_id": str(event_id or "")})
    return pnl


def open_cost_total(sleeve: str | None = None) -> float:
    return sum(l["cost_krw"] for l in _fold()["lots"].values()
               if l["qty"] > 0 and (sleeve is None or l.get("sleeve", "A") == sleeve))


def open_cost_symbol(symbol: str, sleeve: str | None = None) -> float:
    return sum(l["cost_krw"] for l in _fold()["lots"].values()
               if l["qty"] > 0 and l.get("symbol") == symbol
               and (sleeve is None or l.get("sleeve", "A") == sleeve))


def open_qty(symbol: str, sleeve: str | None = None) -> int:
    """봇 claim 수량(원장 관점) — IS5 비대칭 대사·매도 상한 입력."""
    return sum(l["qty"] for l in _fold()["lots"].values()
               if l.get("symbol") == symbol
               and (sleeve is None or l.get("sleeve", "A") == sleeve))


def totals(sleeve: str | None = None) -> dict:
    """{buy_cost, sell_proceeds} — envelope.bot_cash 입력."""
    f = _fold()
    if sleeve is None:
        return dict(f["totals"])
    return dict(f["by_sleeve"].get(
        sleeve, {"buy_cost": 0.0, "sell_proceeds": 0.0}))


def market_totals(market: str, sleeve: str | None = None) -> dict:
    """시장×전략 누적 매수/매도 현금흐름(KRW) — NAV/TWR 흐름 중립 계산용."""
    buckets = _fold()["by_market_sleeve"].get(str(market).upper(), {})
    if sleeve is not None:
        return dict(buckets.get(
            str(sleeve).upper(), {"buy_cost": 0.0, "sell_proceeds": 0.0}))
    return {
        "buy_cost": sum(float(v.get("buy_cost") or 0) for v in buckets.values()),
        "sell_proceeds": sum(
            float(v.get("sell_proceeds") or 0) for v in buckets.values()),
    }


def realized_on(day_kst: str | None = None) -> float:
    """KST 날짜의 실현손익. 일일 신규매수 서킷브레이커의 단일 입력."""
    return float(_fold()["daily_realized"].get(day_kst or _day_kst(), 0.0))
