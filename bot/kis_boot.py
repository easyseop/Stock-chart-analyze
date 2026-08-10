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

import fcntl
import json
import os
import time

from bot import kis, kis_reconcile, ledger

# 모듈 상태 + 프로세스 간 공유 스냅샷. /diagnosis은 telegram 프로세스에서
# 읽으므로 메모리 dict만으로는 buyloop/sentinel의 성공·실패가 보이지 않는다.
_STATE = {"done": False, "low": 0, "last_success_at": None,
          "failure_streak": 0, "last_error": "", "failure_alerted": False}
RECONCILE_FAILURE_ALERT_N = int(
    os.environ.get("RECONCILE_FAILURE_ALERT_N", "6") or 6)


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
            state = {**_STATE, **_read_status_unlocked(path)}
            if success:
                state.update(last_success_at=time.time(), failure_streak=0,
                             last_error="", failure_alerted=False)
            else:
                streak = int(state.get("failure_streak") or 0) + 1
                alerted = bool(state.get("failure_alerted"))
                should_alert = streak >= max(1, RECONCILE_FAILURE_ALERT_N) and not alerted
                state.update(failure_streak=streak, last_error=str(error)[:160],
                             failure_alerted=alerted or should_alert)
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
            _STATE["failure_alerted"] = bool(
                _STATE.get("failure_alerted") or should_alert)
        return dict(_STATE), should_alert


def reconcile_health() -> dict:
    """진단용 대사 상태. 시크릿이나 주문 상세를 포함하지 않는다."""
    state = {**_STATE, **_read_status_unlocked(_status_path())}
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
        _notify(f"🚨 KIS 주문 대사 {state['failure_streak']}회 연속 실패 — "
                "조회 실패를 부재로 판정하지 않고 주문 잠금 유지",
                critical=True, category="trade")


def _notify(text: str, *, critical: bool = False,
            category: str | None = None) -> None:
    try:
        from bot import notify
        notify.send(text, critical=critical, category=category)
    except Exception:
        pass


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
        aged = [o for o in ledger.open_orders()
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
            return time.strftime("%Y%m%d", time.localtime(stamp))

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

        # 해외: 기본 7일 ccnl은 연속키로 TAP 당일 행을 놓쳤다. 접수일 단일
        # 조회로 페이지 완전성을 검증하고 주문별 근거를 분리한다.
        us_orders = [o for o in aged if not _is_kr(o)]
        for ex in sorted({str(o.get("excg") or "NASD") for o in us_orders}):
            ex_orders = [o for o in us_orders if str(o.get("excg") or "NASD") == ex]
            n_raw = kis.open_orders(excg=ex)
            n_rows = kis_reconcile.trusted_response_rows(n_raw)
            if n_rows is None:
                errors.append(f"US {ex} nccs untrusted")
            c_by_day: dict[str, list[dict] | None] = {}
            c_all: list[dict] = []
            for day in sorted({_day(o) for o in ex_orders}):
                raw = kis.fills(excg=ex, start=day, end=day)
                rows = kis_reconcile.trusted_response_rows(raw)
                c_by_day[day] = rows
                if rows is None:
                    errors.append(f"US {ex} ccnl {day} untrusted")
                else:
                    c_all.extend(rows)
            fill_rows += kis_reconcile.normalize_rows(
                {"rt_cd": "0", "output": n_rows} if n_rows is not None else None,
                {"rt_cd": "0", "output": c_all})
            for order in ex_orders:
                proofs[str(order.get("key") or "")] = {
                    "nccs_rows": n_rows, "ccnl_rows": c_by_day.get(_day(order)),
                    "holdings": None,
                }

        # 1순위: ODNO 행. 0주 종결도 여기서 즉시 rejected로 닫힌.
        rs = kis_reconcile.resolve_acks_from_rows(fill_rows)

        # 잔고는 주문조회 두 종류 다음에 읽는다. US 3거래소 중 하나라도
        # 실패하면 전체 snapshot을 None으로 남겨 '미보유'로 오판하지 않는다.
        aged = [o for o in ledger.open_orders()
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
            proofs, now_ts=now)
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
                    f"잔고 {item['hldg_before']}→{item['hldg_now']} 부러운데 "
                    "미체결·체결내역에 ODNO 없음; 자동 정산 금지",
                    critical=True, category="trade")

        remaining_keys = {
            str(o.get("key") or "") for o in ledger.open_orders()
            if o.get("state") in ("submitted", "ack")
            and str(o.get("key") or "") not in contradiction_keys
        }
        rs += kis_reconcile.resolve_acks_by_balance(
            hmaps, fill_prices=fill_prices, only_keys=remaining_keys)

        for r in rs:
            try:
                filled = int(r.get("filled") or 0)
            except (TypeError, ValueError):
                continue
            if filled <= 0:
                if r.get("state") == "rejected":
                    side_name = "매도" if r.get("side") == "SELL" else "매수"
                    via = "부재 증명" if r.get("via") == "absence-proof" else "브로커 종결 행"
                    qty = int(r.get("residual") or r.get("intended") or 0)
                    reason = str(r.get("broker_reason") or "사유 미상")
                    suffix = " · 보호는 유지" if r.get("side") == "SELL" else ""
                    _notify(f"⚠️ {side_name} 거절 종결({via}) — "
                            f"{r.get('symbol')} {qty}주 · {reason}{suffix}",
                            critical=(r.get("side") == "SELL"), category="trade")
                continue
            # 포지션 차감/소멸은 kis_accounting.apply_sell_fill이 실제 체결수량으로
            # 이미 처리한다. 주문 1건이 full-fill이어도 절반익절일 수 있으므로
            # residual==0만 보고 포지션 전체를 close하면 남은 보유가 무보호가 된다.
            _notify(f"✅ 체결 확정(잔고대사) — {r.get('symbol')} "
                    f"{'매수' if r.get('side') == 'BUY' else '매도'} "
                    f"{filled}주", critical=(r.get("side") == "SELL"),
                    category="trade")
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
