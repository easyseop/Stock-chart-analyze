"""눌림 지정가 주문 수명주기 — half 2차·pullback 전량 주문을 동일하게 관리.

half의 2차 주문은 1차 주문이 체결 확정된 뒤에만 전송한다. 같은 종목의 두 주문을
동시에 열어 대사가 모호해지는 것을 피하면서 autopaper와 같은 수량 의미론을 유지한다.
모든 계획과 주문 상태는 order_ledger.jsonl에 남아 재시작 후에도 이어진다.
"""
from __future__ import annotations

import datetime

from bot import envelope, kis, kis_buy, kis_orders, ledger, notify, settings

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


def _cancel_family_state(base: str) -> str:
    """취소 시도 묶음 상태: retry | accepted | uncertain.

    확정 거부만 새 시도를 허용한다. 접수 완료나 응답유실/전송중 시도가 있으면
    같은 원주문에 두 번째 취소 HTTP를 보내지 않는다.
    """
    attempts = [
        o for o in ledger.orders_for(key_prefix=base)
        if o["key"] == base or o["key"].startswith(base + "#")
    ]
    if any(o.get("state") == "filled" for o in attempts):
        return "accepted"
    if any(o.get("state") != "rejected" for o in attempts):
        return "uncertain"
    return "retry"


def _next_cancel_key(base: str) -> str:
    """확정 거부 뒤 재시도할 때마다 충돌하지 않는 취소 원장키."""
    return f"{base}#{ledger.attempts(base) + 1}"


def cancel_open_buys_for_protection(symbol: str) -> bool:
    """손절 전에 같은 종목의 미체결 BUY 잔량을 안전하게 없앤다.

    반환 True는 모든 BUY 계획/원주문이 브로커 조회로 종료 확인됐다는 뜻이다.
    취소 API의 성공 응답은 접수일 뿐이므로 그 사이클에는 False를 반환한다. 호출부는
    다음 사이클에서 취소 확인 후 실잔고를 다시 읽고, 확인된 수량만 보호 매도한다.
    ODNO가 없거나 조회가 모호하면 추측 취소/동시 매도를 하지 않고 수동 잠금한다.
    """
    symbol = str(symbol or "").upper()
    ready = True
    for o in ledger.open_orders(symbol, side="BUY"):
        key = str(o.get("key") or "")
        state = str(o.get("state") or "")
        if state == "planned":
            ledger.finish_plan(key, "cancelled", "손절 우선 — 미제출 BUY 계획 취소")
            continue
        if state == "partial" and o.get("open") is False:
            ledger.mark_cancelled(key, "손절 우선 — 부분체결 후 잔량 종료 확인")
            continue
        if state == "cancel_pending":
            if not _cancel_confirmed(o):
                ready = False
                continue
            cur = ledger.state_of(key) or {}
            if cur.get("state") != "filled":
                ledger.mark_cancelled(key, "손절 우선 — BUY 취소 확인")
            continue

        intended = int(o.get("intended") or 0)
        filled = int(o.get("filled") or 0)
        residual = max(0, intended - filled)
        odno = str(o.get("odno") or "")
        if residual <= 0:
            # 수량상 완료여도 대사 상태가 끝나지 않았다면 새 SELL을 내지 않는다.
            ready = False
            continue
        if not odno:
            ready = False
            notify.send(
                f"🚨 {symbol} 손절 대기 — 미체결 BUY 주문번호 불명({state}). "
                "대사/수동 확인 전 동시 매도 금지",
                critical=True)
            continue
        market = o.get("market") or kis.market_of_symbol(symbol)
        excg = o.get("excg") or ("KRX" if market == "KR"
                                  else kis.us_excg_of(symbol))
        cxl_base = key + ":protect-cxl"
        cxl_state = _cancel_family_state(cxl_base)
        if cxl_state == "accepted":
            # 취소 HTTP 성공 직후 프로세스가 죽어 원주문 전이를 못 남긴 경우 복구.
            ledger.on_result(key, "cancel_pending", filled, open_order=True)
            ready = False
            continue
        if cxl_state == "uncertain":
            ready = False                       # 응답유실/전송중 — 중복 취소 금지
            continue
        r = kis_orders.cancel_order(
            _next_cancel_key(cxl_base), symbol, odno, residual, excg=excg,
            orgno=str(o.get("orgno") or ""), market=market,
            attempt_group=cxl_base)
        if r.get("act") == "canceled":
            ledger.on_result(key, "cancel_pending", filled, open_order=True)
        else:
            notify.send(
                f"🚨 {symbol} 손절 전 BUY 취소 실패({r.get('act')}) — "
                "확인 전 보호 매도 보류",
                critical=True)
        ready = False                              # 취소 '접수' 당일은 미확정
    return ready and not ledger.open_orders(symbol, side="BUY")


