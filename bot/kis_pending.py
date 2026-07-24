"""눌림 지정가 주문 수명주기 — half 2차·pullback 전량 주문을 동일하게 관리.

half의 2차 주문은 1차 주문이 체결 확정된 뒤에만 전송한다. 같은 종목의 두 주문을
동시에 열어 대사가 모호해지는 것을 피하면서 autopaper와 같은 수량 의미론을 유지한다.
모든 계획과 주문 상태는 order_ledger.jsonl에 남아 재시작 후에도 이어진다.
"""
from __future__ import annotations

import datetime

from bot import kis, kis_buy, kis_orders, ledger

PENDING_DAYS = 21
_KST = datetime.timezone(datetime.timedelta(hours=9))


def _day(v: str | None = None) -> datetime.date:
    return (datetime.date.fromisoformat(v) if v
            else datetime.datetime.now(_KST).date())


def create_half_plan(key: str, symbol: str, qty: int, *, parent_key: str,
                     limit: float, stop: float, market: str, excg: str,
                     fx: float, sleeve: str, meta: dict) -> None:
    """1차 주문 뒤에 보낼 2차 눌림 주문 의도를 원장에 영속한다."""
    if int(qty) < 1 or not (float(stop) < float(limit)):
        return
    ledger.record_plan(
        key, symbol, int(qty),
        meta={**meta, "pending": True, "parent_key": parent_key,
              "limit": float(limit), "stop": float(stop), "market": market,
              "excg": excg, "fx": float(fx), "sleeve": sleeve,
              "created_day": _day().isoformat(), "side": "BUY",
              "price": float(limit)})


def _age_days(o: dict, today: str | None = None) -> int:
    try:
        created = o.get("created_day")
        if created:
            return (_day(today) - _day(str(created))).days
        ts = float(o.get("created_at") or o.get("submitted_at") or 0)
        made = datetime.datetime.fromtimestamp(ts, _KST).date()
        return (_day(today) - made).days
    except Exception:
        return PENDING_DAYS


def _cancel_confirmed(o: dict) -> bool:
    """원주문이 브로커 미체결 목록에서 사라졌을 때만 취소 확정."""
    from bot import kis_reconcile
    market = o.get("market") or kis.market_of_symbol(o.get("symbol", ""))
    if market == "KR":
        nccs, ccnl = kis.domestic_open_orders(), kis.domestic_fills()
        if nccs is None or ccnl is None:
            return False
        rows = kis_reconcile.normalize_domestic_rows(nccs, ccnl)
    else:
        ex = o.get("excg") or "NASD"
        nccs, ccnl = kis.open_orders(excg=ex), kis.fills(excg=ex)
        if nccs is None or ccnl is None:
            return False
        rows = kis_reconcile.normalize_rows(nccs, ccnl)
    kis_reconcile.resolve_acks_from_rows(rows)      # 취소 중 체결을 먼저 회계
    odno = str(o.get("odno") or "")
    return bool(odno) and not any(
        str(r.get("odno") or "") == odno and r.get("open") for r in rows)


def process(*, today: str | None = None, quote_fn=None) -> list[dict]:
    """계획 제출·손절 이탈·21일 만료를 처리한다. 판단 불가는 주문을 늘리지 않는다."""
    out: list[dict] = []
    for o in ledger.pending_orders():
        key, symbol = o["key"], str(o.get("symbol") or "").upper()
        market = o.get("market") or kis.market_of_symbol(symbol)
        excg = o.get("excg") or ("KRX" if market == "KR" else kis.us_excg_of(symbol))
        quote = None
        try:
            quote = (quote_fn(symbol, market, excg) if quote_fn
                     else kis.last_price(symbol, market=market, excg=excg))
        except Exception:
            pass
        stop = float(o.get("stop") or 0)
        expired = _age_days(o, today) >= PENDING_DAYS
        broken = quote is not None and stop > 0 and float(quote) <= stop

        if o.get("state") == "cancel_pending":
            if _cancel_confirmed(o):
                cur = ledger.state_of(key) or {}
                if cur.get("state") != "filled":
                    ledger.mark_cancelled(key, "눌림 주문 취소 확인")
                out.append({"key": key,
                            "act": "filled" if cur.get("state") == "filled" else "cancelled",
                            "why": "브로커 취소/체결 확인"})
            continue
        if o.get("state") == "partial" and o.get("open") is False:
            ledger.mark_cancelled(key, "부분체결 후 원주문 종료")
            out.append({"key": key, "act": "cancelled",
                        "why": "부분체결 후 잔량 주문 종료"})
            continue

        if expired or broken:
            why = ("21일 만료" if expired else f"손절선 이탈({quote}≤{stop})")
            if o.get("state") == "planned":
                ledger.finish_plan(key, "expired" if expired else "cancelled", why)
                out.append({"key": key, "act": "expired" if expired else "cancelled",
                            "why": why})
                continue
            odno = str(o.get("odno") or "")
            residual = max(0, int(o.get("intended") or 0) - int(o.get("filled") or 0))
            if not odno or residual <= 0:
                continue
            r = kis_orders.cancel_order(
                key + ":cxl", symbol, odno, residual, excg=excg,
                orgno=str(o.get("orgno") or ""), market=market)
            if r.get("act") == "canceled":
                # 취소 API 성공은 접수일 수 있다. 미체결 목록에서 사라질 때까지 원주문을
                # in-flight로 유지해 새 주문·중복 매수를 막는다.
                ledger.on_result(key, "cancel_pending", int(o.get("filled") or 0),
                                 open_order=True)
            out.append({"key": key, "act": r.get("act"), "why": why})
            continue

        if o.get("state") != "planned":
            continue                              # 이미 브로커에 제출됨 — 체결 대사 대기
        parent = ledger.state_of(str(o.get("parent_key") or ""))
        if parent and parent.get("state") not in ("filled", "partial", "rejected",
                                                   "cancelled"):
            continue                              # 1차 주문 미확정 — 동시 주문 금지
        if parent and parent.get("state") == "partial" and parent.get("open") is not False:
            continue                              # 1차 잔량이 살아 있음 — 2차 동시 주문 금지
        if parent and int(parent.get("filled") or 0) <= 0:
            ledger.finish_plan(key, "rejected", "1차 주문 미체결")
            out.append({"key": key, "act": "rejected", "why": "1차 주문 미체결"})
            continue
        held = kis.holdings(market, excg=excg) if market == "US" else kis.holdings("KR")
        if held is None:
            continue                              # 잔고 불명 — 다음 사이클
        limit = float(o.get("limit") or 0)
        if not (stop < limit):
            ledger.finish_plan(key, "rejected", "limit/stop 무효")
            continue
        meta = {f: o.get(f) for f in
                ("pos_key", "sleeve", "ccy", "stop", "target", "name",
                 "opened", "tactic", "pending", "parent_key") if o.get(f) is not None}
        d = kis_buy.execute_entry(
            key, symbol, price_usd=limit, per_share_risk_usd=limit - stop,
            krw_per_usd=float(o.get("fx") or 0), excg=excg, market=market,
            reason="눌림 지정가", hldg_before=int(held.get(symbol, 0)),
            seed_krw=None, sleeve=str(o.get("sleeve") or "A"),
            limit_price=limit, qty_cap=int(o.get("intended") or 0),
            order_meta=meta)
        out.append({"key": key, "act": d.gate, "ok": d.ok, "qty": d.qty,
                    "why": d.why})
    return out
