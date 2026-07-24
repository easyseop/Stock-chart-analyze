"""KIS 일일 실현손실 서킷브레이커 — 신규 매수만 영속 차단.

손절·청산·파수꾼은 이 모듈을 보지 않는다. 당일 실현손익이
`-DAILY_LOSS_LIMIT × (SEED_A + SEED_B)`에 닿으면 KST 자정까지 래치한다.
프로세스 재시작으로 손실 한도가 풀리지 않도록 파일에 원자적으로 저장한다.
"""
from __future__ import annotations

import datetime
import json
import os

from bot import costbook

LATCH_PATH = os.environ.get(
    "KIS_DAILY_LOSS_PATH", os.path.join(os.path.dirname(__file__), "daily_loss.json"))
_KST = datetime.timezone(datetime.timedelta(hours=9))


def _today() -> str:
    return datetime.datetime.now(_KST).date().isoformat()


def _load() -> dict:
    try:
        with open(LATCH_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(LATCH_PATH) or ".", exist_ok=True)
    tmp = LATCH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, LATCH_PATH)


def status(*, seed_total: float | None = None, realized: float | None = None,
           day: str | None = None) -> dict:
    """현재 게이트 상태를 계산하고 한도 도달 시 래치한다."""
    from bot import envelope, settings
    day = day or _today()
    if seed_total is None:
        seed_total = envelope.seed_krw() + envelope.seed_krw_sb()
    seed_total = float(seed_total)
    if realized is None:
        realized = costbook.realized_on(day)
    realized = float(realized)
    limit = float(settings.DAILY_LOSS_LIMIT)
    threshold = -seed_total * limit

    old = _load()
    if old.get("day") == day and old.get("latched"):
        return {**old, "allowed": False}
    if seed_total <= 0:
        return {"day": day, "latched": False, "allowed": False,
                "realized": realized, "threshold": threshold,
                "why": "SEED_A+SEED_B 미설정"}
    if realized <= threshold:
        out = {"day": day, "latched": True, "allowed": False,
               "realized": realized, "threshold": threshold}
        _save(out)
        # 차단 상태를 먼저 영속화한다. 알림 채널 장애가 매수 차단을 되돌리면 안 된다.
        try:
            from bot import notify
            notify.send(
                f"🛑 일일 손실 서킷브레이커 발동 — 당일 실현 {realized:.0f}원 "
                f"≤ 한도 {threshold:.0f}원. 신규 매수 중단"
                "(손절·청산은 계속, KST 자정 리셋).",
                critical=True,
                category="trade",
            )
        except Exception:
            pass
        return out
    # 전일 래치는 KST 날짜가 바뀌면 자동 무효. 새 날짜 상태를 남겨 운영자가 확인 가능.
    out = {"day": day, "latched": False, "allowed": True,
           "realized": realized, "threshold": threshold}
    if old.get("day") != day:
        _save(out)
    return out


def entry_allowed() -> tuple[bool, str]:
    s = status()
    if s["allowed"]:
        return True, "ok"
    if s.get("latched"):
        return False, (f"일일 손실 래치 {s['realized']:.0f}원 "
                       f"≤ {s['threshold']:.0f}원(KST 자정 리셋)")
    return False, s.get("why", "일일 손실 게이트 판정 불가")
