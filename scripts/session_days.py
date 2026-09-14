"""거래 세션 날짜 — 사후 분석은 KST 달력일이 아니라 **시장의 거래일**로 짝을 짓는다.

실측 2026-09-14: RZLV 09-01 03:41 KST 진입(= 08-31 미 동부 세션)을 KST 날짜
'2026-09-01'로 야후 일봉에 대응시키면 **다음 세션**(09-01 ET, 손절 후 종가
2.39)을 진입 봉으로 읽는다. 그 결과 "200일선 −11.5% 아래 진입"이 나왔지만
실제 진입 세션(08-31 ET) 종가 2.89는 200일선 2.71 **위**였다. 브레인 판정
(위)이 맞고 검정 도구가 틀렸다. 미국 22:30~05:00 KST 세션은 자정을 걸치므로
자정 이후 진입은 전부 이 오독에 걸린다.

여기서는 원장 주문의 제출 시각(epoch)을 시장 시간대로 바꿔 거래일을 얻는다.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from bot import ledger

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def zone_of(code: str, market: str | None = None) -> ZoneInfo:
    code = str(code or "").upper()
    if str(market or "").upper() == "KR" or (code.isdigit() and len(code) == 6):
        return KST
    return ET


def session_day(ts: float, code: str, market: str | None = None) -> str:
    """epoch → 그 시장의 거래일 'YYYY-MM-DD'."""
    return datetime.datetime.fromtimestamp(float(ts), zone_of(code, market)).strftime(
        "%Y-%m-%d")


def iso_to_ts(value) -> float | None:
    """trade_history의 executed_at(ISO, 오프셋 포함) → epoch. 실패=None."""
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _latest(orders: list[dict], *, before_ts: float) -> dict | None:
    best = None
    for order in orders:
        try:
            ts = float(order.get("submitted_at") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or ts > before_ts:
            continue
        if best is None or ts >= float(best.get("submitted_at") or 0):
            best = order
    return best


def entry_order_for(code: str, sell_ts: float) -> dict | None:
    """매도 시각 이전의 **마지막 체결 매수** 주문(진입)."""
    buys = [o for o in ledger.orders_for(code, side="BUY")
            if int(o.get("filled") or 0) > 0]
    return _latest(buys, before_ts=sell_ts)


def exit_order_for(code: str, sell_ts: float) -> dict | None:
    """매도 실현 시각 이전의 마지막 매도 주문(청산). 저널 지연으로 실현 시각이
    제출보다 늦을 수 있어 '이전'으로 찾는다."""
    sells = [o for o in ledger.orders_for(code, side="SELL")
             if int(o.get("filled") or 0) > 0]
    return _latest(sells, before_ts=sell_ts)


def sessions_for_sell(row: dict) -> tuple[str | None, str | None, dict | None]:
    """trade_history 매도 행 → (진입 세션일, 청산 세션일, 진입 주문).

    진입 주문을 못 찾으면 (None, 청산일, None) — 추정하지 않는다.
    """
    code = str(row.get("code") or "").upper()
    market = row.get("market")
    sell_ts = iso_to_ts(row.get("executed_at"))
    if sell_ts is None:
        return None, None, None
    entry = entry_order_for(code, sell_ts)
    exit_order = exit_order_for(code, sell_ts)
    exit_ts = float(exit_order.get("submitted_at")) if exit_order else sell_ts
    d1 = session_day(exit_ts, code, market)
    if entry is None:
        return None, d1, None
    d0 = session_day(float(entry["submitted_at"]), code, market)
    return d0, d1, entry
