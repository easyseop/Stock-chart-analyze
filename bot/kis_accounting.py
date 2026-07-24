"""KIS 체결 회계 배선 — 브로커가 확인한 체결만 costbook·포지션에 반영.

주문 접수(ack)는 체결이 아니다. 대사에서 누적 체결수량과 체결가가 확인되면 이 모듈이
원장 `accounted`와 비교해 증가분만 반영한다. 같은 대사를 반복해도 중복 lot·중복
실현손익이 생기지 않는다.
"""
from __future__ import annotations

from bot import costbook, kis_positions, ledger


def sync_fill(key: str, *, filled_qty: int | None = None,
              fill_price: float | None = None,
              commission_krw: float = 0.0,
              fill_price_source: str = "broker") -> dict:
    """확인된 누적 체결을 회계에 동기화. 반환 {ok, delta, why?, pnl?}.

    체결가가 없으면 추측해 장부를 오염시키지 않고 보류한다. 호출부는 다음 ccnl/잔고
    대사에서 다시 시도한다.
    """
    cur = ledger.state_of(key)
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
    if side == "BUY":
        stop = float(cur.get("stop") or 0)
        ccy = str(cur.get("ccy") or ("KRW" if market == "KR" else "USD"))
        if stop <= 0:
            return {"ok": False, "delta": 0, "why": "보호 손절선 메타 누락"}
        costbook.add_lot(pos_key, symbol, delta, px, fx=fx,
                         commission_krw=fee, sleeve=sleeve)
        kis_positions.apply_buy_fill(
            symbol, qty=delta, price=px, stop=stop, ccy=ccy,
            pos_key=pos_key, name=str(cur.get("name") or ""),
            opened=str(cur.get("opened") or ""), sleeve=sleeve,
            target=cur.get("target"))
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
                                 fx=fx, sleeve=sleeve)
        proceeds = px * delta * fx - fee
        pnl = costbook.close_lot(pos_key, delta, proceeds, sleeve=sleeve)
        kis_positions.apply_sell_fill(symbol, qty=delta, price=px, pos_key=pos_key)

    ledger.mark_accounted(key, target)
    return {"ok": True, "delta": delta, "price": px, "side": side,
            "source": fill_price_source, "pnl": pnl}
