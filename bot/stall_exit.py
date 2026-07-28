"""절반익절 후 정체 기반 잔량 관리의 공용 순수 로직.

KIS 실계좌와 scanner.autopaper가 같은 상태 전이 규칙을 사용한다. 이 모듈은
네트워크·파일·주문을 전혀 다루지 않고 새 상태와 전이 이벤트만 반환한다.
"""
from __future__ import annotations

import copy
import math


VALID_MODES = frozenset({"off", "shadow", "live"})


def mode(value: object) -> str:
    """알 수 없는 모드는 안전하게 off로 닫는다."""
    text = str(value or "off").strip().lower()
    return text if text in VALID_MODES else "off"


def advance(
    state: dict,
    *,
    half_confirmed: bool,
    price: float,
    entry: float,
    stop0: float,
    day: str,
    valid_trading_day: bool,
    enabled: bool,
    new_high_r: float = 0.25,
    tighten_days: int = 15,
    exit_days: int = 30,
) -> tuple[dict, tuple[str, ...]]:
    """시세 1개를 반영한 새 상태와 전이 이벤트를 반환한다.

    이벤트는 ``started``·``meaningful_high``·``tighten``·``exit`` 중 일부다.
    같은 날짜는 한 번만 세고, 의미있는 신고가는 경계값(정확히 +new_high_r R)을
    포함한다. ``high``는 기존 1.5R 트레일용 절대 최고가이며 작은 신고가도 보존한다.
    """
    current = copy.deepcopy(state) if isinstance(state, dict) else {}
    events: list[str] = []
    try:
        px = float(price)
        ent = float(entry)
        initial_stop = float(stop0)
        reset_r = float(new_high_r)
        tight_at = int(tighten_days)
        exit_at = int(exit_days)
    except (TypeError, ValueError):
        return current, ()
    if not all(math.isfinite(v) for v in (px, ent, initial_stop, reset_r)):
        return current, ()
    if px <= 0 or ent <= initial_stop or reset_r <= 0:
        return current, ()

    # 장 마감 뒤/휴장일의 비정규 시세가 최고가와 정체 기준을 바꾸지 않게 한다.
    # 기존 트레일용 high 역시 실제 거래일의 유효 시세로만 갱신한다.
    if not valid_trading_day:
        return current, ()
    current["high"] = max(float(current.get("high") or 0.0), px)
    if not (enabled and half_confirmed and day):
        return current, ()

    risk = ent - initial_stop
    anchor = current.get("stall_anchor_high")
    try:
        anchor = float(anchor)
    except (TypeError, ValueError):
        anchor = 0.0
    if not math.isfinite(anchor) or anchor <= 0:
        current.update({
            "stall_anchor_high": max(px, current["high"], ent + risk),
            "high_day": str(day),
            "stall_days": 0,
            "counted_days": str(day),
            "stall_tight_notified": False,
            "stall_exit_notified": False,
        })
        return current, ("started",)

    threshold = anchor + reset_r * risk
    if px + 1e-12 >= threshold:
        current.update({
            "stall_anchor_high": px,
            "high_day": str(day),
            "stall_days": 0,
            "counted_days": str(day),
            "stall_tight_notified": False,
            "stall_exit_notified": False,
        })
        return current, ("meaningful_high",)

    if str(current.get("counted_days") or "") == str(day):
        return current, ()
    before = max(0, int(current.get("stall_days") or 0))
    after = before + 1
    current["stall_days"] = after
    current["counted_days"] = str(day)
    if before < tight_at <= after:
        events.append("tighten")
    if before < exit_at <= after:
        events.append("exit")
    return current, tuple(events)


def trail_r(
    state: dict,
    *,
    run_mode: str,
    base_r: float = 1.5,
    tight_r: float = 1.0,
    tighten_days: int = 15,
) -> float:
    """실제 적용할 트레일 폭. shadow/off는 기존 폭을 절대 바꾸지 않는다."""
    if mode(run_mode) != "live":
        return float(base_r)
    return (float(tight_r)
            if int((state or {}).get("stall_days") or 0) >= int(tighten_days)
            else float(base_r))


def exit_due(state: dict, *, run_mode: str, exit_days: int = 30) -> bool:
    """live일 때만 잔량 청산을 허용한다."""
    return (mode(run_mode) == "live"
            and int((state or {}).get("stall_days") or 0) >= int(exit_days))
