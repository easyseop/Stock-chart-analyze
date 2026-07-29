"""L1 하향 전 기술 게이트를 읽기 전용으로 판정한다.

이 모듈은 kill-switch를 내리거나 주문을 전송하지 않는다. 서버의 현재 원장·
포지션·총시드·heartbeat와 운영자가 남긴 관찰 증거를 모아 ``GO/NO-GO`` 근거만
출력한다. 최종 결과가 GO여도 ``bot.kill --lower``는 별도 사용자 승인과
operator ack 없이는 실행하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from typing import Any


DEFAULT_EVIDENCE_PATH = (
    "/var/lib/stock-l1-readiness/evidence.json"
)
FRESH_EVIDENCE_HOURS = 72.0
MIN_STALL_SHADOW_DAYS = 7.0
REQUIRED_HALF_RATCHET_SYMBOLS = frozenset({
    "AQN", "CAG", "GPK", "KKR", "LW", "SNN", "STE", "VRSK", "WDAY",
})
REQUIRED_FROZEN_REVIEW_SYMBOLS = frozenset({
    "AQN", "CAG", "GPK", "LW", "SNN", "VRSK",
})
_FROZEN_DECISIONS = frozenset({"keep_close_only", "unfreeze_approved"})


def _finite(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def _gate(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _shadow_days(evidence: dict, *, now: datetime) -> float | None:
    started = _parse_time(evidence.get("stall_shadow_started_at"))
    if started is None or started > now:
        return None
    return max(0.0, (now - started).total_seconds() / 86400.0)


def evaluate(snapshot: dict, evidence: dict,
             *, now: datetime | None = None) -> dict:
    """수집된 상태를 L1 하향 전 fail-closed 게이트로 판정한다."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    gates: list[dict] = []

    env = str(snapshot.get("kis_env") or "").lower()
    gates.append(_gate(
        "mock_environment", env == "mock",
        f"KIS_ENV={env or 'unknown'} (mock만 허용)"))

    level = snapshot.get("kill_level")
    gates.append(_gate(
        "l1_latched", level == 1,
        f"현재 L{level if level is not None else '?'} (점검 중 L1 유지 필요)"))
    permissions_ok = (
        snapshot.get("buy_new_allowed") is False
        and snapshot.get("protect_sell_allowed") is True
    )
    gates.append(_gate(
        "l1_permissions", permissions_ok,
        "buy_new=False·protect_sell=True 필요"))

    ledger_ok = snapshot.get("ledger_healthy") is True
    gates.append(_gate(
        "ledger_healthy", ledger_ok,
        f"주문 원장 healthy={snapshot.get('ledger_healthy')}"))

    local_open = snapshot.get("local_open_orders")
    broker_open = snapshot.get("broker_open_orders")
    gates.append(_gate(
        "no_open_orders",
        local_open == 0 and broker_open == 0,
        f"원장 열린주문={local_open!r}, 브로커 열린주문={broker_open!r}"))

    unknowns = snapshot.get("unresolved_unknowns")
    unaccounted = snapshot.get("unaccounted_buy_fills")
    gates.append(_gate(
        "orders_fully_reconciled",
        unknowns == 0 and unaccounted == 0,
        f"UNKNOWN={unknowns!r}, 미회계 BUY={unaccounted!r}"))

    held_cost = _finite(snapshot.get("open_cost_krw"))
    limit = _finite(snapshot.get("operating_limit_krw"))
    budget_ok = (
        snapshot.get("costbook_healthy") is True
        and held_cost is not None and limit is not None and limit > 0
        and held_cost <= limit + 1e-6
    )
    gates.append(_gate(
        "operating_budget",
        budget_ok,
        f"운용원가={held_cost!r}, 5% 완충 후 한도={limit!r}"))

    positions_match = snapshot.get("positions_match_broker")
    gates.append(_gate(
        "positions_match_broker", positions_match is True,
        f"브로커·보호원장·costbook 수량 일치={positions_match!r}"))

    age = _finite(snapshot.get("heartbeat_age_s"))
    gates.append(_gate(
        "heartbeat_fresh",
        age is not None and age <= 60.0,
        f"sentinel heartbeat age={age!r}초 (60초 이하 필요)"))

    fallback = snapshot.get("fallback_enabled")
    gates.append(_gate(
        "fallback_still_shadow",
        fallback is False,
        f"ORACLE_SIGNAL_FALLBACK_ENABLED={1 if fallback is True else 0 if fallback is False else '?'}"))

    stall_mode = str(snapshot.get("stall_exit_mode") or "").lower()
    days = _shadow_days(evidence, now=current)
    gates.append(_gate(
        "stall_shadow_observed",
        stall_mode == "shadow" and days is not None
        and days >= MIN_STALL_SHADOW_DAYS,
        f"mode={stall_mode or 'unknown'}, 관찰={None if days is None else round(days, 2)}일 "
        f"(최소 {MIN_STALL_SHADOW_DAYS:.0f}일)"))

    evidence_half = {
        str(code).upper() for code in (
            evidence.get("half_ratchet_symbols") or [])
        if str(code).strip()
    }
    required_half = REQUIRED_HALF_RATCHET_SYMBOLS
    verified_half = {
        str(code).upper() for code in (
            snapshot.get("half_ratchet_verified") or [])
        if str(code).strip()
    }
    missing_half = sorted(required_half - verified_half)
    gates.append(_gate(
        "half_ratchets_durable",
        evidence_half == required_half and not missing_half,
        f"증거목록 일치={evidence_half == required_half}, "
        f"검증 {len(verified_half & required_half)}/{len(required_half)}, "
        f"미확인={missing_half}"))

    frozen = {
        str(code).upper() for code in (
            snapshot.get("frozen_symbols") or [])
        if str(code).strip()
    }
    review_symbols = {
        str(code).upper() for code in (
            evidence.get("frozen_review_symbols") or [])
        if str(code).strip()
    }
    decisions = {
        str(code).upper(): str(value).strip().lower()
        for code, value in (
            evidence.get("frozen_decisions") or {}).items()
    }
    undecided = sorted(code for code in REQUIRED_FROZEN_REVIEW_SYMBOLS
                       if decisions.get(code) not in _FROZEN_DECISIONS)
    state_mismatch = sorted(
        code for code in REQUIRED_FROZEN_REVIEW_SYMBOLS
        if (
            decisions.get(code) == "keep_close_only" and code not in frozen
        ) or (
            decisions.get(code) == "unfreeze_approved" and code in frozen
        ))
    unexpected_frozen = sorted(frozen - REQUIRED_FROZEN_REVIEW_SYMBOLS)
    gates.append(_gate(
        "frozen_symbols_reviewed",
        review_symbols == REQUIRED_FROZEN_REVIEW_SYMBOLS
        and not undecided and not state_mismatch and not unexpected_frozen,
        f"증거목록 일치={review_symbols == REQUIRED_FROZEN_REVIEW_SYMBOLS}, "
        f"현재동결={sorted(frozen)}, 미결정={undecided}, "
        f"결정-상태불일치={state_mismatch}, 추가동결={unexpected_frozen}"))

    sessions = evidence.get("oracle_brain_sessions") or {}
    kr_sessions = int(_finite(sessions.get("KR")) or 0)
    us_sessions = int(_finite(sessions.get("US")) or 0)
    gates.append(_gate(
        "oracle_brain_both_markets",
        kr_sessions >= 1 and us_sessions >= 1,
        f"관찰 세션 KR={kr_sessions}, US={us_sessions}"))

    outage = evidence.get("github_outage") or {}
    outage_minutes = _finite(outage.get("minutes"))
    outage_orders = _finite(outage.get("new_orders"))
    gates.append(_gate(
        "github_outage_injection",
        outage_minutes is not None and outage_minutes >= 60.0
        and outage_orders == 0,
        f"장애주입={outage_minutes!r}분, 신규주문={outage_orders!r}"))

    observed_at = _parse_time(evidence.get("observed_at"))
    evidence_age_h = (
        None if observed_at is None or observed_at > current
        else (current - observed_at).total_seconds() / 3600.0
    )
    gates.append(_gate(
        "observation_evidence_fresh",
        evidence_age_h is not None
        and evidence_age_h <= FRESH_EVIDENCE_HOURS,
        f"관찰 증거 나이={None if evidence_age_h is None else round(evidence_age_h, 2)}시간 "
        f"(최대 {FRESH_EVIDENCE_HOURS:.0f}시간)"))

    blockers = [g for g in gates if not g["ok"]]
    return {
        "ready_for_operator_review": not blockers,
        "operator_approval_still_required": True,
        "checked_at": current.isoformat(),
        "gates": gates,
        "blockers": blockers,
    }


