"""KIS 체결 회계 배선 — 브로커가 확인한 체결만 costbook·포지션에 반영.

주문 접수(ack)는 체결이 아니다. 대사에서 누적 체결수량과 체결가가 확인되면 이 모듈이
원장 `accounted`와 비교해 증가분만 반영한다. 같은 대사를 반복해도 중복 lot·중복
실현손익이 생기지 않는다.
"""
from __future__ import annotations

import json
import os
import tempfile

from bot import costbook, kis_positions, ledger

WATCH_PATH = "bot/kis_accounting_watch.json"
DEFAULT_ALERT_CYCLES = 3


def _watch_load() -> dict:
    try:
        with open(WATCH_PATH, encoding="utf-8") as fp:
            raw = json.load(fp)
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _watch_save(state: dict) -> None:
    """가용성 감시 상태만 원자 교체한다. 주문·예산 판정에는 사용하지 않는다."""
    parent = os.path.dirname(WATCH_PATH) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kis-accounting-watch-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        payload = (json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        with os.fdopen(fd, "wb", closefd=True) as fp:
            fd = -1
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, WATCH_PATH)
        os.chmod(WATCH_PATH, 0o600)
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


def monitor_unaccounted_fills(*, alert_cycles: int | None = None) -> dict:
    """BUY 체결이 durable 회계로 넘어가지 못한 상태를 연속 사이클로 감시한다.

    안전 방향은 이미 원장의 예약 유지가 보장한다. 이 함수는 브로커가 실체결가를
    오래 주지 않아 예약이 계속 묶이는 가용성 문제만 알린다. 주문·취소·게이트를
    변경하지 않으며 buyloop 한 프로세스에서 사이클마다 호출한다.
    """
    if alert_cycles is None:
        try:
            alert_cycles = int(os.environ.get(
                "KIS_ACCOUNTING_ALERT_CYCLES", str(DEFAULT_ALERT_CYCLES)))
        except ValueError:
            alert_cycles = DEFAULT_ALERT_CYCLES
    alert_cycles = max(2, min(60, int(alert_cycles)))

    # 원장 flock은 여기서 읽고 즉시 놓는다. watch 파일/알림 중에는 보유하지 않아
    # ledger > {costbook, kis_positions} 잠금 계층과 네트워크 무잠금 규약을 지킨다.
    with ledger._file_lock(False):
        fold, corrupt = ledger._fold_unlocked()
    if corrupt:
        return {"ok": False, "pending": 0, "alerts": [],
                "why": "주문 원장 손상"}

    pending: dict[str, dict] = {}
    for key, cur in fold.items():
        if str(cur.get("side") or "").upper() != "BUY":
            continue
        filled = max(0, int(cur.get("filled") or 0))
        accounted = max(0, int(cur.get("accounted") or 0))
        if filled > accounted:
            pending[key] = {
                "symbol": str(cur.get("symbol") or "").upper(),
                "filled": filled,
                "accounted": accounted,
            }

    old = _watch_load().get("items") or {}
    items: dict[str, dict] = {}
    alerts: list[dict] = []
    due: list[tuple[str, dict, dict]] = []
    for key, cur in pending.items():
        prev = old.get(key) if isinstance(old, dict) else None
        same_gap = (
            isinstance(prev, dict)
            and int(prev.get("filled") or 0) == cur["filled"]
            and int(prev.get("accounted") or 0) == cur["accounted"]
        )
        count = int(prev.get("count") or 0) + 1 if same_gap else 1
        alerted = bool(prev.get("alerted")) if same_gap else False
        item = {**cur, "count": count, "alerted": alerted}
        if count >= alert_cycles and not alerted:
            due.append((key, cur, item))
        items[key] = item

    if due:
        labels = [
            f"{cur['symbol'] or key}({cur['filled']}/{cur['accounted']})"
            for key, cur, _item in due[:8]
        ]
        more = f" 외 {len(due) - 8}건" if len(due) > 8 else ""
        text = (
            f"🚨 KIS 체결 회계 지연 {len(due)}건 — "
            f"{', '.join(labels)}{more}. "
            f"{min(int(item['count']) for _key, _cur, item in due)}회 이상 연속. "
            "신규매수 예약은 계속 유지 중이며 브로커 체결가·원장 상태 수동 확인 필요"
        )
        try:
            from bot import notify
            sent = bool(notify.send(text, critical=True, category="trade"))
        except Exception:
            sent = False
        if sent:
            for key, cur, item in due:
                item["alerted"] = True
                alerts.append({
                    "key": key, **cur, "cycles": int(item["count"]),
                })

    _watch_save({"version": 1, "items": items})
    return {"ok": True, "pending": len(pending), "alerts": alerts}


