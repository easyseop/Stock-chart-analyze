#!/usr/bin/env python3
"""브로커 체결이 원장 기록보다 많은 **닫힌 매도**의 회계 복구(주문 0건).

실측 2건. 둘 다 브로커 저널 지연이 뿌리다:

  PAAS  08-26 매도 5주(odno 41030) @52.99 전량 체결. 11분 뒤 부재 증명이 돌 때
        저널에 체결행이 없어 `rejected·filled=0`으로 닫혔다.
  TRUP  09-02 매도 7주(odno 41895) @28.08 전량 체결. 원장은 `partial 1/7 ·
        open=False`로 닫혀 6주가 미회계로 남았다.

둘 다 원장이 있지도 않은 수량을 보유 중이라 믿는다(유령 포지션) — costbook이
원가를 묶어두고, 보호원장은 없는 수량에 손절선을 건다.

기준은 상태 이름이 아니라 **닫혀 있는데 기록이 브로커보다 적은가**다. 열린
주문은 자동 대사의 몫이라 거부한다 — 끼어들면 이중 회계가 난다.

`bot/accounting_recovery.py`는 반대 시나리오(매수 유실)만 다룬다. 매도 유실은
포지션을 **닫아야** 하므로 경로가 다르다.

설계 원칙:
  · 회계를 재구현하지 않는다 — 정상 경로인 `kis_accounting.sync_fill`을 그대로
    태운다. 복구가 평상시 회계와 갈라지면 그 자체가 새 버그다.
  · plan은 읽기 전용. apply는 증거를 **처음부터 다시** 수집한다(plan 재사용 금지).
  · 브로커 ccnl의 ODNO가 원장과 일치하고 체결량이 기록을 넘을 때만 진행.
    조회 실패는 부재가 아니다.
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
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import (costbook, kis, kis_accounting, kis_positions,  # noqa: E402
                 kis_reconcile, ledger)

US_EXCGS = ("NASD", "NYSE", "AMEX")
KST = datetime.timezone(datetime.timedelta(hours=9))
ET = ZoneInfo("America/New_York")


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
    # 대상은 '거절'만이 아니다. 실측 2026-09-02 TRUP: 7주 전량 체결(odno 41895
    #   @28.08)인데 원장이 `partial 1/7 · open=False`로 닫혀 6주가 미회계로
    #   남았다. 같은 뿌리(브로커 저널 지연)의 다른 얼굴이라 같은 도구로 다룬다.
    #   기준은 상태 이름이 아니라 **닫혀 있는데 기록이 브로커보다 적은가**다.
    if ledger.fold_is_open(order):
        raise Refused(
            f"아직 열린 주문(state={order.get('state')!r}) — 자동 대사가 처리한다. "
            "끼어들면 이중 회계가 난다")
    recorded = int(order.get("filled") or 0)
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
    if fill["qty"] <= recorded:
        raise Refused(
            f"브로커 체결({fill['qty']})이 원장 기록({recorded})을 넘지 않는다 — "
            "복구할 것이 없다")
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
        "recorded_filled": recorded, "missing_qty": fill["qty"] - recorded,
        "intended": intended,
        "pos_key": str(order.get("pos_key")), "sleeve": str(order.get("sleeve")),
        "fx": float(order.get("fx")),
        "ledger_position_qty": ledger_qty,
        "costbook_open_qty": costbook.open_qty(symbol),
        "broker_qty_now": held_now,
        "realized_day_kst": _realized_day_kst(order, trade_date),
    }


def _realized_day_kst(order: dict, trade_date: str) -> str:
    """실현손익이 귀속될 KST 일자.

    첫 판본은 `거래일 + 1일`로 계산했는데 틀렸다. 미장은 KST 22:30~05:00에
    걸쳐 있어 **11:00 ET가 경계**다 — 그 전 체결은 KST 같은 날, 그 뒤는 익일이다.
    09:35 ET 체결을 익일로 밀면 거래가 달을 넘어가 월별 결산이 어긋난다.

    ccnl 행에는 체결 시각이 없으므로 주문의 `submitted_at`(정확한 epoch)에서
    유도한다. 체결은 제출 몇 분 뒤이므로 날짜 판정에는 충분하다. 다만 제출
    세션과 체결 세션이 다르면 시각을 유추할 수 없어 거부한다 — 결산 숫자를
    추측으로 채우지 않는다.
    """
    stamp = float(order.get("submitted_at") or 0)
    if stamp <= 0:
        raise Refused("주문 제출 시각이 없어 실현일을 판정할 수 없다")
    submitted_et = datetime.datetime.fromtimestamp(stamp, ET).strftime("%Y%m%d")
    if submitted_et != trade_date:
        raise Refused(
            f"제출 거래일({submitted_et})과 체결 거래일({trade_date})이 다르다 — "
            "실현일 귀속을 자동 판단할 수 없다(월별 결산에 영향)")
    return datetime.datetime.fromtimestamp(stamp, KST).strftime("%Y-%m-%d")


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
