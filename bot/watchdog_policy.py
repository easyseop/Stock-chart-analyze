"""watchdog 상향과 self-heal 판정이 함께 쓰는 고정 문자열 계약."""
from __future__ import annotations

WATCHDOG_WHO = "watchdog"
HEARTBEAT_EXHAUSTED_REASON = "파수꾼 120s+ 다운·재기동 소진 — 신규 금지"
BALANCE_FAILURE_REASON = "KIS 잔고 조회 실패 지속 — 신규 금지"
SELF_HEAL_REASONS = {
    WATCHDOG_WHO: frozenset({HEARTBEAT_EXHAUSTED_REASON, BALANCE_FAILURE_REASON}),
}


def self_heal_allowed(who: object, why: object) -> bool:
    reasons = SELF_HEAL_REASONS.get(str(who))
    return reasons is not None and str(why) in reasons