def load_evidence(path: str | None = None) -> dict:
    evidence_path = path or os.environ.get(
        "L1_READINESS_EVIDENCE_PATH", DEFAULT_EVIDENCE_PATH)
    try:
        with open(evidence_path, encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _broker_open_count(response: dict | None) -> int | None:
    if not response or response.get("rt_cd") != "0":
        return None
    if "output" in response:
        rows = response["output"]
    elif "output1" in response:
        rows = response["output1"]
    else:
        return None
    if not isinstance(rows, list):
        return None
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            return None
        remaining = None
        for key in ("nccs_qty", "rmnd_qty", "ord_remn_qty"):
            if row.get(key) not in (None, ""):
                remaining = _finite(row.get(key))
                break
        if remaining is None:
            ordered = _finite(row.get("ord_qty") or row.get("ft_ord_qty"))
            filled = _finite(row.get("tot_ccld_qty") or row.get("ft_ccld_qty"))
            if ordered is not None and filled is not None:
                remaining = max(0.0, ordered - filled)
        if remaining is None:
            return None
        if remaining > 0:
            count += 1
    return count


def collect_runtime(*, fetch_broker: bool = False,
                    evidence: dict | None = None) -> dict:
    """현재 서버 상태를 읽는다. ``fetch_broker``도 조회 API만 사용한다."""
    from bot import (costbook, envelope, heartbeat, kill, kis, kis_positions,
                     ledger, ownership, settings)

    folded_orders = ledger.orders_for()
    local_positions = kis_positions.load()
    budget = costbook.budget_snapshot()
    frozen_state = ownership.frozen_state()

    snapshot: dict[str, Any] = {
        "kis_env": kis.ENV,
        "kill_level": kill.level(),
        "buy_new_allowed": kill.allows("buy_new"),
        "protect_sell_allowed": kill.allows("protect_sell"),
        "ledger_healthy": ledger.ledger_healthy(),
        "local_open_orders": len(ledger.open_orders()),
        "broker_open_orders": None,
        "unresolved_unknowns": sum(
            1 for order in folded_orders
            if order.get("state") == "unknown" and not order.get("reconciled")),
        "unaccounted_buy_fills": sum(
            1 for order in folded_orders
            if str(order.get("side") or "").upper() == "BUY"
            and int(order.get("filled") or 0) > int(order.get("accounted") or 0)),
        "costbook_healthy": budget is not None,
        "open_cost_krw": None if budget is None else budget.get("total"),
        "operating_limit_krw": envelope.operating_limit_krw(),
        "positions_match_broker": None,
        "heartbeat_age_s": heartbeat.age_s(),
        "fallback_enabled": (
            os.environ.get("ORACLE_SIGNAL_FALLBACK_ENABLED", "0") == "1"),
        "stall_exit_mode": settings.STALL_EXIT_MODE,
        "half_ratchet_verified": [],
        "frozen_symbols": sorted(frozen_state),
    }

    required = REQUIRED_HALF_RATCHET_SYMBOLS
    snapshot["half_ratchet_verified"] = sorted(
        code for code in required
        if code in local_positions
        and local_positions[code].get("half_done") is True
        and _finite(local_positions[code].get("stop")) is not None
        and _finite(local_positions[code].get("entry")) is not None
        and float(local_positions[code]["stop"])
        >= float(local_positions[code]["entry"]))

    if not fetch_broker:
        return snapshot

    broker_holdings: dict[str, int] = {}
    broker_complete = True
    kr = kis.holdings("KR")
    if kr is None:
        broker_complete = False
    else:
        broker_holdings.update(kr)
    for exchange in ("NASD", "NYSE", "AMEX"):
        us = kis.holdings("US", excg=exchange)
        if us is None:
            broker_complete = False
            break
        broker_holdings.update(us)

    local_qty = {
        code: int(row.get("qty") or 0)
        for code, row in local_positions.items()
        if int(row.get("qty") or 0) > 0
    }
    costbook_qty = {
        code: costbook.open_qty(code)
        for code in set(local_qty) | set(broker_holdings)
    }
    snapshot["positions_match_broker"] = (
        broker_complete
        and local_qty == broker_holdings
        and local_qty == {
            code: qty for code, qty in costbook_qty.items() if qty > 0
        }
    )

    open_counts: list[int] = []
    for response in (
        kis.domestic_open_orders(),
        *(kis.open_orders(excg=exchange)
          for exchange in ("NASD", "NYSE", "AMEX")),
    ):
        count = _broker_open_count(response)
        if count is None:
            open_counts = []
            break
        open_counts.append(count)
    snapshot["broker_open_orders"] = (
        sum(open_counts) if len(open_counts) == 4 else None)
    return snapshot


def render_text(report: dict) -> str:
    lines = []
    for gate in report.get("gates") or []:
        lines.append(
            f"[{'PASS' if gate.get('ok') else 'BLOCK'}] "
            f"{gate.get('name')}: {gate.get('detail')}")
    verdict = "GO — operator 승인 검토 가능" if report.get(
        "ready_for_operator_review") else "NO-GO — L1 유지"
    lines.append(f"\n결과: {verdict}")
    lines.append("주의: GO도 kill-switch를 자동으로 내리지 않는다.")
    return "\n".join(lines)