def sync_fill(key: str, *, filled_qty: int | None = None,
              fill_price: float | None = None,
              commission_krw: float = 0.0,
              fill_price_source: str = "broker") -> dict:
    """동일 체결을 여러 프로세스가 동시에 회계하지 못하게 원장 flock으로 직렬화."""
    with ledger._file_lock(True):
        return _sync_fill_locked(
            key, filled_qty=filled_qty, fill_price=fill_price,
            commission_krw=commission_krw,
            fill_price_source=fill_price_source)


def _sync_fill_locked(key: str, *, filled_qty: int | None = None,
                      fill_price: float | None = None,
                      commission_krw: float = 0.0,
                      fill_price_source: str = "broker") -> dict:
    """확인된 누적 체결을 회계에 동기화. 반환 {ok, delta, why?, pnl?}.

    체결가가 없으면 추측해 장부를 오염시키지 않고 보류한다. 호출부는 다음 ccnl/잔고
    대사에서 다시 시도한다.
    """
    fold, corrupt = ledger._fold_unlocked()
    if corrupt:
        return {"ok": False, "delta": 0, "why": "주문 원장 손상"}
    cur = fold.get(key)
    if not cur:
        return {"ok": False, "delta": 0, "why": "원장 주문 없음"}
    target = int(cur.get("filled", 0) if filled_qty is None else filled_qty)
    accounted = int(cur.get("accounted", 0) or 0)
    delta = max(0, target - accounted)
    if delta <= 0:
        return {"ok": True, "delta": 0, "why": "이미 반영"}
    px = fill_price if fill_price is not None else cur.get("fill_price")
    try:
        px = float(px)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return {"ok": False, "delta": 0, "why": "실체결가 미확인"}

    symbol = str(cur.get("symbol") or "").upper()
    side = str(cur.get("side") or "").upper()
    pos_key = str(cur.get("pos_key") or "")
    sleeve = str(cur.get("sleeve") or "A")
    market = cur.get("market") or ("KR" if symbol.isdigit() and len(symbol) == 6 else "US")
    try:
        fx = 1.0 if market == "KR" else float(cur.get("fx") or 0)
    except (TypeError, ValueError):
        fx = 0.0
    if not symbol or side not in ("BUY", "SELL") or not pos_key or fx <= 0:
        return {"ok": False, "delta": 0,
                "why": "symbol/side/pos_key/fx 체결 메타 누락"}

    # 한 주문의 수수료는 누적 체결 중 이번 증가분 비율만 반영한다.
    total = max(1, target)
    fee = float(commission_krw) * (delta / total)
    fill_event = f"fill:{key}:{side}:{target}"
    if side == "BUY":
        stop = float(cur.get("stop") or 0)
        ccy = str(cur.get("ccy") or ("KRW" if market == "KR" else "USD"))
        if stop <= 0:
            return {"ok": False, "delta": 0, "why": "보호 손절선 메타 누락"}
        costbook.add_lot(pos_key, symbol, delta, px, fx=fx,
                         commission_krw=fee, sleeve=sleeve,
                         event_id=fill_event)
        kis_positions.apply_buy_fill(
            symbol, qty=delta, price=px, stop=stop, ccy=ccy,
            pos_key=pos_key, name=str(cur.get("name") or ""),
            opened=str(cur.get("opened") or ""), sleeve=sleeve,
            target=cur.get("target"), event_id=fill_event)
        pnl = None
    else:
        # 업그레이드 전에 ack 시점으로만 기록된 기존 포지션은 costbook lot이 없다.
        # 저장된 진입가로 보수적 1회 시딩해 실현손익을 0원가 이익으로 오계상하지 않는다.
        book_qty = costbook.open_qty(symbol, sleeve)
        if book_qty < delta:
            rec = kis_positions.load().get(symbol) or {}
            try:
                legacy_qty = int(rec.get("qty") or 0)
                legacy_entry = float(rec.get("entry") or 0)
            except (TypeError, ValueError):
                legacy_qty, legacy_entry = 0, 0.0
            missing = max(0, legacy_qty - book_qty)
            if missing > 0 and legacy_entry > 0:
                costbook.add_lot(pos_key, symbol, missing, legacy_entry,
                                 fx=fx, sleeve=sleeve,
                                 event_id=fill_event + ":legacy")
        proceeds = px * delta * fx - fee
        pnl = costbook.close_lot(
            pos_key, delta, proceeds, sleeve=sleeve, event_id=fill_event)
        kis_positions.apply_sell_fill(
            symbol, qty=delta, price=px, pos_key=pos_key,
            event_id=fill_event)

    # costbook의 fsync와 포지션 기록이 끝난 뒤에만 예약 해제 표식을 같은 원장
    # 임계구역에 남긴다. 다른 매수 프로세스는 이 순서 전체가 끝날 때까지 기다린다.
    ledger._append_unlocked({
        "ev": "accounted", "key": key,
        "accounted": max(0, int(target)),
    })
    return {"ok": True, "delta": delta, "price": px, "side": side,
            "source": fill_price_source, "pnl": pnl}
