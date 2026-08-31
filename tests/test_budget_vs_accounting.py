"""예산선 초과와 회계 붕괴를 구분한다 — kill 래치의 오발동 방지.

실측 2026-08-29~31: A 투입원가 35,152,634가 슬리브 배분한도 35,150,000을
**2,634원**(0.0075%) 넘겨 kill L1이 68시간 지속됐다. 회계는 멀쩡했다 —
명목 시드 37,000,000 대비 costbook은 34,205,761로 280만원 여유였다.

초과가 사라지지 않은 이유는 두 값이 서로 다른 축이기 때문이다. 호출부가 넘기는
브로커 투입원가는 현재 환율로 재평가되지만(kis_buyloop.py:281) costbook은 체결
시점 환율로 고정된다. 움직이는 값을 고정된 선에 대면 원-달러가 하루 0.3%만
움직여도 선을 넘나든다 — 08-19에 손으로 내린 kill이 1분 만에 재발했다.

계약: 예산선 초과 = 그 매수만 거절(gate="budget"). kill은 환율·예약 잡음으로
설명되지 않는 폭(기본 5%)을 넘었을 때만. 완충 구간에서도 신규 매수는 전부
막히므로 노출은 늘지 않는다 — 달라지는 건 "사람을 부를 사건인가"뿐이다.
"""
import os

import pytest

from bot import envelope


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BOT_BUDGET_KILL_MARGIN", raising=False)


# ── 예산선(소프트) ──────────────────────────────────────────────
def test_at_limit_is_ok():
    assert envelope.invariant_ok(35_150_000, 35_150_000)


def test_one_won_over_is_a_budget_breach():
    assert not envelope.invariant_ok(35_150_000, 35_150_001)


# ── 회계선(하드·kill) ───────────────────────────────────────────
def test_production_hairline_is_not_an_accounting_breach():
    """실제로 68시간 kill을 유발한 그 값 — 이제 kill 대상이 아니어야 한다."""
    assert not envelope.accounting_breach(35_150_000, 35_152_634)


def test_prior_incident_value_also_clears():
    """08-18/08-19에 두 번 kill을 올렸던 값."""
    assert not envelope.accounting_breach(35_150_000, 35_094_687)


def test_material_overage_still_kills():
    """진짜 회계 붕괴는 여전히 잡는다 — 완화가 구멍이 되면 안 된다."""
    assert envelope.accounting_breach(10_000_000, 11_000_000)     # +10%
    assert envelope.accounting_breach(35_150_000, 40_000_000)     # +13.8%


def test_margin_boundary_is_exclusive():
    limit = 10_000_000
    assert not envelope.accounting_breach(limit, limit * 1.05)     # 정확히 5%
    assert envelope.accounting_breach(limit, limit * 1.05 + 1)


@pytest.mark.parametrize("limit", [0, -1, -35_150_000])
def test_nonpositive_limit_fails_closed(limit):
    """한도 미설정·오설정이면 어떤 투입도 설명 불가로 본다."""
    assert envelope.accounting_breach(limit, 1.0)
    assert not envelope.accounting_breach(limit, 0.0)


@pytest.mark.parametrize("raw,expected", [
    ("0.02", 0.02), ("0", 0.0), ("1", 1.0),
    ("-0.1", envelope.DEFAULT_BUDGET_KILL_MARGIN),      # 음수 거부
    ("1.5", envelope.DEFAULT_BUDGET_KILL_MARGIN),       # 과대 거부
    ("nan", envelope.DEFAULT_BUDGET_KILL_MARGIN),
    ("abc", envelope.DEFAULT_BUDGET_KILL_MARGIN),
    ("", envelope.DEFAULT_BUDGET_KILL_MARGIN),
])
def test_margin_env_override_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("BOT_BUDGET_KILL_MARGIN", raw)
    assert envelope.budget_kill_margin() == expected


def test_margin_zero_restores_old_behaviour(monkeypatch):
    """완충을 0으로 두면 종전과 동일 — 되돌릴 수 있는 스위치를 남긴다."""
    monkeypatch.setenv("BOT_BUDGET_KILL_MARGIN", "0")
    assert envelope.accounting_breach(35_150_000, 35_152_634)


# ── 두 선의 관계 ────────────────────────────────────────────────
def test_accounting_line_is_never_tighter_than_budget_line():
    """회계선이 예산선보다 좁으면 원래 버그로 되돌아간다."""
    for limit in (1_000_000, 35_150_000, 42_000_000):
        for over in (1, 1_000, 100_000):
            cost = limit + over
            assert not envelope.invariant_ok(limit, cost)
            if envelope.accounting_breach(limit, cost):
                assert cost > limit, "회계선이 예산선보다 좁다"
