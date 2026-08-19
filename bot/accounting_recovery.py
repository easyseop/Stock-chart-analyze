"""브로커 체결은 존재하지만 회계가 유실된 BUY의 forensic plan/apply 복구.

주문을 전혀 내지 않는다. plan과 apply 모두 KIS 체결·잔고를 새로 읽고, apply는
서비스 runtime mask·stale heartbeat·SHA 승인·미존재 백업 디렉터리를 요구한다.
append-only 세 원장은 공통 event_id로 멱등 복구하므로 중간 크래시 뒤 새 백업
디렉터리로 같은 plan을 재실행할 수 있다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time

from bot import costbook, kis, kis_positions, kis_reconcile, ledger, ownership
from bot import legacy_migration

PLAN_VERSION = 1
PARTIAL_EXIT_PLAN_VERSION = 2
PARTIAL_EXIT_SCENARIO = "buy-partial-sell-existing-accounting"
PLAN_MAX_AGE_S = 300
US_EXCGS = ("NASD", "NYSE", "AMEX")


class RecoveryRefused(RuntimeError):
    """증거·원장·운영 전제 하나라도 불충분해 복구를 거부했다."""


def _canonical(payload: dict) -> bytes:
    clean = {key: value for key, value in payload.items()
             if key != "plan_sha256"}
    return json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def plan_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _positive(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused(f"{label} 숫자 변환 실패") from exc
    if not math.isfinite(number) or number <= 0:
        raise RecoveryRefused(f"{label} 유한한 양수 필요")
    return number


def _paths() -> dict[str, str]:
    return {
        "order_ledger": os.path.abspath(ledger.LEDGER_PATH),
        "kis_positions": os.path.abspath(kis_positions.PATH),
        "costbook": os.path.abspath(costbook._path()),
    }


def _broker_fill(*, trade_date: str, odno: str, symbol: str,
                 qty: int, fill_price: float, side: str = "BUY") -> dict:
    expected_side = str(side or "").strip().upper()
    if expected_side not in {"BUY", "SELL"}:
        raise RecoveryRefused("KIS 체결 side 계약 불일치")
    matches: list[dict] = []
    for excg in US_EXCGS:
        response = kis.fills(excg=excg, start=trade_date, end=trade_date)
        if not isinstance(response, dict) or response.get("rt_cd") != "0":
            raise RecoveryRefused(f"KIS 체결조회 실패: {excg}")
        rows = kis_reconcile.normalize_rows(
            {"rt_cd": "0", "output": []}, response)
        for row in rows:
            if kis_reconcile.order_no_key(row.get("odno")) \
                    == kis_reconcile.order_no_key(odno):
                matches.append(row)
    if not matches:
        raise RecoveryRefused("KIS 체결조회에 지정 ODNO 부재")
    normalized = {
        (str(row.get("pdno") or "").upper(), str(row.get("side") or ""),
         int(row.get("filled") or 0), round(float(row.get("price") or 0), 6))
        for row in matches
    }
    if len(normalized) != 1:
        raise RecoveryRefused("거래소별 체결 증거 충돌")
    code, side, filled, price = next(iter(normalized))
    if code != symbol or side != expected_side or filled != qty \
            or abs(price - fill_price) > 0.005:
        raise RecoveryRefused(
            f"KIS 체결 증거 불일치({code}/{side}/{filled}/{price})")
    return {"symbol": code, "side": side, "qty": filled,
            "fill_price": price, "odno": str(odno),
            "trade_date": trade_date}


def _broker_position(symbol: str, qty: int) -> dict:
    matches: list[tuple[int, float]] = []
    for excg in US_EXCGS:
        rows = kis.positions_detail("US", excg=excg)
        if rows is None:
            raise RecoveryRefused(f"KIS 잔고조회 실패: {excg}")
        for row in rows:
            if str(row.get("code") or "").upper() != symbol:
                continue
            try:
                matches.append((int(row.get("qty") or 0),
                                round(float(row.get("avg") or 0), 6)))
            except (TypeError, ValueError) as exc:
                raise RecoveryRefused("KIS 잔고 수량/평단 형식 오류") from exc
    unique = set(matches)
    if len(unique) != 1:
        raise RecoveryRefused("거래소별 잔고 증거 부재/충돌")
    actual_qty, average = next(iter(unique))
    if actual_qty != qty or average <= 0:
        raise RecoveryRefused(
            f"KIS 잔고 불일치({actual_qty}주, 평단 {average})")
    return {"symbol": symbol, "qty": actual_qty, "avg": average}


def _validate_order(order: dict, spec: dict) -> None:
    if (str(order.get("symbol") or "").upper() != spec["symbol"]
            or str(order.get("side") or "").upper() != "BUY"
            or int(order.get("intended") or 0) != spec["qty"]
            or kis_reconcile.order_no_key(order.get("odno"))
            != kis_reconcile.order_no_key(spec["odno"])):
        raise RecoveryRefused("주문 원장 정체성/수량 불일치")
    state = str(order.get("state") or "")
    filled = int(order.get("filled") or 0)
    accounted = int(order.get("accounted") or 0)
    initial = (
        state == "rejected" and filled == 0 and accounted == 0
        and str(order.get("reconcile_reason") or "")
        in {"broker-closed-zero-fill", "zero-fill-balance-proof"}
    )
    progressing = (
        state == "filled" and filled == spec["qty"]
        and accounted in (0, spec["qty"])
        and bool(order.get("accounting_recovery_pending")
                 or order.get("accounting_recovery_complete"))
    )
    if not (initial or progressing):
        raise RecoveryRefused(
            f"주문 원장 예상 상태 아님({state}/{filled}/{accounted})")


def _validate_sell_order(order: dict, spec: dict) -> None:
    sale = spec["sell"]
    if (str(order.get("symbol") or "").upper() != spec["symbol"]
            or str(order.get("side") or "").upper() != "SELL"
            or int(order.get("intended") or 0) != int(sale["qty"])
            or kis_reconcile.order_no_key(order.get("odno"))
            != kis_reconcile.order_no_key(sale["odno"])
            or str(order.get("pos_key") or "") != spec["pos_key"]):
        raise RecoveryRefused("SELL 원장 정체성/수량 불일치")
    if (str(order.get("state") or "") != "filled"
            or int(order.get("filled") or 0) != int(sale["qty"])
            or int(order.get("accounted") or 0) != int(sale["qty"])):
        raise RecoveryRefused("SELL 체결/회계가 확정 상태 아님")


def _raw_costbook_events(event_ids: set[str]) -> dict[str, dict]:
    """forensic 증거용 원문 이벤트 조회. 손상·중복은 부분 신뢰하지 않는다."""
    path = costbook._path()
    found: dict[str, dict] = {}
    try:
        with costbook._file_lock(path, False):
            with open(path, encoding="utf-8") as fp:
                for lineno, line in enumerate(fp, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise RecoveryRefused(
                            f"costbook 원문 손상({lineno})") from exc
                    if not isinstance(event, dict):
                        raise RecoveryRefused(f"costbook 원문 손상({lineno})")
                    event_id = str(event.get("event_id") or "")
                    if event_id not in event_ids:
                        continue
                    if event_id in found:
                        raise RecoveryRefused(
                            f"costbook event_id 중복({event_id})")
                    found[event_id] = event
    except FileNotFoundError as exc:
        raise RecoveryRefused("costbook 원문 부재") from exc
    except (OSError, UnicodeError) as exc:
        raise RecoveryRefused("costbook 원문 읽기 실패") from exc
    return found


def _close_day(event: dict) -> str:
    return str(event.get("day_kst") or "")


def _inspect_partial_exit_costbook(spec: dict) -> dict:
    """이미 durable한 legacy seed→부분매도를 검증하고 절대 재기입하지 않는다."""
    sale = spec["sell"]
    seed_id = str(sale["seed_event_id"])
    close_id = str(sale["event_id"])
    events = _raw_costbook_events({seed_id, close_id})
    if set(events) != {seed_id, close_id}:
        raise RecoveryRefused("기존 BUY seed/SELL costbook 증거 부재")
    seed = events[seed_id]
    close = events[close_id]
    q = int(spec["qty"])
    sold = int(sale["qty"])
    expected_raw_cost = float(spec["fill_price"]) * q * float(spec["fx"])
    seed_cost = float(seed.get("cost_krw") or 0)
    if (seed.get("ev") != "add"
            or str(seed.get("key") or "") != spec["pos_key"]
            or str(seed.get("symbol") or "").upper() != spec["symbol"]
            or int(seed.get("qty") or 0) != q
            or abs(float(seed.get("fill_price") or 0)
                   - float(spec["fill_price"])) > 0.005
            or abs(float(seed.get("fx") or 0) - float(spec["fx"])) > 1e-9
            or abs(seed_cost - expected_raw_cost) > 1e-6
            or abs(seed_cost - float(spec["cost_krw"])) >= 1.0):
        raise RecoveryRefused("기존 legacy BUY seed 경제값 불일치")
    expected_proceeds = float(sale["fill_price"]) * sold * float(spec["fx"])
    expected_cost_closed = seed_cost * sold / q
    expected_pnl = expected_proceeds - expected_cost_closed
    if (close.get("ev") != "close"
            or str(close.get("key") or "") != spec["pos_key"]
            or int(close.get("qty") or 0) != sold
            or abs(float(close.get("proceeds_krw") or 0)
                   - expected_proceeds) > 1e-6
            or abs(float(close.get("cost_closed_krw") or 0)
                   - expected_cost_closed) > 1e-6
            or abs(float(close.get("realized_pnl_krw") or 0)
                   - expected_pnl) > 1e-6
            or _close_day(close) != str(sale["day_kst"])):
        raise RecoveryRefused("기존 SELL 회계 경제값 불일치")
    book = costbook._fold()
    if not book.get("healthy"):
        raise RecoveryRefused("costbook 손상")
    lots = {
        key: lot for key, lot in (book.get("lots") or {}).items()
        if str(lot.get("symbol") or "").upper() == spec["symbol"]
        and int(lot.get("qty") or 0) > 0
    }
    lot = lots.get(spec["pos_key"])
    final_qty = int(spec["final_qty"])
    expected_remaining_cost = seed_cost - expected_cost_closed
    if (set(lots) != {spec["pos_key"]} or not lot
            or int(lot.get("qty") or 0) != final_qty
            or abs(float(lot.get("cost_krw") or 0)
                   - expected_remaining_cost) > 1e-6):
        raise RecoveryRefused("CVNA 잔여 lot 수량/원가 불일치")
    return {
        "mode": "preexisting-legacy-seed-close",
        "seed_event_id": seed_id,
        "sell_event_id": close_id,
        "seed_cost_krw": seed_cost,
        "requested_cost_krw": float(spec["cost_krw"]),
        "rounding_delta_krw": seed_cost - float(spec["cost_krw"]),
        "sell_proceeds_krw": expected_proceeds,
        "cost_closed_krw": expected_cost_closed,
        "realized_pnl_krw": expected_pnl,
        "remaining_cost_krw": expected_remaining_cost,
        "remaining_qty": final_qty,
        "day_kst": str(sale["day_kst"]),
    }


def _validate_local(spec: dict) -> dict:
    if _paths() != spec["journal_paths"]:
        raise RecoveryRefused("plan과 운영 원장 경로 불일치")
    if not ledger.ledger_healthy():
        raise RecoveryRefused("주문 원장 손상")
    order = ledger.state_of(spec["order_key"]) or {}
    _validate_order(order, spec)
    base = ownership.baseline()
    if base is None or spec["symbol"] in base:
        raise RecoveryRefused("ownership 미armed 또는 사용자 baseline 종목")
    rec = (kis_positions.load() or {}).get(spec["symbol"])
    if not rec or int(rec.get("qty") or 0) != spec["qty"]:
        raise RecoveryRefused("보호 포지션 수량 불일치")
    if abs(float(rec.get("entry") or 0) - spec["fill_price"]) > 0.005 \
            or float(rec.get("stop") or 0) <= 0:
        raise RecoveryRefused("보호 포지션 평단/손절선 불일치")
    book = costbook._fold()
    if not book.get("healthy"):
        raise RecoveryRefused("costbook 손상")
    event = (book.get("event_results") or {}).get(spec["event_id"])
    lot = (book.get("lots") or {}).get(spec["pos_key"])
    if event is None:
        if costbook.open_qty(spec["symbol"]) != 0:
            raise RecoveryRefused("기존 동종목 costbook lot 충돌")
    else:
        if abs(float(event.get("cost_krw") or 0) - spec["cost_krw"]) > 1e-6 \
                or not lot or int(lot.get("qty") or 0) != spec["qty"] \
                or abs(float(lot.get("cost_krw") or 0) - spec["cost_krw"]) > 1e-6:
            raise RecoveryRefused("복구 costbook event 충돌")
    complete = (
        int(order.get("filled") or 0) == spec["qty"]
        and int(order.get("accounted") or 0) == spec["qty"]
        and order.get("accounting_recovery_complete") is True
        and event is not None
        and rec.get("accounting_repaired") is True
        and str(rec.get("pos_key") or "") == spec["pos_key"]
    )
    return {"order": order, "position": rec, "book": book,
            "complete": complete}


def _validate_partial_exit_local(spec: dict) -> dict:
    if _paths() != spec["journal_paths"]:
        raise RecoveryRefused("plan과 운영 원장 경로 불일치")
    if not ledger.ledger_healthy():
        raise RecoveryRefused("주문 원장 손상")
    buy = ledger.state_of(spec["order_key"]) or {}
    sell = ledger.state_of(spec["sell"]["order_key"]) or {}
    _validate_order(buy, spec)
    _validate_sell_order(sell, spec)
    base = ownership.baseline()
    if base is None or spec["symbol"] in base:
        raise RecoveryRefused("ownership 미armed 또는 사용자 baseline 종목")
    rec = (kis_positions.load() or {}).get(spec["symbol"])
    if not rec or int(rec.get("qty") or 0) != int(spec["final_qty"]):
        raise RecoveryRefused("보호 포지션 최종 수량 불일치")
    if (abs(float(rec.get("entry") or 0) - spec["fill_price"]) > 0.005
            or float(rec.get("stop") or 0) <= 0
            or float(rec.get("stop0") or 0) <= 0
            or rec.get("half_done") is not True):
        raise RecoveryRefused("보호 포지션 평단/손절/절반익절 상태 불일치")
    economic = _inspect_partial_exit_costbook(spec)
    if economic != spec.get("economic_accounting"):
        raise RecoveryRefused("plan 이후 기존 경제 장부 변경")
    complete = (
        int(buy.get("filled") or 0) == int(spec["qty"])
        and int(buy.get("accounted") or 0) == int(spec["qty"])
        and buy.get("accounting_recovery_complete") is True
        and rec.get("accounting_repaired") is True
        and str(rec.get("pos_key") or "") == spec["pos_key"]
        and int(sell.get("filled") or 0) == int(spec["sell"]["qty"])
        and int(sell.get("accounted") or 0) == int(spec["sell"]["qty"])
    )
    if not complete and str(rec.get("pos_key") or "") not in {
            "", spec["pos_key"]}:
        raise RecoveryRefused("보호 포지션 pos_key 충돌")
    return {"buy_order": buy, "sell_order": sell, "position": rec,
            "economic": economic, "complete": complete}


def build_plan(*, order_key: str, odno: str, symbol: str, qty: int,
               fill_price: float, fx: float, cost_krw: float,
               trade_date: str, now: float | None = None) -> dict:
    stamp = time.time() if now is None else float(now)
    code = str(symbol or "").strip().upper()
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise RecoveryRefused("trade_date YYYYMMDD 필요")
    q = int(qty)
    if not code or q <= 0 or not str(order_key or "").strip() \
            or not str(odno or "").strip():
        raise RecoveryRefused("복구 주문 정체성 누락")
    px = _positive(fill_price, "fill_price")
    rate = _positive(fx, "fx")
    exact = _positive(cost_krw, "cost_krw")
    order = ledger.state_of(order_key) or {}
    pos_key = str(order.get("pos_key") or order_key)
    rec = (kis_positions.load() or {}).get(code) or {}
    spec = {
        "version": PLAN_VERSION, "created_at": stamp,
        "expires_at": stamp + PLAN_MAX_AGE_S,
        "journal_paths": _paths(), "order_key": str(order_key),
        "odno": str(odno), "symbol": code, "qty": q,
        "fill_price": px, "fx": rate, "cost_krw": exact,
        "trade_date": trade_date, "pos_key": pos_key,
        "event_id": f"fill:{order_key}:BUY:{q}",
        "position": {
            "entry": float(rec.get("entry") or 0),
            "stop": float(rec.get("stop") or 0),
            "stop0": float(rec.get("stop0") or rec.get("stop") or 0),
            "ccy": str(rec.get("ccy") or "USD"),
            "name": str(rec.get("name") or order.get("name") or code),
            "opened": str(rec.get("opened") or order.get("opened") or ""),
            "sleeve": str(rec.get("sleeve") or order.get("sleeve") or "A").upper(),
            "target": rec.get("target", order.get("target")),
        },
    }
    _validate_local(spec)
    spec["broker_fill"] = _broker_fill(
        trade_date=trade_date, odno=odno, symbol=code, qty=q, fill_price=px)
    spec["broker_position"] = _broker_position(code, q)
    spec["plan_sha256"] = plan_hash(spec)
    return spec


def build_partial_exit_plan(
        *, order_key: str, odno: str, sell_order_key: str, sell_odno: str,
        symbol: str, qty: int, fill_price: float, sell_qty: int,
        sell_fill_price: float, fx: float, cost_krw: float,
        trade_date: str, sell_trade_date: str, sell_day_kst: str,
        now: float | None = None) -> dict:
    """유실 BUY 뒤 이미 회계된 절반매도를 하나의 immutable plan으로 증명한다."""
    stamp = time.time() if now is None else float(now)
    code = str(symbol or "").strip().upper()
    if any(len(day) != 8 or not day.isdigit()
           for day in (trade_date, sell_trade_date)):
        raise RecoveryRefused("trade_date YYYYMMDD 필요")
    if (len(str(sell_day_kst)) != 10
            or str(sell_day_kst)[4:5] != "-"
            or str(sell_day_kst)[7:8] != "-"):
        raise RecoveryRefused("sell_day_kst YYYY-MM-DD 필요")
    q = int(qty)
    sold = int(sell_qty)
    final_qty = q - sold
    if (not code or q <= 0 or sold <= 0 or final_qty <= 0
            or not str(order_key or "").strip()
            or not str(odno or "").strip()
            or not str(sell_order_key or "").strip()
            or not str(sell_odno or "").strip()):
        raise RecoveryRefused("부분매도 복구 정체성/수량 누락")
    buy_px = _positive(fill_price, "fill_price")
    sell_px = _positive(sell_fill_price, "sell_fill_price")
    rate = _positive(fx, "fx")
    exact = _positive(cost_krw, "cost_krw")
    buy_order = ledger.state_of(order_key) or {}
    sell_order = ledger.state_of(sell_order_key) or {}
    pos_key = str(sell_order.get("pos_key") or "").strip()
    if not pos_key:
        raise RecoveryRefused("SELL 원장의 costbook pos_key 부재")
    rec = (kis_positions.load() or {}).get(code) or {}
    sell_event = f"fill:{sell_order_key}:SELL:{sold}"
    spec = {
        "version": PARTIAL_EXIT_PLAN_VERSION,
        "scenario": PARTIAL_EXIT_SCENARIO,
        "created_at": stamp, "expires_at": stamp + PLAN_MAX_AGE_S,
        "journal_paths": _paths(), "order_key": str(order_key),
        "odno": str(odno), "symbol": code, "qty": q,
        "fill_price": buy_px, "fx": rate, "cost_krw": exact,
        "trade_date": trade_date, "pos_key": pos_key,
        "event_id": f"fill:{order_key}:BUY:{q}",
        "final_qty": final_qty,
        "sell": {
            "order_key": str(sell_order_key), "odno": str(sell_odno),
            "qty": sold, "fill_price": sell_px,
            "trade_date": sell_trade_date, "day_kst": str(sell_day_kst),
            "event_id": sell_event,
            "seed_event_id": sell_event + ":legacy",
        },
        "position": {
            "entry": float(rec.get("entry") or 0),
            "stop": float(rec.get("stop") or 0),
            "stop0": float(rec.get("stop0") or rec.get("stop") or 0),
            "half_done": rec.get("half_done") is True,
            "ccy": str(rec.get("ccy") or "USD"),
            "name": str(rec.get("name") or buy_order.get("name") or code),
            "opened": str(rec.get("opened") or buy_order.get("opened") or ""),
            "sleeve": str(rec.get("sleeve") or sell_order.get("sleeve")
                          or buy_order.get("sleeve") or "A").upper(),
            "target": rec.get("target", buy_order.get("target")),
        },
    }
    spec["economic_accounting"] = _inspect_partial_exit_costbook(spec)
    _validate_partial_exit_local(spec)
    spec["broker_fill"] = _broker_fill(
        trade_date=trade_date, odno=odno, symbol=code, qty=q,
        fill_price=buy_px, side="BUY")
    spec["broker_sell_fill"] = _broker_fill(
        trade_date=sell_trade_date, odno=sell_odno, symbol=code,
        qty=sold, fill_price=sell_px, side="SELL")
    spec["broker_position"] = _broker_position(code, final_qty)
    spec["plan_sha256"] = plan_hash(spec)
    return spec


def _assert_plan(plan: dict, *, ack: str, services_stopped: bool,
                 now: float | None = None) -> None:
    stamp = time.time() if now is None else float(now)
    version = int(plan.get("version") or 0)
    if version not in {PLAN_VERSION, PARTIAL_EXIT_PLAN_VERSION}:
        raise RecoveryRefused("지원하지 않는 plan 버전")
    if (version == PARTIAL_EXIT_PLAN_VERSION
            and plan.get("scenario") != PARTIAL_EXIT_SCENARIO):
        raise RecoveryRefused("지원하지 않는 부분매도 복구 시나리오")
    digest = plan_hash(plan)
    if digest != str(plan.get("plan_sha256") or ""):
        raise RecoveryRefused("plan SHA256 불일치")
    if str(ack or "") != f"APPLY {digest}":
        raise RecoveryRefused("정확한 plan SHA operator ack 필요")
    if not services_stopped:
        raise RecoveryRefused("--services-stopped 명시 필요")
    if stamp > float(plan.get("expires_at") or 0):
        raise RecoveryRefused("plan 만료 — 새 5분 plan 필요")
    required = {
        "order_key", "odno", "symbol", "qty", "fill_price", "fx",
        "cost_krw", "trade_date", "pos_key", "event_id", "position",
        "journal_paths", "broker_fill", "broker_position",
    }
    if not required.issubset(plan):
        raise RecoveryRefused("plan 필수 필드 누락")
    if version == PARTIAL_EXIT_PLAN_VERSION:
        partial_required = {
            "scenario", "final_qty", "sell", "economic_accounting",
            "broker_sell_fill",
        }
        if not partial_required.issubset(plan):
            raise RecoveryRefused("부분매도 plan 필수 필드 누락")


def _broker_matches(plan: dict) -> None:
    fill = _broker_fill(
        trade_date=plan["trade_date"], odno=plan["odno"],
        symbol=plan["symbol"], qty=int(plan["qty"]),
        fill_price=float(plan["fill_price"]), side="BUY")
    if int(plan.get("version") or 0) == PARTIAL_EXIT_PLAN_VERSION:
        sale = plan["sell"]
        sell_fill = _broker_fill(
            trade_date=sale["trade_date"], odno=sale["odno"],
            symbol=plan["symbol"], qty=int(sale["qty"]),
            fill_price=float(sale["fill_price"]), side="SELL")
        position_qty = int(plan["final_qty"])
        if sell_fill != plan["broker_sell_fill"]:
            raise RecoveryRefused("plan 이후 SELL 브로커 증거 변경")
    else:
        position_qty = int(plan["qty"])
    position = _broker_position(plan["symbol"], position_qty)
    if fill != plan["broker_fill"] or position != plan["broker_position"]:
        raise RecoveryRefused("plan 이후 브로커 증거 변경")


def _apply_partial_exit_plan(plan: dict, *, backup_dir: str) -> dict:
    pre = _validate_partial_exit_local(plan)
    if pre["complete"]:
        return {
            "ok": True, "already_applied": True, "orders_sent": 0,
            "remaining_qty": int(plan["final_qty"]),
            "remaining_cost_krw": pre["economic"]["remaining_cost_krw"],
            "realized_pnl_krw": pre["economic"]["realized_pnl_krw"],
        }
    backup = legacy_migration._backup_sources(backup_dir)
    quiesced, why = legacy_migration._services_quiesced()
    if not quiesced:
        raise RecoveryRefused(f"백업 후 주문 서비스 재등장: {why}")
    _broker_matches(plan)
    _validate_partial_exit_local(plan)

    q = int(plan["qty"])
    final_qty = int(plan["final_qty"])
    pos = plan["position"]
    with ledger._file_lock(True):
        fold, corrupt = ledger._fold_unlocked()
        if corrupt:
            raise RecoveryRefused("mutation 직전 주문 원장 손상")
        current = fold.get(plan["order_key"]) or {}
        sale = fold.get(plan["sell"]["order_key"]) or {}
        _validate_order(current, plan)
        _validate_sell_order(sale, plan)
        # 기존 legacy seed/close는 이미 경제적으로 정확하다. 여기서는 절대
        # costbook을 재기입하지 않고 BUY 원장과 잔여 포지션 정체성만 잇는다.
        if _inspect_partial_exit_costbook(plan) != plan["economic_accounting"]:
            raise RecoveryRefused("mutation 직전 경제 장부 변경")
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {
                "pos_key": plan["pos_key"], "fx": float(plan["fx"]),
                "ccy": pos["ccy"], "stop": float(pos["stop"]),
                "target": pos.get("target"), "name": pos["name"],
                "opened": pos["opened"], "sleeve": pos["sleeve"],
                "accounting_recovery_pending": True,
                "accounting_recovery_complete": False,
                "accounting_recovery_scenario": PARTIAL_EXIT_SCENARIO,
            },
        })
        if int(current.get("filled") or 0) != q:
            ledger._append_unlocked({
                "ev": "reconcile", "key": plan["order_key"],
                "state": "filled", "filled": q, "open": False,
                "fill_price": float(plan["fill_price"]),
                "fill_price_source": "broker-recovery-delayed-ccnl",
            })
            ledger._append_unlocked({
                "ev": "reconcile_meta", "key": plan["order_key"],
                "reason": "broker-recovery-delayed-ccnl",
                "meta": {"source": "broker-recovery-delayed-ccnl",
                         "side": "BUY", "intended": q,
                         "linked_sell_key": plan["sell"]["order_key"]},
            })
        kis_positions.repair_buy_fill(
            plan["symbol"], qty=final_qty, entry=plan["fill_price"],
            stop=pos["stop"], stop0=pos["stop0"], ccy=pos["ccy"],
            pos_key=plan["pos_key"], name=pos["name"], opened=pos["opened"],
            sleeve=pos["sleeve"], target=pos.get("target"),
            half_done=True, recovered_buy_qty=q,
            recovered_sell_qty=int(plan["sell"]["qty"]),
            economic_seed_event_id=plan["sell"]["seed_event_id"],
            economic_sell_event_id=plan["sell"]["event_id"],
            event_id=plan["event_id"])
        ledger._append_unlocked({
            "ev": "accounted", "key": plan["order_key"], "accounted": q})
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {"accounting_recovery_pending": False,
                     "accounting_recovery_complete": True},
        })
    final = _validate_partial_exit_local(plan)
    if not final["complete"]:
        raise RecoveryRefused("부분매도 복구 후 세 원장 최종 검증 실패")
    return {
        "ok": True, "already_applied": False, "orders_sent": 0,
        "remaining_qty": final_qty,
        "remaining_cost_krw": final["economic"]["remaining_cost_krw"],
        "realized_pnl_krw": final["economic"]["realized_pnl_krw"],
        "costbook_mutations": 0,
        "backup_manifest_sha256": backup["manifest_sha256"],
    }


def apply_plan(plan: dict, *, ack: str, services_stopped: bool,
               backup_dir: str, now: float | None = None) -> dict:
    _assert_plan(plan, ack=ack, services_stopped=services_stopped, now=now)
    quiesced, why = legacy_migration._services_quiesced()
    if not quiesced:
        raise RecoveryRefused(f"주문 서비스 미정지: {why}")
    _broker_matches(plan)
    if int(plan.get("version") or 0) == PARTIAL_EXIT_PLAN_VERSION:
        return _apply_partial_exit_plan(plan, backup_dir=backup_dir)
    pre = _validate_local(plan)
    if pre["complete"]:
        return {"ok": True, "already_applied": True, "orders_sent": 0,
                "cost_krw": float(plan["cost_krw"])}
    backup = legacy_migration._backup_sources(backup_dir)
    quiesced, why = legacy_migration._services_quiesced()
    if not quiesced:
        raise RecoveryRefused(f"백업 후 주문 서비스 재등장: {why}")
    _broker_matches(plan)
    _validate_local(plan)

    q = int(plan["qty"])
    pos = plan["position"]
    with ledger._file_lock(True):
        fold, corrupt = ledger._fold_unlocked()
        if corrupt:
            raise RecoveryRefused("mutation 직전 주문 원장 손상")
        current = fold.get(plan["order_key"]) or {}
        _validate_order(current, plan)
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {
                "pos_key": plan["pos_key"], "fx": float(plan["fx"]),
                "ccy": pos["ccy"], "stop": float(pos["stop"]),
                "target": pos.get("target"), "name": pos["name"],
                "opened": pos["opened"], "sleeve": pos["sleeve"],
                "accounting_recovery_pending": True,
                "accounting_recovery_complete": False,
            },
        })
        if int(current.get("filled") or 0) != q:
            ledger._append_unlocked({
                "ev": "reconcile", "key": plan["order_key"],
                "state": "filled", "filled": q, "open": False,
                "fill_price": float(plan["fill_price"]),
                "fill_price_source": "broker-recovery-delayed-ccnl",
            })
            ledger._append_unlocked({
                "ev": "reconcile_meta", "key": plan["order_key"],
                "reason": "broker-recovery-delayed-ccnl",
                "meta": {"source": "broker-recovery-delayed-ccnl",
                         "side": "BUY", "intended": q},
            })
        costbook.add_recovery_lot(
            plan["pos_key"], plan["symbol"], q, plan["fill_price"],
            fx=plan["fx"], cost_krw=plan["cost_krw"],
            sleeve=pos["sleeve"], event_id=plan["event_id"])
        kis_positions.repair_buy_fill(
            plan["symbol"], qty=q, entry=plan["fill_price"],
            stop=pos["stop"], stop0=pos["stop0"], ccy=pos["ccy"],
            pos_key=plan["pos_key"], name=pos["name"], opened=pos["opened"],
            sleeve=pos["sleeve"], target=pos.get("target"),
            event_id=plan["event_id"])
        ledger._append_unlocked({
            "ev": "accounted", "key": plan["order_key"], "accounted": q})
        ledger._append_unlocked({
            "ev": "migration_meta", "key": plan["order_key"],
            "meta": {"accounting_recovery_pending": False,
                     "accounting_recovery_complete": True},
        })
    final = _validate_local(plan)
    if not final["complete"]:
        raise RecoveryRefused("복구 후 세 원장 최종 검증 실패")
    return {"ok": True, "already_applied": False, "orders_sent": 0,
            "cost_krw": float(plan["cost_krw"]), "qty": q,
            "backup_manifest_sha256": backup["manifest_sha256"]}


def _write_plan(path: str, plan: dict) -> None:
    target = os.path.abspath(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".accounting-recovery-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fp:
            fd = -1
            json.dump(plan, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="유실 BUY/후속 부분매도 forensic 회계 복구(주문 없음)")
    sub = ap.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("plan", help="KIS 체결·잔고를 재확인해 5분 plan 생성")
    px = sub.add_parser(
        "plan-partial-exit",
        help="유실 BUY와 이미 회계된 부분매도를 함께 증명하는 5분 plan 생성")
    for parser in (pp, px):
        parser.add_argument("--order-key", required=True)
        parser.add_argument("--odno", required=True)
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--qty", required=True, type=int)
        parser.add_argument("--fill-price", required=True, type=float)
        parser.add_argument("--fx", required=True, type=float)
        parser.add_argument("--cost-krw", required=True, type=float)
        parser.add_argument("--trade-date", required=True)
    pp.add_argument("--output", required=True)
    px.add_argument("--sell-order-key", required=True)
    px.add_argument("--sell-odno", required=True)
    px.add_argument("--sell-qty", required=True, type=int)
    px.add_argument("--sell-fill-price", required=True, type=float)
    px.add_argument("--sell-trade-date", required=True)
    px.add_argument("--sell-day-kst", required=True)
    px.add_argument("--output", required=True)
    pa = sub.add_parser("apply", help="검증 plan을 append-only 원장에 적용")
    pa.add_argument("--plan", required=True)
    pa.add_argument("--ack", required=True)
    pa.add_argument("--services-stopped", action="store_true")
    pa.add_argument("--backup-dir", required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_plan(
                order_key=args.order_key, odno=args.odno, symbol=args.symbol,
                qty=args.qty, fill_price=args.fill_price, fx=args.fx,
                cost_krw=args.cost_krw, trade_date=args.trade_date)
            _write_plan(args.output, plan)
            result = {"ok": True, "mode": "read-only-plan", "orders_sent": 0,
                      "plan": os.path.abspath(args.output),
                      "plan_sha256": plan["plan_sha256"],
                      "apply_ack": f"APPLY {plan['plan_sha256']}"}
        elif args.command == "plan-partial-exit":
            plan = build_partial_exit_plan(
                order_key=args.order_key, odno=args.odno,
                sell_order_key=args.sell_order_key,
                sell_odno=args.sell_odno, symbol=args.symbol, qty=args.qty,
                fill_price=args.fill_price, sell_qty=args.sell_qty,
                sell_fill_price=args.sell_fill_price, fx=args.fx,
                cost_krw=args.cost_krw, trade_date=args.trade_date,
                sell_trade_date=args.sell_trade_date,
                sell_day_kst=args.sell_day_kst)
            _write_plan(args.output, plan)
            result = {"ok": True, "mode": "read-only-partial-exit-plan",
                      "orders_sent": 0,
                      "plan": os.path.abspath(args.output),
                      "plan_sha256": plan["plan_sha256"],
                      "apply_ack": f"APPLY {plan['plan_sha256']}"}
        else:
            with open(args.plan, encoding="utf-8") as fp:
                plan = json.load(fp)
            result = apply_plan(
                plan, ack=args.ack, services_stopped=args.services_stopped,
                backup_dir=args.backup_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RecoveryRefused, legacy_migration.MigrationRefused,
            OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "refused": True, "why": str(exc),
                          "orders_sent": 0}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
