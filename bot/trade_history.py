"""확정 체결 원장으로 만드는 읽기 전용 거래이력.

주문 판단이나 KIS 조회를 하지 않는다. 주문 원장·원가장부·보호 포지션 장부를
각각 공유 잠금으로 읽고 즉시 놓은 뒤, 공통 ``event_id``로 매도 체결을 결합한다.
계좌번호·주문번호·원장 키·파일 경로는 응답에 포함하지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from typing import Iterable

from bot import costbook, kis_positions, ledger

KST = timezone(timedelta(hours=9))


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@contextmanager
def _shared_lock(path: str):
    """기존 writer의 ``.lock``을 읽기 전용 fd로 공유 잠금한다.

    개인 웹은 systemd에서 저장소 쓰기가 차단돼 있다. 기존 모듈의 reader 잠금은
    잠금 파일을 ``O_RDWR|O_CREAT``로 열기 때문에 그 격리 안에서는 실패한다.
    여기서는 이미 writer가 만든 같은 파일을 ``O_RDONLY``로 열어 원장 본문과 잠금
    파일 모두 수정하지 않는다. 데이터 파일이 있는데 잠금 파일만 없으면 안전하게
    실패해 무잠금 스냅샷을 만들지 않는다.
    """
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_jsonl(path: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    corrupt = 0
    if not os.path.exists(path):
        return rows, corrupt
    try:
        with _shared_lock(path):
            with open(path, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        corrupt += 1
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
                    else:
                        corrupt += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        corrupt += 1
    return rows, corrupt


def _order_key(event_id: str) -> str:
    """``fill:<order-key>:SELL:<cumulative>``에서 내부 주문키만 복구."""
    if not event_id.startswith("fill:") or ":SELL:" not in event_id:
        return ""
    return event_id[5:].rsplit(":SELL:", 1)[0]


def _reason_kind(reason: str) -> str:
    text = str(reason or "")
    if "손절" in text:
        return "stop"
    if "익절" in text or "목표" in text or "VAH" in text:
        return "take_profit"
    if "타임스탑" in text:
        return "time_stop"
    if "트레일" in text:
        return "trail"
    return "other"


def _iso_kst(ts) -> str:
    number = _number(ts)
    if number is None or number <= 0:
        return ""
    return datetime.fromtimestamp(number, KST).isoformat(timespec="seconds")


def _position_events() -> tuple[list[dict], int]:
    return _read_jsonl(kis_positions.PATH)


def _cost_events() -> tuple[list[dict], int]:
    path = costbook._path()
    return _read_jsonl(path)


def _order_state() -> tuple[list[dict], list[int]]:
    """주문 원장도 기존 writer와 같은 lock을 쓰되 잠금 파일은 수정하지 않는다."""
    if not os.path.exists(ledger.LEDGER_PATH):
        return [], []
    with _shared_lock(ledger.LEDGER_PATH):
        folded, corrupt = ledger._fold_unlocked()
    return [{"key": key, **row} for key, row in folded.items()], corrupt


def _position_meta(orders: Iterable[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for order in orders:
        pos_key = str(order.get("pos_key") or "")
        if not pos_key:
            continue
        cur = out.setdefault(pos_key, {})
        for field in ("symbol", "name", "opened", "sleeve", "ccy", "market"):
            value = order.get(field)
            if value not in (None, ""):
                cur[field] = value
    return out


def _lot_for(lots: dict[str, dict], pos_key: str, code: str) -> tuple[str, dict] | None:
    if pos_key and pos_key in lots:
        return pos_key, lots[pos_key]
    matches = [
        (key, lot) for key, lot in lots.items()
        if lot.get("symbol") == code and int(lot.get("qty") or 0) > 0
    ]
    return matches[-1] if len(matches) == 1 else None


def _sale_rows(position_events: list[dict], cost_events: list[dict],
               orders: list[dict]) -> tuple[list[dict], int]:
    order_map = {str(row.get("key") or ""): row for row in orders}
    meta_by_pos = _position_meta(orders)
    close_by_event: dict[str, dict] = {}
    for event in cost_events:
        event_id = str(event.get("event_id") or "")
        if event.get("ev") == "close" and event_id:
            close_by_event.setdefault(event_id, event)

    lots: dict[str, dict] = {}
    active_by_code: dict[str, str] = {}
    seen_events: set[str] = set()
    sales: list[dict] = []
    incomplete = 0

    for event in position_events:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_events:
            continue
        if event_id:
            seen_events.add(event_id)
        ev = str(event.get("ev") or "")
        code = str(event.get("code") or "").upper()
        if not code:
            continue
        pos_key = str(event.get("pos_key") or active_by_code.get(code) or
                      f"legacy:{code}:{event.get('opened') or '?'}")

        if ev in ("open", "buy_fill"):
            qty = max(0, int(_number(event.get("qty")) or 0))
            price = _number(event.get("price") if ev == "buy_fill"
                            else event.get("entry"))
            if qty <= 0 or price is None or price <= 0:
                continue
            lot = lots.get(pos_key)
            if ev == "open" or lot is None:
                lot = {
                    "symbol": code, "qty": 0, "native_cost": 0.0,
                    "source": "legacy" if ev == "open" else "confirmed",
                }
                lots[pos_key] = lot
            old_qty = max(0, int(lot.get("qty") or 0))
            lot["native_cost"] = float(lot.get("native_cost") or 0) + price * qty
            lot["qty"] = old_qty + qty
            lot["source"] = (
                "confirmed" if ev == "buy_fill" and old_qty == 0
                else lot.get("source", "legacy"))
            for field in ("name", "opened", "sleeve", "ccy"):
                value = event.get(field)
                if value not in (None, ""):
                    lot[field] = value
            active_by_code[code] = pos_key
            continue

        if ev == "close":
            for key in [key for key, lot in lots.items()
                        if lot.get("symbol") == code]:
                lots.pop(key, None)
            active_by_code.pop(code, None)
            continue

        if ev != "sell_fill":
            continue
        match = _lot_for(lots, pos_key, code)
        exit_price = _number(event.get("price"))
        qty_requested = max(0, int(_number(event.get("qty")) or 0))
        if match is None or exit_price is None or exit_price <= 0 or qty_requested <= 0:
            incomplete += 1
            continue
        actual_key, lot = match
        before_qty = max(0, int(lot.get("qty") or 0))
        qty = min(before_qty, qty_requested)
        if qty <= 0:
            incomplete += 1
            continue
        entry_price = (
            float(lot.get("native_cost") or 0) / before_qty
            if before_qty > 0 else 0.0)
        cost_event = close_by_event.get(event_id) or {}
        order = order_map.get(_order_key(event_id), {})
        fallback_meta = meta_by_pos.get(actual_key, {})
        reason = str(order.get("reason") or "매도 체결")
        cost_closed = _number(cost_event.get("cost_closed_krw"))
        pnl_krw = _number(cost_event.get("realized_pnl_krw"))
        return_pct = (
            pnl_krw / cost_closed * 100
            if pnl_krw is not None and cost_closed is not None and cost_closed > 0
            else (exit_price / entry_price - 1) * 100 if entry_price > 0 else None)
        ccy = str(lot.get("ccy") or fallback_meta.get("ccy") or
                  ("KRW" if code.isdigit() and len(code) == 6 else "USD")).upper()
        timestamp = (cost_event.get("ts") or event.get("ts")
                     or order.get("submitted_at"))
        remaining = max(0, before_qty - qty)
        name = str(lot.get("name") or fallback_meta.get("name") or code)
        sleeve = str(lot.get("sleeve") or fallback_meta.get("sleeve") or "A").upper()
        sales.append({
            "executed_at": _iso_kst(timestamp),
            "day": str(cost_event.get("day_kst") or
                       (_iso_kst(timestamp)[:10] if timestamp else "")),
            "code": code,
            "name": name,
            "market": "KR" if ccy == "KRW" else "US",
            "ccy": ccy,
            "sleeve": "B" if sleeve == "B" else "A",
            "reason": reason,
            "reason_kind": _reason_kind(reason),
            "qty": qty,
            "entry_price": round(entry_price, 6) if entry_price > 0 else None,
            "exit_price": round(exit_price, 6),
            "price_pnl": round((exit_price - entry_price) * qty, 6)
            if entry_price > 0 else None,
            "realized_pnl_krw": round(pnl_krw, 2) if pnl_krw is not None else None,
            "return_pct": round(return_pct, 4) if return_pct is not None else None,
            "remaining_qty": remaining,
            "partial_exit": remaining > 0,
            "verified": bool(
                lot.get("source") == "confirmed"
                and cost_closed is not None and pnl_krw is not None),
        })
        ratio = qty / before_qty
        lot["native_cost"] = max(
            0.0, float(lot.get("native_cost") or 0) * (1 - ratio))
        lot["qty"] = remaining
        if remaining <= 0:
            lots.pop(actual_key, None)
            if active_by_code.get(code) == actual_key:
                active_by_code.pop(code, None)

    sales.sort(key=lambda row: row.get("executed_at") or "", reverse=True)
    return sales, incomplete


def snapshot(limit: int = 200) -> dict:
    """개인 대시보드에 필요한 최소 거래이력. 네트워크·KIS 호출은 0건."""
    limit = max(1, min(500, int(limit)))
    orders, order_corrupt = _order_state()
    if order_corrupt:
        return {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "available": False,
            "partial": True,
            "trades": [],
            "summary": {
                "sell_fills": 0, "wins": 0, "losses": 0,
                "win_rate": None, "realized_pnl_krw": None,
            },
            "message": "주문 원장 무결성 확인 전에는 거래이력을 표시하지 않습니다.",
        }

    position_events, position_corrupt = _position_events()
    cost_events, cost_corrupt = _cost_events()
    rows, incomplete = _sale_rows(position_events, cost_events, orders)
    visible = rows[:limit]
    exact = [row for row in rows if row["realized_pnl_krw"] is not None]
    wins = sum(1 for row in exact if row["realized_pnl_krw"] > 0)
    losses = sum(1 for row in exact if row["realized_pnl_krw"] < 0)
    decided = wins + losses
    partial = bool(position_corrupt or cost_corrupt or incomplete
                   or any(not row["verified"] for row in rows))
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "available": True,
        "partial": partial,
        "source": "confirmed-local-fill-journals",
        "summary": {
            "sell_fills": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / decided * 100, 2) if decided else None,
            "realized_pnl_krw": round(
                sum(float(row["realized_pnl_krw"]) for row in exact), 2),
        },
        "trades": visible,
        "message": (
            "확정 체결 이후의 로컬 원장 기록만 표시합니다. "
            "원장 도입 전 과거 거래는 추정하지 않습니다."),
    }