def process(*, today: str | None = None, quote_fn=None) -> list[dict]:
    """계획 제출·손절 이탈·21일 만료를 처리한다. 판단 불가는 주문을 늘리지 않는다."""
    out: list[dict] = []
    pending = ledger.pending_orders()
    budget_state = None
    for o in pending:
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
            cxl_base = key + ":cxl"
            cxl_state = _cancel_family_state(cxl_base)
            if cxl_state == "accepted":
                ledger.on_result(
                    key, "cancel_pending", int(o.get("filled") or 0),
                    open_order=True)
                out.append({"key": key, "act": "cancel_pending", "why": why})
                continue
            if cxl_state == "uncertain":
                continue                        # 응답유실/전송중 — 중복 취소 금지
            r = kis_orders.cancel_order(
                _next_cancel_key(cxl_base), symbol, odno, residual, excg=excg,
                orgno=str(o.get("orgno") or ""), market=market,
                attempt_group=cxl_base)
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
        if budget_state is None:
            # 메인 매수루프와 같은 held+in-flight+planned 스냅샷을 사용한다. 별도
            # 빈 계좌/기본 A seed로 계획 주문을 사이징하던 우회 경로를 없앤다.
            from bot import kis_buyloop
            budget_state = kis_buyloop._broker_state(settings.FX_USDKRW)
        if budget_state is None:
            continue                              # 브로커 진실 불명 — 다음 사이클
        held, held_cost, reservations, _, held_sleeves = budget_state
        sleeve = str(o.get("sleeve") or "A")
        # 지금 제출하려는 계획은 이미 reservations에 잡혀 있다. 기존 예약을 한 번
        # 빼고 execute_entry의 worst-case 신규수량으로 교체해야 이중 차감이 없다.
        current_reservation = next(
            (r for r in reservations if r.get("key") == key), None)
        reservations[:] = [r for r in reservations if r.get("key") != key]
        open_positions, open_cost = kis_buyloop._partition(
            held_cost, reservations, sleeve, held_sleeves)
        total_open_cost = (sum(float(v) for v in held_cost.values())
                           + sum(float(r["cost"]) for r in reservations))
        total_held_cost = sum(float(v) for v in held_cost.values())
        sleeve_held_cost = sum(
            float(cost) for code, cost in held_cost.items()
            if held_sleeves.get(code, "A") == sleeve)
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
            seed_krw=envelope.sleeve_limit_krw(sleeve), sleeve=sleeve,
            open_positions=open_positions, open_cost_krw=open_cost,
            total_open_cost_krw=total_open_cost,
            held_cost_krw=sleeve_held_cost,
            total_held_cost_krw=total_held_cost,
            operating_limit_krw=envelope.operating_limit_krw(),
            limit_price=limit, qty_cap=int(o.get("intended") or 0),
            order_meta=meta)
        if d.ok:
            reserved_qty = int(d.planned_qty or d.qty)
            cost = reserved_qty * limit * (1.0 if market == "KR"
                                            else float(o.get("fx") or 0))
            reservations.append({
                "key": key, "symbol": symbol, "qty": reserved_qty,
                "cost": cost, "sleeve": sleeve})
        elif current_reservation:
            reservations.append(current_reservation)
        out.append({"key": key, "act": d.gate, "ok": d.ok, "qty": d.qty,
                    "why": d.why})
    return out
