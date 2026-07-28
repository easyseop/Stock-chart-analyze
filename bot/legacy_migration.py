"""업그레이드 전 KIS 체결을 현재 회계/보호장부로 1회 이관한다.

기본 동작은 읽기 전용 plan 생성이다. apply는 KIS 모의계좌·L1 이상·서비스 정지
확인·짧은 수명의 동일 broker snapshot·계획 해시 operator ack가 모두 맞아야 한다.
주문 API는 import/호출하지 않는다.

실행 예:
  python -m bot.legacy_migration plan --output /tmp/legacy-plan.json
  python -m bot.legacy_migration apply --plan /tmp/legacy-plan.json \
    --services-stopped --ack "APPLY <plan_sha256>"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time

from bot import (
    costbook,
    kill,
    kis,
    kis_accounting,
    kis_positions,
    kis_reconcile,
    ledger,
    ownership,
    settings,
)

PLAN_VERSION = 1
PLAN_MAX_AGE_S = 300
REQUIRED_STOPPED_UNITS = ("sentinel.service", "buyloop.service")


class MigrationRefused(RuntimeError):
    """안전 전제 하나라도 증명되지 않아 이관을 거부했다."""


def _canonical(payload: dict) -> bytes:
    clean = {k: v for k, v in payload.items() if k != "plan_sha256"}
    return json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def plan_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _positive_number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MigrationRefused(f"{label} 숫자 변환 실패") from exc
    if not math.isfinite(number) or number <= 0:
        raise MigrationRefused(f"{label} 유한한 양수 필요")
    return number


def _position_history() -> tuple[dict[str, dict], list[int]]:
    """마지막 open과 이후 raise를 복원한다. 손상 줄은 무시하지 않고 호출부가 거부."""
    records: dict[str, dict] = {}
    corrupt: list[int] = []
    try:
        with kis_positions._file_lock(False):
            with open(kis_positions.PATH, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        corrupt.append(lineno)
                        continue
                    if not isinstance(ev, dict):
                        corrupt.append(lineno)
                        continue
                    code = str(ev.get("code") or "").upper()
                    if not code:
                        corrupt.append(lineno)
                        continue
                    kind = str(ev.get("ev") or "")
                    if kind == "open":
                        stop = float(ev.get("stop") or 0)
                        records[code] = {
                            "code": code, "qty": int(ev.get("qty") or 0),
                            "entry": float(ev.get("entry") or 0),
                            "stop": stop, "stop0": stop,
                            "ccy": str(ev.get("ccy") or "USD"),
                            "name": str(ev.get("name") or ""),
                            "opened": str(ev.get("opened") or ""),
                            "sleeve": str(ev.get("sleeve") or "A").upper(),
                            "target": ev.get("target"),
                        }
                    elif kind == "raise" and code in records:
                        new_stop = float(ev.get("stop") or 0)
                        if new_stop > float(records[code].get("stop") or 0):
                            records[code]["stop"] = new_stop
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        corrupt.append(-1)
    return records, corrupt


def _legacy_buys(fold: dict[str, dict]) -> list[tuple[str, dict]]:
    rows = []
    for key, order in fold.items():
        if str(order.get("side") or "").upper() != "BUY":
            continue
        filled = int(order.get("filled") or 0)
        accounted = int(order.get("accounted") or 0)
        if filled <= accounted:
            continue
        if str(order.get("pos_key") or "") \
                and order.get("legacy_migrated_order") is not True:
            continue
        if order.get("state") != "filled" or filled != int(order.get("intended") or 0):
            raise MigrationRefused(
                f"{order.get('symbol')} legacy BUY가 full-filled가 아님")
        if accounted not in (0, filled):
            raise MigrationRefused(
                f"{order.get('symbol')} legacy BUY가 부분 회계 상태")
        rows.append((key, order))
    by_symbol: dict[str, int] = {}
    for _, order in rows:
        symbol = str(order.get("symbol") or "").upper()
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
    duplicated = sorted(symbol for symbol, count in by_symbol.items() if count != 1)
    if duplicated:
        raise MigrationRefused(f"legacy BUY 종목별 유일성 실패: {duplicated}")
    return sorted(rows, key=lambda item: str(item[1].get("symbol") or ""))


def _broker_snapshot(entries: list[tuple[str, dict]]) -> dict[str, dict]:
    """후보 종목의 KIS 잔고를 읽고 거래소 중복은 동일값일 때만 합친다."""
    wanted = {str(order.get("symbol") or "").upper() for _, order in entries}
    calls: set[tuple[str, str]] = set()
    for _, order in entries:
        market = str(order.get("market") or kis.market_of_symbol(
            str(order.get("symbol") or ""))).upper()
        if market == "KR":
            calls.add(("KR", "KRX"))
        else:
            # 모의 잔고는 거래소 인자와 무관하게 같은 행을 돌려줄 수 있다.
            # 세 거래소를 모두 성공해야 US 전체 snapshot을 신뢰한다.
            calls.update({("US", "NASD"), ("US", "NYSE"), ("US", "AMEX")})

    merged: dict[str, dict] = {}
    for market, excg in sorted(calls):
        rows = kis.positions_detail(market, excg=excg)
        if rows is None:
            raise MigrationRefused(f"KIS 잔고 조회 실패: {market}/{excg}")
        for row in rows:
            symbol = str(row.get("code") or "").upper()
            if symbol not in wanted:
                continue
            try:
                average = float(row.get("avg") or 0)
            except (TypeError, ValueError) as exc:
                raise MigrationRefused(
                    f"{symbol} broker 평단 숫자 변환 실패") from exc
            if not math.isfinite(average) or average < 0:
                raise MigrationRefused(f"{symbol} broker 평단 비정상")
            normalized = {
                "qty": int(row.get("qty") or 0),
                "avg": round(average, 8),
                "market": market,
            }
            prior = merged.get(symbol)
            if prior is not None and prior != normalized:
                raise MigrationRefused(
                    f"{symbol} 거래소별 잔고 충돌: {prior} != {normalized}")
            merged[symbol] = normalized
    for symbol in wanted:
        merged.setdefault(
            symbol,
            {"qty": 0, "avg": 0.0, "market": kis.market_of_symbol(symbol)},
        )
    return dict(sorted(merged.items()))


def _effective_sells(fold: dict[str, dict], symbol: str) -> list[tuple[str, dict]]:
    terminal_or_open = {
        "submitted", "ack", "partial", "filled", "unknown", "cancel_pending",
    }
    return [
        (key, order) for key, order in fold.items()
        if str(order.get("symbol") or "").upper() == symbol
        and str(order.get("side") or "").upper() == "SELL"
        and str(order.get("state") or "") in terminal_or_open
        and (int(order.get("filled") or 0) > 0
             or str(order.get("state") or "") in {
                 "submitted", "ack", "partial", "unknown", "cancel_pending",
             })
    ]


def _entry_for(symbol: str, order: dict, broker: dict,
               history: dict[str, dict]) -> dict:
    original = int(order.get("filled") or 0)
    current = int(broker.get("qty") or 0)
    if current < 0 or current > original:
        raise MigrationRefused(
            f"{symbol} broker qty {current}가 legacy BUY {original} 범위 밖")
    rec = history.get(symbol) or {}
    stop = _positive_number(rec.get("stop"), f"{symbol} stop")
    stop0 = _positive_number(rec.get("stop0") or stop, f"{symbol} stop0")
    historical_entry = _positive_number(
        rec.get("entry"), f"{symbol} historical entry")
    broker_avg = float(broker.get("avg") or 0)
    if not math.isfinite(broker_avg) or broker_avg < 0:
        raise MigrationRefused(f"{symbol} broker average 비정상")
    entry = (
        _positive_number(broker_avg, f"{symbol} broker average")
        if current > 0 else historical_entry)
    if not rec.get("opened"):
        raise MigrationRefused(f"{symbol} 진입가/손절선/opened 복원 실패")
    sleeve = str(rec.get("sleeve") or "A").upper()
    if sleeve not in ("A", "B"):
        raise MigrationRefused(f"{symbol} sleeve가 A/B가 아님")
    pos_key = f"legacy:{sleeve}:{symbol}:{rec['opened']}"
    raw_target = rec.get("target")
    if raw_target in (None, "", 0, 0.0):
        target = None
    else:
        target = _positive_number(raw_target, f"{symbol} target")
    return {
        "symbol": symbol,
        "original_qty": original,
        "current_qty": current,
        "entry": round(entry, 8),
        "broker_avg": round(broker_avg, 8),
        "stop": round(stop, 8),
        "stop0": round(stop0, 8),
        "ccy": str(rec.get("ccy") or (
            "KRW" if broker.get("market") == "KR" else "USD")),
        "name": str(rec.get("name") or symbol),
        "opened": str(rec["opened"]),
        "sleeve": sleeve,
        "target": target,
        "pos_key": pos_key,
        "market": str(order.get("market") or broker.get("market") or "US"),
    }


def _validate_sale(entry: dict, fold: dict[str, dict]) -> dict:
    symbol = entry["symbol"]
    sold = int(entry["original_qty"]) - int(entry["current_qty"])
    sells = _effective_sells(fold, symbol)
    if sold == 0:
        if sells:
            raise MigrationRefused(f"{symbol} 잔고 감소 0인데 유효 SELL 원장 존재")
        return {"sell_key": "", "sell_mode": "none", "sold_qty": 0}
    if len(sells) != 1:
        raise MigrationRefused(
            f"{symbol} 잔고 감소 {sold}주를 설명하는 SELL이 {len(sells)}건")
    key, sale = sells[0]
    state = str(sale.get("state") or "")
    if state == "filled":
        proven = int(sale.get("filled") or 0)
        mode = "filled"
    elif state in ("submitted", "ack"):
        proven = int(sale.get("intended") or 0)
        before = int(sale.get("hldg_before") or -1)
        if int(sale.get("filled") or 0) != 0 \
                or before not in (proven, entry["original_qty"]):
            raise MigrationRefused(f"{symbol} 열린 SELL의 before/filled 증거 불일치")
        mode = "ack-balance"
    else:
        raise MigrationRefused(f"{symbol} SELL 상태 {state}는 자동 이관 금지")
    if proven != sold:
        raise MigrationRefused(
            f"{symbol} broker 감소 {sold} != SELL 증거 {proven}")
    sale_pos_key = str(sale.get("pos_key") or "")
    if sale_pos_key and sale_pos_key != entry["pos_key"]:
        raise MigrationRefused(f"{symbol} SELL pos_key 불일치")
    sell_price = _positive_number(
        sale.get("fill_price") or sale.get("price"),
        f"{symbol} SELL price")
    return {
        "sell_key": key, "sell_mode": mode, "sold_qty": sold,
        "sell_price": round(sell_price, 8),
        "sell_price_source": str(
            sale.get("fill_price_source")
            or ("legacy-ledger-price" if mode == "filled"
                else "submitted-fallback")),
    }


def _verify_costbook_prestate(entries: list[dict]) -> None:
    book = costbook._fold()
    if not book.get("healthy"):
        raise MigrationRefused(
            f"costbook 손상: {book.get('corrupt_lines') or ['unknown']}")
    lots = book.get("lots") or {}
    for entry in entries:
        symbol = entry["symbol"]
        pos_key = entry["pos_key"]
        other = [
            key for key, lot in lots.items()
            if str(lot.get("symbol") or "").upper() == symbol and key != pos_key
            and int(lot.get("qty") or 0) > 0
        ]
        if other:
            raise MigrationRefused(f"{symbol} 다른 costbook lot 존재")
        if pos_key in lots:
            event_id = (
                f"fill:{entry['buy_key']}:BUY:{entry['original_qty']}")
            if event_id not in (book.get("event_results") or {}):
                raise MigrationRefused(f"{symbol} 출처 불명 동일 pos_key lot 존재")


def build_plan(*, now: float | None = None) -> dict:
    if kis.ENV != "mock":
        raise MigrationRefused("legacy 이관은 KIS mock에서만 허용")
    health = ledger.corruption_status()
    if not health["healthy"]:
        raise MigrationRefused(f"주문 원장 손상: {health['lines']}")
    if ownership.baseline() is None:
        raise MigrationRefused("ownership baseline 미armed")
    fold = ledger._fold()
    buys = _legacy_buys(fold)
    if not buys:
        raise MigrationRefused("미회계 legacy BUY 없음")
    history, corrupt = _position_history()
    if corrupt:
        raise MigrationRefused(f"kis_positions 손상: {corrupt}")
    current = kis_positions.load()
    history.update(current)  # 열린 포지션은 raise가 반영된 fold를 우선한다.
    broker = _broker_snapshot(buys)

    entries = []
    for buy_key, order in buys:
        symbol = str(order.get("symbol") or "").upper()
        entry = _entry_for(symbol, order, broker[symbol], history)
        entry["buy_key"] = buy_key
        entry.update(_validate_sale(entry, fold))
        entries.append(entry)
    _verify_costbook_prestate(entries)

    stamp = time.time() if now is None else float(now)
    plan = {
        "version": PLAN_VERSION,
        "environment": kis.ENV,
        "generated_at": stamp,
        "expires_at": stamp + PLAN_MAX_AGE_S,
        "broker_snapshot": broker,
        "entries": entries,
        "safety": {
            "orders_sent": False,
            "apply_requires_l1": True,
            "apply_requires_services_stopped": True,
            "apply_requires_fresh_exact_snapshot": True,
        },
    }
    plan["plan_sha256"] = plan_hash(plan)
    return plan


def _assert_plan(plan: dict, *, ack: str, services_stopped: bool,
                 now: float | None = None) -> None:
    stamp = time.time() if now is None else float(now)
    if int(plan.get("version") or 0) != PLAN_VERSION:
        raise MigrationRefused("지원하지 않는 plan version")
    expected = plan_hash(plan)
    if str(plan.get("plan_sha256") or "") != expected:
        raise MigrationRefused("plan SHA-256 불일치")
    if ack.strip() != f"APPLY {expected}":
        raise MigrationRefused("operator ack 불일치")
    if not services_stopped:
        raise MigrationRefused("sentinel/buyloop 서비스 정지 확인 필요")
    if kis.ENV != "mock" or plan.get("environment") != "mock":
        raise MigrationRefused("KIS mock plan만 apply 가능")
    if kill.level() < 1 or kill.allows("buy_new"):
        raise MigrationRefused("L1 이상 신규매수 차단이 확인되지 않음")
    generated = float(plan.get("generated_at") or 0)
    expires = float(plan.get("expires_at") or 0)
    if not all(math.isfinite(value) for value in (stamp, generated, expires)) \
            or abs((expires - generated) - PLAN_MAX_AGE_S) > 1e-6 \
            or stamp > expires or stamp < generated:
        raise MigrationRefused("plan snapshot 만료/시각 이상 — plan 재생성 필요")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise MigrationRefused("plan entries 부재")
    symbols: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MigrationRefused("plan entry 형식 오류")
        symbol = str(entry.get("symbol") or "").upper()
        if not symbol or symbol in symbols:
            raise MigrationRefused("plan symbol 부재/중복")
        symbols.add(symbol)
        original = int(entry.get("original_qty") or 0)
        current = int(entry.get("current_qty") or 0)
        sold = int(entry.get("sold_qty") or 0)
        if original <= 0 or current < 0 or current > original \
                or sold != original - current:
            raise MigrationRefused(f"{symbol} plan 수량 불변식 실패")
        for field in ("entry", "stop", "stop0"):
            _positive_number(entry.get(field), f"{symbol} plan {field}")
        if sold > 0:
            _positive_number(
                entry.get("sell_price"), f"{symbol} plan sell_price")
        if not entry.get("buy_key") or not entry.get("pos_key"):
            raise MigrationRefused(f"{symbol} plan 정체성 누락")


def _snapshot_matches(plan: dict) -> None:
    synthetic = [
        (entry["buy_key"], {
            "symbol": entry["symbol"], "market": entry["market"],
        })
        for entry in plan["entries"]
    ]
    fresh = _broker_snapshot(synthetic)
    if fresh != plan.get("broker_snapshot"):
        raise MigrationRefused("broker snapshot 변경 — 서비스 정지 후 plan 재생성 필요")


def _migration_meta(entry: dict) -> dict:
    return {
        "pos_key": entry["pos_key"], "sleeve": entry["sleeve"],
        "fx": 1.0 if entry["market"] == "KR" else float(settings.FX_USDKRW),
        "ccy": entry["ccy"], "stop": entry["stop"],
        "target": entry.get("target"), "name": entry["name"],
        "opened": entry["opened"], "legacy_migrated_order": True,
        "legacy_hldg_before": entry["original_qty"],
    }


def _prepare_entry(entry: dict) -> None:
    meta = _migration_meta(entry)
    fill_event = f"fill:{entry['buy_key']}:BUY:{entry['original_qty']}"
    costbook.add_lot(
        entry["pos_key"], entry["symbol"], entry["original_qty"],
        entry["entry"], fx=meta["fx"], sleeve=entry["sleeve"],
        event_id=fill_event,
    )
    kis_positions.migrate_legacy(
        entry["symbol"], qty=entry["original_qty"], entry=entry["entry"],
        stop=entry["stop"], stop0=entry["stop0"], ccy=entry["ccy"],
        pos_key=entry["pos_key"], name=entry["name"],
        opened=entry["opened"], sleeve=entry["sleeve"],
        target=entry.get("target"), event_id=fill_event,
    )
    ledger.attach_migration_meta(entry["buy_key"], meta=meta)
    if entry["sell_key"]:
        ledger.attach_migration_meta(
            entry["sell_key"],
            meta={**meta, "fill_price_source": entry["sell_price_source"]},
        )


def _verify_entry(entry: dict) -> None:
    symbol = entry["symbol"]
    expected = int(entry["current_qty"])
    book = costbook._fold()
    if not book.get("healthy"):
        raise MigrationRefused(f"{symbol} apply 후 costbook 손상")
    lot = (book.get("lots") or {}).get(entry["pos_key"]) or {}
    if int(lot.get("qty") or 0) != expected:
        raise MigrationRefused(
            f"{symbol} costbook qty {lot.get('qty')} != broker {expected}")
    rec = kis_positions.load().get(symbol)
    if expected == 0:
        if rec:
            raise MigrationRefused(f"{symbol} broker 0인데 보호 포지션 잔존")
    elif not rec or int(rec.get("qty") or 0) != expected \
            or rec.get("legacy_migrated") is not True \
            or str(rec.get("pos_key") or "") != entry["pos_key"]:
        raise MigrationRefused(f"{symbol} 보호 포지션 최종 검증 실패")
    if entry["sell_key"]:
        sale = ledger.state_of(entry["sell_key"]) or {}
        if sale.get("state") != "filled" \
                or int(sale.get("accounted") or 0) != entry["sold_qty"]:
            raise MigrationRefused(f"{symbol} SELL 체결/회계 최종 검증 실패")


def _services_quiesced() -> tuple[bool, str]:
    """주문 서비스가 inactive이며 재기동 불가(runtime mask)인지 확인한다."""
    for unit in REQUIRED_STOPPED_UNITS:
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", unit],
                check=False, capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{unit} 상태 조회 실패({type(exc).__name__})"
        state = str(proc.stdout or "").strip()
        if state != "inactive":
            return False, f"{unit}={state or 'unknown'} (inactive 필요)"
        try:
            enabled = subprocess.run(
                ["systemctl", "is-enabled", unit],
                check=False, capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{unit} mask 조회 실패({type(exc).__name__})"
        mask_state = str(enabled.stdout or "").strip()
        if mask_state not in ("masked", "masked-runtime"):
            return False, (
                f"{unit}={mask_state or 'unknown'} "
                "(systemctl mask --runtime 필요)")
    return True, "ok"


def _backup_sources(backup_dir: str) -> dict:
    """세 append-only 원장을 apply 전에 byte-for-byte 백업하고 fsync한다."""
    target = os.path.abspath(str(backup_dir or ""))
    if not backup_dir or target in ("/", os.path.expanduser("~")):
        raise MigrationRefused("안전한 절대 backup dir 필요")
    sources = {
        "order_ledger.jsonl": os.path.abspath(ledger.LEDGER_PATH),
        "kis_positions.jsonl": os.path.abspath(kis_positions.PATH),
        "costbook.jsonl": os.path.abspath(costbook._path()),
    }
    for label, source in sources.items():
        if os.path.islink(source):
            raise MigrationRefused(f"backup source 부재/심볼릭링크: {label}")
        if not os.path.isfile(source) and label != "costbook.jsonl":
            raise MigrationRefused(f"backup source 부재/심볼릭링크: {label}")
    if os.path.lexists(target):
        raise MigrationRefused("backup dir가 이미 존재함")
    parent = os.path.dirname(target)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.mkdir(target, 0o700)
    manifest = {"version": 1, "created_at": time.time(), "files": {}}
    try:
        for label, source in sources.items():
            if not os.path.exists(source):
                manifest["files"][label] = {
                    "exists": False,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "size": 0,
                }
                continue
            destination = os.path.join(target, label)
            digest = hashlib.sha256()
            size = 0
            with open(source, "rb") as src, open(destination, "xb") as dst:
                os.chmod(destination, 0o600)
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            manifest["files"][label] = {
                "exists": True, "sha256": digest.hexdigest(), "size": size,
            }
        manifest_path = os.path.join(target, "manifest.json")
        manifest_payload = (
            json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        with open(manifest_path, "xb") as fp:
            os.chmod(manifest_path, 0o600)
            fp.write(manifest_payload)
            fp.flush()
            os.fsync(fp.fileno())
        dfd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception as exc:
        # 불완전 백업은 그대로 남겨 forensic 근거로 보존하되 apply는 중단한다.
        raise MigrationRefused(
            f"원장 백업 실패({type(exc).__name__}) — apply 중단") from exc
    manifest["manifest_sha256"] = hashlib.sha256(manifest_payload).hexdigest()
    return manifest


def apply_plan(plan: dict, *, ack: str, services_stopped: bool,
               backup_dir: str, now: float | None = None) -> dict:
    """주문을 내지 않고 장부만 재구성한다. 실패 시 BUY 예약은 끝까지 유지된다."""
    _assert_plan(
        plan, ack=ack, services_stopped=services_stopped, now=now,
    )
    quiesced, why = _services_quiesced()
    if not quiesced:
        raise MigrationRefused(f"주문 서비스 미정지: {why}")
    _snapshot_matches(plan)
    if not ledger.ledger_healthy():
        raise MigrationRefused("apply 직전 주문 원장 손상")
    _verify_costbook_prestate(plan["entries"])
    backup = _backup_sources(backup_dir)

    # 모든 legacy position을 먼저 원래 BUY 수량으로 복원해야 baseline SELL 예외의
    # pos_key·before·costbook 3중 증명이 성립한다.
    for entry in plan["entries"]:
        buy = ledger.state_of(entry["buy_key"]) or {}
        if int(buy.get("accounted") or 0) < int(entry["original_qty"]):
            _prepare_entry(entry)

    # 이미 filled인 legacy SELL(BAM형)은 직접 회계한다.
    for entry in plan["entries"]:
        if entry["sell_mode"] != "filled":
            continue
        result = kis_accounting.sync_fill(
            entry["sell_key"], filled_qty=entry["sold_qty"],
            fill_price=entry["sell_price"],
            fill_price_source="legacy-ledger-price",
        )
        if not result.get("ok") or int(result.get("delta") or 0) < 0:
            raise MigrationRefused(
                f"{entry['symbol']} terminal SELL 회계 실패: {result}")

    # ACK인데 브로커 잔고가 정확히 줄어든 CAG/KKR/LW형은 기존 resolver를 통과시킨다.
    hmaps: dict[str, dict[str, int]] = {}
    for symbol, row in plan["broker_snapshot"].items():
        hmaps.setdefault(str(row["market"]), {})[symbol] = int(row["qty"])
    wanted_ack = {
        entry["sell_key"] for entry in plan["entries"]
        if entry["sell_mode"] == "ack-balance"
    }
    resolved = kis_reconcile.resolve_acks_by_balance(
        hmaps,
        now_ts=float(plan["generated_at"]) + kis_reconcile.ACK_AGE_MIN_S + 1,
        only_keys=wanted_ack,
    )
    # reconcile 이벤트 append 뒤 accounting 전에 프로세스가 죽으면 주문은 이미
    # filled라 resolver 대상에서 빠진다. 같은 plan 재실행이 그 창을 복구하도록
    # 미회계 누적분을 plan의 보수적 가격 근거로 직접 동기화한다.
    for entry in plan["entries"]:
        if entry["sell_mode"] != "ack-balance":
            continue
        sale = ledger.state_of(entry["sell_key"]) or {}
        if sale.get("state") == "filled" \
                and int(sale.get("accounted") or 0) < int(entry["sold_qty"]):
            result = kis_accounting.sync_fill(
                entry["sell_key"], filled_qty=entry["sold_qty"],
                fill_price=entry["sell_price"],
                fill_price_source=entry["sell_price_source"],
            )
            if not result.get("ok"):
                raise MigrationRefused(
                    f"{entry['symbol']} ACK 후 SELL 회계 복구 실패: {result}")
    got_ack = {row["key"] for row in resolved}
    still_open = {
        key for key in wanted_ack
        if (ledger.state_of(key) or {}).get("state") != "filled"
    }
    if still_open or not wanted_ack.issubset(got_ack | {
            key for key in wanted_ack
            if int((ledger.state_of(key) or {}).get("accounted") or 0) > 0
    }):
        raise MigrationRefused(
            f"legacy ACK 잔고대사 미해소: {sorted(still_open or wanted_ack - got_ack)}")

    # 모든 하위 장부가 broker current와 맞은 뒤에만 BUY 예약을 해제한다.
    completed = []
    for entry in plan["entries"]:
        _verify_entry(entry)
        if int((ledger.state_of(entry["buy_key"]) or {}).get("accounted") or 0) \
                < int(entry["original_qty"]):
            ledger.mark_accounted(entry["buy_key"], entry["original_qty"])
        if int((ledger.state_of(entry["buy_key"]) or {}).get("accounted") or 0) \
                != int(entry["original_qty"]):
            raise MigrationRefused(f"{entry['symbol']} BUY accounted 기록 실패")
        completed.append(entry["symbol"])
    return {
        "ok": True, "migrated": completed, "count": len(completed),
        "orders_sent": 0, "plan_sha256": plan["plan_sha256"],
        "backup_dir": os.path.abspath(backup_dir),
        "backup_manifest_sha256": backup["manifest_sha256"],
    }


def _write_plan(path: str, plan: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".legacy-plan-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
            fd = -1
            json.dump(
                plan, f, ensure_ascii=False, indent=2, sort_keys=True,
                allow_nan=False,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIS legacy 체결 장부 1회 이관")
    sub = ap.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("plan", help="읽기 전용 계획 생성")
    pp.add_argument("--output", required=True)
    pa = sub.add_parser("apply", help="검증된 계획 적용(주문 없음)")
    pa.add_argument("--plan", required=True)
    pa.add_argument("--ack", required=True)
    pa.add_argument("--services-stopped", action="store_true")
    pa.add_argument("--backup-dir", required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_plan()
            _write_plan(args.output, plan)
            print(json.dumps({
                "ok": True, "mode": "read-only-plan", "entries": len(plan["entries"]),
                "plan": os.path.abspath(args.output),
                "plan_sha256": plan["plan_sha256"],
                "apply_ack": f"APPLY {plan['plan_sha256']}",
            }, ensure_ascii=False, indent=2))
        else:
            with open(args.plan, encoding="utf-8") as f:
                plan = json.load(f)
            print(json.dumps(apply_plan(
                plan, ack=args.ack, services_stopped=args.services_stopped,
                backup_dir=args.backup_dir,
            ), ensure_ascii=False, indent=2))
        return 0
    except (MigrationRefused, OSError, ValueError, KeyError) as exc:
        print(json.dumps({
            "ok": False, "refused": True, "why": str(exc),
            "orders_sent": 0,
        }, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
