#!/usr/bin/env python3
"""실제 체결된 매도가 `rejected·0체결`로 오종결된 주문의 회계 복구(주문 0건).

왜(실측 2026-08-26): PAAS 매도 5주(odno 41030)가 같은 세션에 @52.99 전량
체결됐는데, 11분 뒤 부재 증명이 돌 때 브로커 저널이 아직 그 체결을 보여주지
않아 `rejected·filled=0`으로 닫혔다. 그 결과 원장은 5주를 계속 보유 중이라고
믿고(유령 포지션), costbook은 33만원을 묶어두고, 보호원장은 있지도 않은
수량에 손절선을 걸고 있다.

`bot/accounting_recovery.py`는 반대 시나리오(매수 유실)만 다룬다. 매도 유실은
포지션을 **닫아야** 하므로 경로가 다르다.

설계 원칙:
  · 회계를 재구현하지 않는다 — 정상 경로인 `kis_accounting.sync_fill`을 그대로
    태운다. 복구가 평상시 회계와 갈라지면 그 자체가 새 버그다.
  · plan은 읽기 전용. apply는 증거를 **처음부터 다시** 수집한다(plan 재사용 금지).
  · 브로커 ccnl의 ODNO·수량이 원장과 일치할 때만 진행. 조회 실패는 부재가 아니다.
  · 브로커 현재 보유가 '이미 줄어 있음'을 함께 확인한다 — 아직 들고 있으면
    체결이 아니라 우리 판정이 맞았을 수 있으므로 거부한다.
  · sync_fill의 event_id가 멱등이라 중간 크래시 뒤 재실행이 안전하다.

주문·kill·env를 건드리지 않는다. 서비스 정지도 필요 없다.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (costbook, kis, kis_accounting, kis_positions,  # noqa: E402
                 kis_reconcile, ledger)

US_EXCGS = ("NASD", "NYSE", "AMEX")
KST = datetime.timezone(datetime.timedelta(hours=9))


class Refused(RuntimeError):
    """증거·원장 전제가 불충분해 복구를 거부했다."""


def _broker_fill(symbol: str, odno: str, trade_date: str) -> dict:
    """해당 거래일 ccnl에서 ODNO가 일치하는 **매도** 체결행 하나. 없으면 거부."""
    matches, failures = [], []
    for excg in US_EXCGS:
        raw = kis.fills(excg=excg, start=trade_date, end=trade_date)
        rows = kis_reconcile.trusted_response_rows(raw)
        if rows is None:
            failures.append(excg)
            continue
        for row in rows:
            if kis_reconcile.order_no_key(
                    row.get("odno") or row.get("ODNO")) != odno:
                continue
            if str(row.get("pdno") or "").upper() != symbol:
                continue
            matches.append(row)
    if failures:
        raise Refused(f"체결내역 조회 실패({','.join(failures)}) — 실패는 부재가 아니다")
    if len(matches) != 1:
        raise Refused(f"ODNO {odno} 체결행 {len(matches)}건 — 유일해야 진행")
    row = matches[0]
    if str(row.get("sll_buy_dvsn_cd") or "") not in ("01", "1"):
        raise Refused(f"매도 행이 아니다(sll_buy_dvsn_cd={row.get('sll_buy_dvsn_cd')!r})")
    try:
        qty = int(float(row.get("ft_ccld_qty")))
        price = float(row.get("ft_ccld_unpr3"))
    except (TypeError, ValueError):
        raise Refused("체결 수량·단가를 읽을 수 없다")
    if qty <= 0 or price <= 0:
        raise Refused(f"체결 수량/단가 무효(qty={qty} price={price})")
    return {"qty": qty, "price": price, "odno": odno}


def _broker_holding(symbol: str) -> int:
    """전 거래소 합산 보유. 하나라도 실패하면 거부(실패≠0주)."""
    total = 0
    for excg in US_EXCGS:
        held = kis.holdings("US", excg=excg)
        if held is None:
            raise Refused(f"잔고 조회 실패({excg}) — 실패는 0주가 아니다")
        total += int(held.get(symbol, 0) or 0)
    return total


def collect(key: str, *, trade_date: str) -> dict:
    if not ledger.ledger_healthy():
        raise Refused("주문 원장 손상")
    order = ledger.state_of(key)
    if not order:
        raise Refused(f"원장에 주문 없음: {key}")
    symbol = str(order.get("symbol") or "").upper()
    side = str(order.get("side") or "").upper()
    if side != "SELL":
        raise Refused(f"매도 주문이 아니다(side={side!r})")
    if order.get("state") != "rejected":
        raise Refused(f"거절 종결된 주문만 대상(state={order.get('state')!r})")
    if int(order.get("filled") or 0) != 0:
        raise Refused("이미 체결이 기록돼 있다 — 이 도구 대상 아님")
    odno = kis_reconcile.order_no_key(order.get("odno"))
    if not odno:
        raise Refused("원장에 ODNO가 없다 — 브로커 대조 불가")
    for field in ("pos_key", "fx", "sleeve"):
        if not order.get(field):
            raise Refused(f"체결 회계에 필요한 메타 누락: {field}")

    fill = _broker_fill(symbol, odno, trade_date)
    intended = int(order.get("intended") or 0)
    if fill["qty"] > intended:
        raise Refused(f"체결({fill['qty']})이 주문수량({intended})을 초과")
    held_now = _broker_holding(symbol)
    ledger_qty = int((kis_positions.load().get(symbol) or {}).get("qty") or 0)
    if held_now >= ledger_qty and ledger_qty > 0:
        raise Refused(
            f"브로커 보유({held_now})가 아직 원장({ledger_qty}) 이상 — "
            "매도가 반영되지 않았다. 우리 거절 판정이 맞을 수 있으므로 거부")

    return {
        "key": key, "symbol": symbol, "odno": odno,
        "trade_date": trade_date,
        "fill_qty": fill["qty"], "fill_price": fill["price"],
        "intended": intended,
        "pos_key": str(order.get("pos_key")), "sleeve": str(order.get("sleeve")),
        "fx": float(order.get("fx")),
        "ledger_position_qty": ledger_qty,
        "costbook_open_qty": costbook.open_qty(symbol),
        "broker_qty_now": held_now,
        "realized_day_kst": _day_kst(trade_date),
    }


def _day_kst(trade_date: str) -> str:
    """미 동부 거래일 → 실현손익이 귀속될 KST 일자(장 마감은 KST 익일 새벽)."""
    day = datetime.datetime.strptime(trade_date, "%Y%m%d")
    return (day + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def apply(key: str, *, trade_date: str, ack: str) -> dict:
    if not str(ack or "").strip():
        raise Refused("--apply에는 --ack 문자열이 필요")
    plan = collect(key, trade_date=trade_date)        # 증거 재수집(재사용 금지)
    ledger.record_operator_action(
        key, action="recover-missing-sell-intent", ack=ack,
        evidence={"symbol": plan["symbol"], "side": "SELL",
                  "market": "US", "kind": "missing-sell-accounting",
                  "filled": plan["fill_qty"], "state": "intent"})
    ledger.on_result(key, "filled", plan["fill_qty"],
                     fill_price=plan["fill_price"],
                     fill_price_source="broker-forensic",
                     open_order=False)
    ledger.record_reconcile_meta(
        key, reason="operator-missing-sell-recovery",
        meta={"source": "operator-missing-sell-recovery",
              "broker_reason": "운영자 확인: ccnl ODNO 일치 체결행 + 잔고 감소"})
    acct = kis_accounting.sync_fill(
        key, filled_qty=plan["fill_qty"], fill_price=plan["fill_price"],
        fill_price_source="broker-forensic",
        realized_day_kst=plan["realized_day_kst"])
    ledger.record_operator_action(
        key, action="recover-missing-sell", ack=ack,
        evidence={"symbol": plan["symbol"], "side": "SELL", "market": "US",
                  "kind": "missing-sell-accounting",
                  "filled": plan["fill_qty"],
                  "state": "filled" if acct.get("ok") else "accounting-failed"})
    return {**plan, "accounting": acct,
            "position_qty_after": int(
                (kis_positions.load().get(plan["symbol"]) or {}).get("qty") or 0),
            "costbook_open_after": costbook.open_qty(plan["symbol"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="체결됐으나 거절로 오종결된 매도의 회계 복구(주문 0건)")
    ap.add_argument("--key", required=True, help="원장 주문키")
    ap.add_argument("--trade-date", required=True, help="미 동부 거래일 YYYYMMDD")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="읽기 전용 미리보기")
    mode.add_argument("--apply", action="store_true", help="원장에 적용")
    ap.add_argument("--ack", default="", help="apply 운영자 승인 사유")
    args = ap.parse_args(argv)
    try:
        out = (apply(args.key, trade_date=args.trade_date, ack=args.ack)
               if args.apply else collect(args.key, trade_date=args.trade_date))
    except (Refused, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "refused": True,
                          "why": str(exc), "orders_sent": 0},
                         ensure_ascii=False, indent=1))
        return 2
    print(json.dumps({"ok": True, "orders_sent": 0, **out},
                     ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
