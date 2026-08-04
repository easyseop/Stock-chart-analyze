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
import tempfile
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
SCOPES = frozenset({"strict", "l0"})
_L0_INFORMATIONAL_GATES = frozenset({
    "stall_shadow_observed",
    "half_ratchets_durable",
    "frozen_symbols_reviewed",
    "oracle_brain_both_markets",
    "github_outage_injection",
    "observation_evidence_fresh",
    "baseline_path_persistent",
})
_STRICT_INFORMATIONAL_GATES = frozenset({
    "frozen_symbols_preserved",
    "baseline_path_persistent",
})


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


def _gate(name: str, ok: bool, detail: str, *, blocking: bool) -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "blocking": bool(blocking),
        "detail": str(detail),
    }


def _shadow_days(evidence: dict, *, now: datetime) -> float | None:
    started = _parse_time(evidence.get("stall_shadow_started_at"))
    if started is None or started > now:
        return None
    return max(0.0, (now - started).total_seconds() / 86400.0)


def evaluate(snapshot: dict, evidence: dict, *, scope: str = "strict",
             now: datetime | None = None) -> dict:
    """수집된 상태를 L1 하향 전 fail-closed 게이트로 판정한다.

    ``strict``는 기존 기능 관찰을 모두 차단 조건으로 유지한다. ``l0``는 일반
    GitHub 신호의 제한적 mock 신규매수에 직접 필요한 조건만 차단하고,
    stall-live·fallback 1·동결 해제용 관찰 증거는 정보로만 표시한다.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown readiness scope: {scope}")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    gates: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        informational = (
            name in _L0_INFORMATIONAL_GATES if scope == "l0"
            else name in _STRICT_INFORMATIONAL_GATES
        )
        gates.append(_gate(
            name, ok, detail, blocking=not informational))

    env = str(snapshot.get("kis_env") or "").lower()
    add(
        "mock_environment", env == "mock",
        f"KIS_ENV={env or 'unknown'} (mock만 허용)")

    level = snapshot.get("kill_level")
    add(
        "l1_latched", level == 1,
        f"현재 L{level if level is not None else '?'} (점검 중 L1 유지 필요)")
    permissions_ok = (
        snapshot.get("buy_new_allowed") is False
        and snapshot.get("protect_sell_allowed") is True
    )
    add(
        "l1_permissions", permissions_ok,
        "buy_new=False·protect_sell=True 필요")

    ledger_ok = snapshot.get("ledger_healthy") is True
    add(
        "ledger_healthy", ledger_ok,
        f"주문 원장 healthy={snapshot.get('ledger_healthy')}")

    local_open = snapshot.get("local_open_orders")
    broker_open = snapshot.get("broker_open_orders")
    add(
        "no_open_orders",
        local_open == 0 and broker_open == 0,
        f"원장 열린주문={local_open!r}, 브로커 열린주문={broker_open!r}")

    unknowns = snapshot.get("unresolved_unknowns")
    unaccounted = snapshot.get("unaccounted_buy_fills")
    add(
        "orders_fully_reconciled",
        unknowns == 0 and unaccounted == 0,
        f"UNKNOWN={unknowns!r}, 미회계 BUY={unaccounted!r}")

    held_cost = _finite(snapshot.get("open_cost_krw"))
    limit = _finite(snapshot.get("operating_limit_krw"))
    budget_ok = (
        snapshot.get("costbook_healthy") is True
        and held_cost is not None and limit is not None and limit > 0
        and held_cost <= limit + 1e-6
    )
    add(
        "operating_budget",
        budget_ok,
        f"운용원가={held_cost!r}, 5% 완충 후 한도={limit!r}")

    positions_match = snapshot.get("positions_match_broker")
    add(
        "positions_match_broker", positions_match is True,
        f"브로커·보호원장·costbook 수량 일치={positions_match!r}")

    # 2026-07-31 실사고: baseline이 /tmp에서 재부팅으로 소실 → fail-closed로
    #   전 종목 매수 거부인데 이 체커는 blockers=[]를 보고했다. 사각지대 봉합.
    armed = snapshot.get("ownership_armed")
    base_n = snapshot.get("baseline_symbol_count")
    add(
        "ownership_armed", armed is True,
        (f"IS2 baseline 캡처됨({base_n}종목)" if armed is True
         else "IS2 baseline 미캡처 — 전 종목 매수 거부 상태"
              "(scripts/kis_arm.py로 arming 필요)"))
    volatile = snapshot.get("baseline_path_volatile")
    add(
        "baseline_path_persistent", volatile is False,
        f"baseline 경로 휘발성={volatile!r} — 휘발성(tempdir)이면 재부팅 시 "
        "소실→전 매수 거부(USER_BASELINE_PATH를 영속 경로로)")

    age = _finite(snapshot.get("heartbeat_age_s"))
    add(
        "heartbeat_fresh",
        age is not None and age <= 60.0,
        f"sentinel heartbeat age={age!r}초 (60초 이하 필요)")

    fallback = snapshot.get("fallback_enabled")
    add(
        "fallback_still_shadow",
        fallback is False,
        f"ORACLE_SIGNAL_FALLBACK_ENABLED={1 if fallback is True else 0 if fallback is False else '?'}")

    stall_mode = str(snapshot.get("stall_exit_mode") or "").lower()
    add(
        "stall_mode_shadow", stall_mode == "shadow",
        f"STALL_EXIT_MODE={stall_mode or 'unknown'} (shadow 유지 필요)")

    days = _shadow_days(evidence, now=current)
    add(
        "stall_shadow_observed",
        days is not None and days >= MIN_STALL_SHADOW_DAYS,
        f"관찰={None if days is None else round(days, 2)}일 "
        f"(stall live 전 최소 {MIN_STALL_SHADOW_DAYS:.0f}일)")

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
    add(
        "half_ratchets_durable",
        evidence_half == required_half and not missing_half,
        f"증거목록 일치={evidence_half == required_half}, "
        f"검증 {len(verified_half & required_half)}/{len(required_half)}, "
        f"미확인={missing_half}")

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
    add(
        "frozen_symbols_reviewed",
        review_symbols == REQUIRED_FROZEN_REVIEW_SYMBOLS
        and not undecided and not state_mismatch and not unexpected_frozen,
        f"증거목록 일치={review_symbols == REQUIRED_FROZEN_REVIEW_SYMBOLS}, "
        f"현재동결={sorted(frozen)}, 미결정={undecided}, "
        f"결정-상태불일치={state_mismatch}, 추가동결={unexpected_frozen}")

    missing_frozen = sorted(REQUIRED_FROZEN_REVIEW_SYMBOLS - frozen)
    add(
        "frozen_symbols_preserved", not missing_frozen,
        f"제한적 L0에서 close-only 유지 필요, 누락={missing_frozen}")

    sessions = evidence.get("oracle_brain_sessions") or {}
    kr_sessions = int(_finite(sessions.get("KR")) or 0)
    us_sessions = int(_finite(sessions.get("US")) or 0)
    add(
        "oracle_brain_both_markets",
        kr_sessions >= 1 and us_sessions >= 1,
        f"관찰 세션 KR={kr_sessions}, US={us_sessions}")

    outage = evidence.get("github_outage") or {}
    outage_minutes = _finite(outage.get("minutes"))
    outage_orders = _finite(outage.get("new_orders"))
    add(
        "github_outage_injection",
        outage_minutes is not None and outage_minutes >= 60.0
        and outage_orders == 0,
        f"장애주입={outage_minutes!r}분, 신규주문={outage_orders!r}")

    observed_at = _parse_time(evidence.get("observed_at"))
    evidence_age_h = (
        None if observed_at is None or observed_at > current
        else (current - observed_at).total_seconds() / 3600.0
    )
    add(
        "observation_evidence_fresh",
        evidence_age_h is not None
        and evidence_age_h <= FRESH_EVIDENCE_HOURS,
        f"관찰 증거 나이={None if evidence_age_h is None else round(evidence_age_h, 2)}시간 "
        f"(최대 {FRESH_EVIDENCE_HOURS:.0f}시간)")

    trade_stage = str(snapshot.get("trade_stage") or "").strip()
    allowed_symbols = {
        str(code).upper() for code in (
            snapshot.get("allowed_symbols") or [])
        if str(code).strip()
    }
    allow_buy = snapshot.get("allow_buy_enabled")
    orders_enabled = snapshot.get("orders_enabled")
    # 2026-08-03 완전 L0 승인: allowlist는 더 이상 필수가 아니다(미러는 어차피
    #   autopaper 진입 종목만 산다). 설정돼 있으면 정보로만 표시.
    limited_l0_ok = (
        trade_stage == "mirror"
        and allow_buy is True
        and orders_enabled is True
    )
    # allowlist 없이 여는 근거는 "미러가 autopaper 진입만 산다"이다. 그 게이트가
    #   꺼져 있으면 근거가 사라지므로 차단한다(Codex 미러 P1-3: 환경변수 하나로
    #   조용히 원시 신호 직접매수로 돌아가는데 readiness가 GO를 냈다).
    parity = snapshot.get("mirror_requires_autopaper")
    add(
        "mirror_parity_enforced",
        parity is True or bool(allowed_symbols),
        f"MIRROR_REQUIRES_AUTOPAPER={parity!r} — allowlist 없이 열려면 True 필요")

    add(
        "limited_l0_fence", limited_l0_ok,
        f"TRADE_STAGE={trade_stage or 'unknown'}, "
        f"ALLOWED_SYMBOLS={sorted(allowed_symbols) or '(없음=미러 전 종목)'}, "
        f"ALLOW_BUY={1 if allow_buy is True else 0 if allow_buy is False else '?'}, "
        f"KIS_ORDERS_ENABLED={1 if orders_enabled is True else 0 if orders_enabled is False else '?'}")

    blockers = [g for g in gates if g["blocking"] and not g["ok"]]
    informational = [g for g in gates if not g["blocking"] and not g["ok"]]
    return {
        "scope": scope,
        "ready_for_operator_review": not blockers,
        "operator_approval_still_required": True,
        "checked_at": current.isoformat(),
        "context": {
            "trade_stage": trade_stage or None,
            "allowed_symbols": sorted(allowed_symbols),
            "position_counts_by_sleeve": snapshot.get(
                "position_counts_by_sleeve"),
        },
        "gates": gates,
        "blockers": blockers,
        "informational_findings": informational,
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


def _relevant_open_order_markets(
        local_positions: dict[str, dict],
        local_open_orders: list[dict],
        allowed_symbols: set[str],
        *,
        market_of_symbol) -> set[str]:
    """자동매수/보유 범위에 해당하는 시장만 미체결 조회 대상으로 고른다.

    KIS mock은 국내 미체결 API를 제공하지 않는다. 미국 종목만 보유·허용하고
    로컬 KR 주문도 없는데 이 미지원 응답 때문에 미국 제한적 L0까지 막지 않도록
    범위를 좁힌다. 반대로 KR 포지션·열린 주문·allowlist 중 하나라도 있으면 국내
    응답 실패를 계속 fail-closed로 처리한다.
    """
    markets: set[str] = set()

    def add(symbol: object, row: dict | None = None) -> None:
        item = row or {}
        market = str(item.get("market") or "").upper()
        ccy = str(item.get("ccy") or "").upper()
        if market == "KR" or ccy == "KRW":
            markets.add("KR")
            return
        if market == "US" or ccy == "USD":
            markets.add("US")
            return
        code = str(symbol or "").strip().upper()
        if code:
            markets.add(str(market_of_symbol(code) or "").upper())

    for code, row in local_positions.items():
        try:
            qty = int(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            add(code, row)
    for order in local_open_orders:
        add(order.get("symbol"), order)
    for code in allowed_symbols:
        add(code)
    markets.discard("")
    return markets or {"KR", "US"}   # 범위조차 증명 못 하면 양쪽 모두 조회


def collect_runtime(*, fetch_broker: bool = False,
                    evidence: dict | None = None) -> dict:
    """현재 서버 상태를 읽는다. ``fetch_broker``도 조회 API만 사용한다."""
    from bot import (costbook, envelope, heartbeat, kill, kis, kis_positions,
                     ledger, ownership, rollout, settings)

    folded_orders = ledger.orders_for()
    local_positions = kis_positions.load()
    budget = costbook.budget_snapshot()
    frozen_state = ownership.frozen_state()
    baseline_syms = ownership.baseline()
    tmp_root = os.path.realpath(tempfile.gettempdir())
    baseline_volatile = os.path.realpath(
        ownership.baseline_path()).startswith(tmp_root + os.sep)
    allowed_symbols = rollout.allowed_symbols() or set()   # env 우선, 파일 폴백
    position_counts = {"A": 0, "B": 0}
    for row in local_positions.values():
        try:
            qty = int(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        sleeve = "B" if str(row.get("sleeve") or "A").upper() == "B" else "A"
        position_counts[sleeve] += 1

    open_orders = ledger.open_orders()
    snapshot: dict[str, Any] = {
        "kis_env": kis.ENV,
        "kill_level": kill.level(),
        "buy_new_allowed": kill.allows("buy_new"),
        "protect_sell_allowed": kill.allows("protect_sell"),
        "ledger_healthy": ledger.ledger_healthy(),
        "local_open_orders": len(open_orders),
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
        "ownership_armed": baseline_syms is not None,
        "baseline_symbol_count": (
            None if baseline_syms is None else len(baseline_syms)),
        "baseline_path_volatile": baseline_volatile,
        "heartbeat_age_s": heartbeat.age_s(),
        "fallback_enabled": (
            os.environ.get("ORACLE_SIGNAL_FALLBACK_ENABLED", "0") == "1"),
        "stall_exit_mode": settings.STALL_EXIT_MODE,
        "half_ratchet_verified": [],
        "frozen_symbols": sorted(frozen_state),
        "trade_stage": os.environ.get("TRADE_STAGE", "1.5").strip(),
        "allowed_symbols": sorted(allowed_symbols),
        "mirror_requires_autopaper": (
            os.environ.get("MIRROR_REQUIRES_AUTOPAPER", "1") != "0"),
        "allow_buy_enabled": os.environ.get("ALLOW_BUY") == "1",
        "orders_enabled": os.environ.get("KIS_ORDERS_ENABLED") == "1",
        "position_counts_by_sleeve": position_counts,
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

    relevant_markets = _relevant_open_order_markets(
        local_positions, open_orders, allowed_symbols,
        market_of_symbol=kis.market_of_symbol)
    responses: list[dict | None] = []
    if "KR" in relevant_markets:
        responses.append(kis.domestic_open_orders())
    if "US" in relevant_markets:
        responses.extend(
            kis.open_orders(excg=exchange)
            for exchange in ("NASD", "NYSE", "AMEX"))
    open_counts: list[int] = []
    for response in responses:
        count = _broker_open_count(response)
        if count is None:
            open_counts = []
            break
        open_counts.append(count)
    snapshot["broker_open_orders"] = (
        sum(open_counts) if len(open_counts) == len(responses) else None)
    return snapshot


def render_text(report: dict) -> str:
    lines = [f"scope={report.get('scope', 'strict')}"]
    for gate in report.get("gates") or []:
        status = (
            "PASS" if gate.get("ok")
            else "BLOCK" if gate.get("blocking", True)
            else "INFO"
        )
        lines.append(
            f"[{status}] "
            f"{gate.get('name')}: {gate.get('detail')}")
    verdict = "GO — operator 승인 검토 가능" if report.get(
        "ready_for_operator_review") else "NO-GO — L1 유지"
    lines.append(f"\n결과: {verdict}")
    lines.append("주의: GO도 kill-switch를 자동으로 내리지 않는다.")
    return "\n".join(lines)
