"""O4 — 부팅/재시작 대사: 원장의 미해소 UNKNOWN을 브로커 조회로 자동 대사.

왜(설계 O4·리뷰 R4): 크래시·재시작 직후가 가장 위험하다 — 프로세스가 죽기 직전에
보낸 주문의 결과를 모른 채(UNKNOWN) 다시 뜨면, 대사 없이 매매를 재개할 경우
이중주문/초과매도가 난다. 그래서:

  · 시작 시 nccs(미체결)+ccnl(체결내역)을 읽어 `kis_reconcile.reconcile_unknowns`.
  · **대사가 끝나기 전(또는 실패 시) 신규 매매 금지** — `trading_allowed()` 게이트.
  · LOW(후보 0/2+)는 자동 해소 금지 → 잠금 유지 + P0 알림(수동 검토).
  · 조회 자체가 실패하면(네트워크 등) fail-closed: 게이트 닫힌 채 유지.

사용(상시 서버/파수꾼 시작 시퀀스):
    from bot import kis_boot
    summary = kis_boot.boot_reconcile()      # 1회 대사
    if kis_boot.trading_allowed():           # True여야 신규 매매 허용
        ...
읽기·계산 전용 — 주문 없음.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import time
from zoneinfo import ZoneInfo

from bot import kis, kis_reconcile, ledger

# 모듈 상태 + 프로세스 간 공유 스냅샷. /diagnosis은 telegram 프로세스에서
# 읽으므로 메모리 dict만으로는 buyloop/sentinel의 성공·실패가 보이지 않는다.
_STATE = {"done": False, "low": 0, "last_success_at": None,
          "failure_streak": 0, "last_error": "", "failure_alerted": False}
_HEALTH_KEYS = ("last_success_at", "failure_streak", "last_error",
                "failure_alerted")
RECONCILE_FAILURE_ALERT_N = int(
    os.environ.get("RECONCILE_FAILURE_ALERT_N", "6") or 6)
_RECENT_RECONCILE_EVENTS: dict[str, dict] = {}


def _status_path() -> str:
    return os.environ.get(
        "KIS_RECONCILE_STATUS_PATH",
        os.path.join(os.path.dirname(ledger.LEDGER_PATH), "reconcile_status.json"),
    )


def _read_status_unlocked(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _update_status(*, success: bool, error: str = "") -> tuple[dict, bool]:
    """대사 건강상태를 원자 저장. 반환 두 번째 값은 임계 1회 알림 여부."""
    path = _status_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    lock_path = path + ".lock"
    should_alert = False
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            disk = _read_status_unlocked(path)
            state = {key: disk.get(key, _STATE.get(key))
                     for key in _HEALTH_KEYS}
            if success:
                state.update(last_success_at=time.time(), failure_streak=0,
                             last_error="", failure_alerted=False)
            else:
                streak = int(state.get("failure_streak") or 0) + 1
                alerted = bool(state.get("failure_alerted"))
                should_alert = streak >= max(1, RECONCILE_FAILURE_ALERT_N) and not alerted
                state.update(failure_streak=streak, last_error=str(error)[:160],
                             failure_alerted=alerted)
            tmp = f"{path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(state, fp, ensure_ascii=False, separators=(",", ":"))
                fp.flush()
                os.fsync(fp.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            _STATE.update(state)
            return dict(state), should_alert
    except OSError:
        if success:
            _STATE.update(last_success_at=time.time(), failure_streak=0,
                          last_error="", failure_alerted=False)
        else:
            _STATE["failure_streak"] = int(_STATE.get("failure_streak") or 0) + 1
            _STATE["last_error"] = str(error)[:160]
            should_alert = (_STATE["failure_streak"]
                            >= max(1, RECONCILE_FAILURE_ALERT_N)
                            and not _STATE.get("failure_alerted"))
        return dict(_STATE), should_alert


def _mark_failure_alerted(expected_streak: int) -> None:
    """경보 전송 성공 뒤에만 공유 래치를 잠근다."""
    path = _status_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        with open(path + ".lock", "a+", encoding="utf-8") as lock:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            disk = _read_status_unlocked(path)
            state = {key: disk.get(key, _STATE.get(key))
                     for key in _HEALTH_KEYS}
            # 전송 중 다른 프로세스가 성공 대사를 기록했다면, 이미 끝난 실패
            # 구간의 래치를 새 성공 구간 위에 덮어쓰지 않는다.
            if int(state.get("failure_streak") or 0) < int(expected_streak):
                _STATE.update(state)
                return
            state["failure_alerted"] = True
            tmp = f"{path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(state, fp, ensure_ascii=False, separators=(",", ":"))
                fp.flush()
                os.fsync(fp.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            _STATE.update(state)
    except OSError:
        # 디스크 실패 시 같은 프로세스에서만 중복을 줄인다. 다음 프로세스의
        # 재경보 가능성은 남겨 경보 영구 유실보다 안전한 방향을 택한다.
        if int(_STATE.get("failure_streak") or 0) >= int(expected_streak):
            _STATE["failure_alerted"] = True


def reconcile_health() -> dict:
    """진단용 대사 상태. 시크릿이나 주문 상세를 포함하지 않는다."""
    disk = _read_status_unlocked(_status_path())
    state = {key: disk.get(key, _STATE.get(key)) for key in _HEALTH_KEYS}
    return {"last_success_at": state.get("last_success_at"),
            "failure_streak": int(state.get("failure_streak") or 0),
            "last_error": str(state.get("last_error") or "")[:160]}


def _record_success() -> None:
    _update_status(success=True)


def _record_failure(error: str) -> None:
    state, should_alert = _update_status(success=False, error=error)
    print(f"[kis-reconcile] 대사 실패 #{state['failure_streak']}: "
          f"{state.get('last_error') or 'unknown'}", flush=True)
    if should_alert:
        delivered = _notify(
            f"🚨 KIS 주문 대사 {state['failure_streak']}회 연속 실패 — "
            "조회 실패를 부재로 판정하지 않고 주문 잠금 유지",
            critical=True, category="trade")
        if delivered:
            _mark_failure_alerted(int(state.get("failure_streak") or 0))


def _notify(text: str, *, critical: bool = False,
            category: str | None = None) -> bool:
    try:
        from bot import notify
        return bool(notify.send(text, critical=critical, category=category))
    except Exception:
        return False


def _reconcile_notice_context(results: list[dict], orders: list[dict],
                              hmaps: dict[str, dict | None], *,
                              now: float | None = None) -> dict[str, dict]:
    """대사 결과의 접수시각·브로커/장부 정합·5분 내 반대사건 관계를 만든다.

    관측 실패는 ``미확인``일 뿐 대사 결과나 주문 상태를 되돌리지 않는다.
    반환 key는 주문 원장 key이며 알림 문자열 외에는 아무 상태도 변경하지 않는다.
    """
    from bot import kis_positions

    stamp = time.time() if now is None else float(now)
    order_by_key = {str(row.get("key") or ""): row for row in orders}
    balances = dict(hmaps)

    def market_of(result: dict, order: dict) -> str:
        market = str(result.get("market") or order.get("market") or "").upper()
        return "KR" if market == "KR" or kis.market_of_symbol(
            result.get("symbol") or order.get("symbol") or "") == "KR" else "US"

    needed = {market_of(row, order_by_key.get(str(row.get("key") or ""), {}))
              for row in results}
    if "KR" in needed and "KR" not in balances:
        balances["KR"] = kis.holdings("KR")
    if "US" in needed and "US" not in balances:
        merged: dict | None = {}
        for excg in ("NASD", "NYSE", "AMEX"):
            rows = kis.holdings("US", excg=excg)
            if rows is None:
                merged = None
                break
            merged.update(rows)
        balances["US"] = merged
    try:
        book = kis_positions.load()
        book_ok = isinstance(book, dict)
    except Exception:
        book, book_ok = {}, False

    tz = ZoneInfo("Asia/Seoul")
    # 5분보다 오래된 사건은 관계 후보에서 제거한다.
    for symbol, event in list(_RECENT_RECONCILE_EVENTS.items()):
        if stamp - float(event.get("at") or 0) > 300:
            _RECENT_RECONCILE_EVENTS.pop(symbol, None)

    contexts: dict[str, dict] = {}
    for result in results:
        key = str(result.get("key") or "")
        order = order_by_key.get(key, {})
        symbol = str(result.get("symbol") or order.get("symbol") or "").upper()
        market = market_of(result, order)
        submitted_at = float(order.get("submitted_at") or 0)
        submitted_text = (datetime.datetime.fromtimestamp(submitted_at, tz)
                          .strftime("%m/%d %H:%M") if submitted_at > 0 else "시각 미상")
        broker = balances.get(market)
        if broker is None or not book_ok:
            parity = "정합 미확인(잔고 조회 실패)"
            mismatch = False
        else:
            try:
                broker_qty = int(broker.get(symbol, 0))
                book_qty = int((book.get(symbol) or {}).get("qty") or 0)
                mismatch = broker_qty != book_qty
                parity = (f"🚨 불일치(보유 {broker_qty} vs 장부 {book_qty}) — "
                          "수동 확인 필요" if mismatch else
                          f"✅ 정합(보유 {broker_qty}주 = 장부 {book_qty}주)")
            except (TypeError, ValueError):
                parity, mismatch = "정합 미확인(잔고 조회 실패)", False
        kind = "filled" if int(result.get("filled") or 0) > 0 else "rejected"
        relation = ""
        previous = _RECENT_RECONCILE_EVENTS.get(symbol)
        if previous and previous.get("kind") != kind \
                and stamp - float(previous.get("at") or 0) <= 300:
            if kind == "rejected" and previous.get("kind") == "filled":
                fill_hm = datetime.datetime.fromtimestamp(
                    float(previous["at"]), tz).strftime("%H:%M")
                relation = (f"오늘 {fill_hm} 체결분과 별개 — "
                            f"{submitted_text[:5]} 접수 과거 전표 정리")
            else:
                previous_hm = datetime.datetime.fromtimestamp(
                    float(previous["at"]), tz).strftime("%H:%M")
                relation = (f"오늘 {previous_hm} 거절 종결분과 별개 — "
                            f"{submitted_text} 접수 주문 체결")
        contexts[key] = {"submitted": submitted_text, "parity": parity,
                         "mismatch": mismatch, "relation": relation}
        _RECENT_RECONCILE_EVENTS[symbol] = {
            "kind": kind, "at": stamp, "submitted_at": submitted_at}
    return contexts


def _format_reconcile_notice(result: dict, context: dict) -> tuple[str, bool] | None:
    """H4 알림 본문과 critical 여부를 순수하게 결정한다."""
    detail = (f" · 접수 {context.get('submitted', '시각 미상')}\n"
              f"{context.get('parity', '정합 미확인(잔고 조회 실패)')}")
    if context.get("relation"):
        detail += f"\n{context['relation']}"
    try:
        filled = int(result.get("filled") or 0)
    except (TypeError, ValueError):
        return None
    mismatch = context.get("mismatch") is True
    if filled <= 0:
        if result.get("state") != "rejected":
            return None
        side_name = "매도" if result.get("side") == "SELL" else "매수"
        via = "부재 증명" if result.get("via") == "absence-proof" else "브로커 종결 행"
        qty = int(result.get("residual") or result.get("intended") or 0)
        reason = str(result.get("broker_reason") or "사유 미상")
        suffix = " · 보호는 유지" if result.get("side") == "SELL" else ""
        return (f"⚠️ {side_name} 거절 종결({via}) — "
                f"{result.get('symbol')} {qty}주 · {reason}{suffix}{detail}",
                result.get("side") == "SELL" or mismatch)
    return (f"✅ 체결 확정(잔고대사) — {result.get('symbol')} "
            f"{'매수' if result.get('side') == 'BUY' else '매도'} "
            f"{filled}주{detail}", result.get("side") == "SELL" or mismatch)


def pending_unknowns() -> list[dict]:
    """원장에서 미해소 UNKNOWN 주문 목록."""
    return [o for o in ledger.open_orders()
            if o.get("state") == "unknown" and not o.get("reconciled")]


def _resolve_acks() -> list[dict]:
    """ACK를 미체결 → 체결내역 → 잔고 순서로 대사한다.

    직접 ODNO 행·정확한 잔고 delta는 기존 경로로 확정하고, 600초 이상
    주문은 세 조회가 모두 완전히 성공한 경우에만 부재 증명으로 거절
    종결한다. 빈 응답과 조회 실패를 구분하는 것이 핵심 계약이다.
    """
    try:
        now = time.time()
        initial_open = ledger.open_orders()
        aged = [o for o in initial_open
                if o.get("state") in ("submitted", "ack")
                and (o.get("side") or "").upper() in ("BUY", "SELL")
                and now - float(o.get("submitted_at") or 0)
                >= kis_reconcile.ACK_AGE_MIN_S]
        if not aged:
            _record_success()
            return []

        def _is_kr(u: dict) -> bool:
            return (u.get("market") == "KR"
                    or kis.market_of_symbol(u.get("symbol", "")) == "KR")

        def _day(order: dict) -> str:
            stamp = float(order.get("submitted_at") or now)
            zone = ZoneInfo("Asia/Seoul") if _is_kr(order) \
                else ZoneInfo("America/New_York")
            return datetime.datetime.fromtimestamp(stamp, zone).strftime("%Y%m%d")

        fill_rows: list[dict] = []
        proofs: dict[str, dict] = {}
        errors: list[str] = []

        # 국내: mock의 미체결 전용 폴백은 강한 단일페이지 검증 후만.
        kr_orders = [o for o in aged if _is_kr(o)]
        if kr_orders:
            n_raw = kis.domestic_open_orders()
            n_rows = kis_reconcile.trusted_response_rows(n_raw, domestic=True)
            if n_rows is None and kis.IS_MOCK:
                n_raw = kis.domestic_unfilled_orders()
                n_rows = kis_reconcile.trusted_response_rows(n_raw, domestic=True)
            if n_rows is None:
                errors.append("KR nccs untrusted")
            c_by_day: dict[str, list[dict] | None] = {}
            c_all: list[dict] = []
            for day in sorted({_day(o) for o in kr_orders}):
                raw = kis.domestic_fills(start=day, end=day)
                rows = kis_reconcile.trusted_response_rows(raw, domestic=True)
                c_by_day[day] = rows
                if rows is None:
                    errors.append(f"KR ccnl {day} untrusted")
                else:
                    c_all.extend(rows)
            fill_rows += kis_reconcile.normalize_domestic_rows(
                {"rt_cd": "0", "output": n_rows} if n_rows is not None else None,
                {"rt_cd": "0", "output": c_all})
            for order in kr_orders:
                proofs[str(order.get("key") or "")] = {
                    "nccs_rows": n_rows, "ccnl_rows": c_by_day.get(_day(order)),
                    "holdings": None,
                }

        # 해외: 주문 meta.excg는 구버전에서 없거나 시세 판별 실패로 NASD가
        # 기록될 수 있다. 모의 KIS는 잘못된 거래소 접수도 허용하므로 한 거래소
        # 부재는 증명이 아니다. 모든 미국 ACK에 대해 3거래소를 전부 조회하고
        # union하며, 하나라도 실패/불완전하면 부재 증명을 보류한다.
        us_orders = [o for o in aged if not _is_kr(o)]
        us_days = sorted({_day(o) for o in us_orders})
        nccs_parts: list[list[dict]] = []
        ccnl_parts: dict[str, list[list[dict]]] = {day: [] for day in us_days}
        nccs_complete = True
        ccnl_complete = {day: True for day in us_days}
        for ex in ("NASD", "NYSE", "AMEX") if us_orders else ():
            n_raw = kis.open_orders(excg=ex)
            n_rows = kis_reconcile.trusted_response_rows(n_raw)
            if n_rows is None:
                errors.append(f"US {ex} nccs untrusted")
                nccs_complete = False
            else:
                nccs_parts.append(n_rows)
            c_all: list[dict] = []
            for day in us_days:
                raw = kis.fills(excg=ex, start=day, end=day)
                rows = kis_reconcile.trusted_response_rows(raw)
                if rows is None:
                    errors.append(f"US {ex} ccnl {day} untrusted")
                    ccnl_complete[day] = False
                else:
                    c_all.extend(rows)
                    ccnl_parts[day].append(rows)
            fill_rows += kis_reconcile.normalize_rows(
                {"rt_cd": "0", "output": n_rows} if n_rows is not None else None,
                {"rt_cd": "0", "output": c_all})
        combined_nccs = ([row for part in nccs_parts for row in part]
                         if nccs_complete else None)
        combined_ccnl = {
            day: ([row for part in ccnl_parts[day] for row in part]
                  if ccnl_complete[day] else None)
            for day in us_days
        }
        for order in us_orders:
            proofs[str(order.get("key") or "")] = {
                "nccs_rows": combined_nccs,
                "ccnl_rows": combined_ccnl.get(_day(order)),
                "holdings": None,
            }

        # 1순위: ODNO 행. 0주 종결도 여기서 즉시 rejected로 닫힌.
        rs = kis_reconcile.resolve_acks_from_rows(fill_rows)

        # 잔고는 주문조회 두 종류 다음에 읽는다. US 3거래소 중 하나라도
        # 실패하면 전체 snapshot을 None으로 남겨 '미보유'로 오판하지 않는다.
        current_open = ledger.open_orders()
        aged = [o for o in current_open
                if o.get("state") in ("submitted", "ack")
                and (o.get("side") or "").upper() in ("BUY", "SELL")
                and now - float(o.get("submitted_at") or 0)
                >= kis_reconcile.ACK_AGE_MIN_S]
        hmaps: dict[str, dict | None] = {}
        fill_prices: dict[str, dict[str, float]] = {}
        if any(_is_kr(o) for o in aged):
            hmaps["KR"] = kis.holdings("KR")
            if hmaps["KR"] is None:
                errors.append("KR balance untrusted")
            if kis.enabled():
                rows = kis.positions_detail("KR")
                if rows is not None:
                    fill_prices["KR"] = {r["code"]: float(r.get("avg") or 0)
                                         for r in rows if float(r.get("avg") or 0) > 0}
        if any(not _is_kr(o) for o in aged):
            merged: dict | None = {}
            avgs: dict[str, float] | None = {}
            for ex in ("NASD", "NYSE", "AMEX"):
                h = kis.holdings("US", excg=ex)
                if h is None:
                    merged = None
                    errors.append(f"US {ex} balance untrusted")
                    break
                merged.update(h)
                if kis.enabled() and avgs is not None:
                    rows = kis.positions_detail("US", excg=ex)
                    if rows is None:
                        avgs = None
                    else:
                        avgs.update({r["code"]: float(r.get("avg") or 0)
                                     for r in rows if float(r.get("avg") or 0) > 0})
            hmaps["US"] = merged
            if avgs is not None:
                fill_prices["US"] = avgs

        for order in aged:
            proof = proofs.get(str(order.get("key") or ""))
            if proof is not None:
                proof["holdings"] = hmaps.get("KR" if _is_kr(order) else "US")

        absence_rs, contradictions = kis_reconcile.resolve_acks_by_absence(
            proofs, now_ts=now, orders=current_open)
        rs += absence_rs
        contradiction_keys = {str(r.get("key") or "") for r in contradictions}
        for item in contradictions:
            previous = ledger.state_of(item["key"]) or {}
            if previous.get("reconcile_reason") == "absence-balance-contradiction":
                continue
            ledger.record_reconcile_meta(
                item["key"], reason="absence-balance-contradiction",
                meta={"source": "absence-proof", "hldg_before": item["hldg_before"],
                      "hldg_now": item["hldg_now"], "side": item["side"],
                      "intended": item["intended"]})
            _notify(f"🚨 주문 대사 모순 — {item['symbol']} "
                    f"잔고 {item['hldg_before']}→{item['hldg_now']} 변했는데 "
                    "미체결·체결내역에 ODNO 없음; 자동 정산 금지",
                    critical=True, category="trade")

        remaining_keys = {
            str(o.get("key") or "") for o in aged
            if o.get("state") in ("submitted", "ack")
            and str(o.get("key") or "") not in contradiction_keys
        }
        if remaining_keys:
            rs += kis_reconcile.resolve_acks_by_balance(
                hmaps, fill_prices=fill_prices, only_keys=remaining_keys)

        notice_context = _reconcile_notice_context(
            rs, initial_open, hmaps, now=now) if rs else {}
        for r in rs:
            context = notice_context.get(str(r.get("key") or ""), {})
            notice = _format_reconcile_notice(r, context)
            if notice is None:
                continue
            # 포지션 차감/소멸은 kis_accounting.apply_sell_fill이 실제 체결수량으로
            # 이미 처리한다. 주문 1건이 full-fill이어도 절반익절일 수 있으므로
            # residual==0만 보고 포지션 전체를 close하면 남은 보유가 무보호가 된다.
            _notify(notice[0], critical=notice[1], category="trade")
        if errors:
            _record_failure("; ".join(sorted(set(errors))))
        else:
            _record_success()
        return rs
    except Exception as exc:
        _record_failure(type(exc).__name__)
        return []                                  # 대사 실패가 부팅을 못 깨게


def boot_reconcile(excgs: tuple[str, ...] = ("NASD", "NYSE", "AMEX")) -> dict:
    """부팅 대사 1회. 반환 {ok, unknowns, resolved, low, results}.

    · UNKNOWN이 없으면 즉시 ok(게이트 열림).
    · 있으면 거래소별 nccs+ccnl을 모아 대사 — HIGH는 해소, LOW는 잠금 유지+P0.
    · 조회 실패(None)는 fail-closed: ok=False, 게이트 닫힘(재시도는 호출부 몫).
    """
    _STATE["done"] = False
    health = ledger.corruption_status()
    if not health["healthy"]:
        _notify(f"🚨 주문 원장 손상({health['lines']}) — 신규 매매 전면 차단, "
                "수동 복구 필요", critical=True)
        return {"ok": False, "unknowns": 0, "resolved": 0, "low": 0,
                "results": [], "ack_resolved": 0,
                "ledger_corrupt_lines": health["lines"]}
    acks = _resolve_acks()                        # 접수(ack)→체결 확정(잔고대사)
    stale = ledger.promote_stale_submitted(
        kis_reconcile.ACK_AGE_MIN_S)              # 크래시 창 submitted→UNKNOWN 잠금
    unknowns = pending_unknowns()
    if not unknowns:
        _STATE.update(done=True, low=0)
        return {"ok": True, "unknowns": 0, "resolved": 0, "low": 0,
                "results": [], "ack_resolved": len(acks),
                "stale_promoted": len(stale)}

    def _is_kr(u: dict) -> bool:
        return (u.get("market") == "KR"
                or kis.market_of_symbol(u.get("symbol", "")) == "KR")

    # 국내(KR) UNKNOWN — 잔고 delta 기반 대사(reconcile_unknowns_kr). SELL만 정확
    #   full-fill 확정 시 자동해소, 나머지(BUY·부분·불명·조회실패)는 LOW 잠금 유지.
    #   국내 nccs 모의 미지원·costbook 미배선이라 잔고를 유일 근거로 쓴다(안전 정리).
    kr_results = []
    if any(_is_kr(u) for u in unknowns):
        kr_results = kis_reconcile.reconcile_unknowns_kr(kis.domestic_balance())

    all_nccs_rows, all_ccnl_rows = [], []

    def _notify_low(rs):
        for r in rs:
            if r.get("confidence") != ledger.CONF_LOW or r.get("already_low"):
                continue
            why = r.get("kr_reason") or f"후보 {r.get('candidates')}"
            _notify(f"🚨 부팅 대사 LOW — {r.get('symbol')}({why}) "
                    f"잠금 유지, 수동 검토 필요(MANUAL_REVIEW)", critical=True)

    # 미국(US) UNKNOWN — 필요한 거래소만 조회(유량 절약). meta.excg 없으면 전체.
    us = [u for u in unknowns if not _is_kr(u)]
    if us:
        need = {u.get("excg") for u in us if u.get("excg")} or set(excgs)
        for ex in sorted(need):
            n = kis.open_orders(excg=ex)
            c = kis.fills(excg=ex)
            if n is None or c is None:             # 조회 실패 — fail-closed
                _notify(f"🚨 부팅 대사 조회 실패({ex}) — 매매 게이트 닫힌 채 유지",
                        critical=True)
                # KR은 이미 대사됨(원장 반영) — 결과·알림 보존(관측성). 게이트만 닫는다.
                _notify_low(kr_results)
                kr_low = sum(1 for r in kr_results
                             if r.get("confidence") == ledger.CONF_LOW)
                return {"ok": False, "unknowns": len(unknowns),
                        "resolved": sum(1 for r in kr_results
                                        if r.get("confidence") == ledger.CONF_HIGH),
                        "low": kr_low, "results": kr_results,
                        "ack_resolved": len(acks),
                        "stale_promoted": len(stale)}
            all_nccs_rows += (n.get("output") or [])
            all_ccnl_rows += (c.get("output") or [])

    # US는 nccs/ccnl per-order 매칭(reconcile_unknowns는 KR을 건너뛴다).
    results = kis_reconcile.reconcile_unknowns(
        {"rt_cd": "0", "output": all_nccs_rows},
        {"rt_cd": "0", "output": all_ccnl_rows}) + kr_results
    low = [r for r in results if r.get("confidence") == ledger.CONF_LOW]
    resolved = [r for r in results if r.get("confidence") == ledger.CONF_HIGH]
    _notify_low(results)                          # already_low는 내부에서 억제
    if resolved:
        _notify("🔁 부팅 대사 — " + ", ".join(
            f"{r.get('symbol')} {r.get('state')}(체결 {r.get('filled')})"
            for r in resolved), critical=True)
    _STATE.update(done=True, low=len(low))
    return {"ok": True, "unknowns": len(unknowns), "resolved": len(resolved),
            "low": len(low), "results": results, "ack_resolved": len(acks),
            "stale_promoted": len(stale)}


def trading_allowed() -> bool:
    """신규 매매 허용 게이트 — 부팅 대사가 이 프로세스에서 완료됐어야 True.
    (LOW 잔존은 종목별 잠금이 이미 막으므로 전체 게이트는 열되, 그 종목은 잠김.)"""
    return bool(_STATE["done"] and ledger.ledger_healthy())
