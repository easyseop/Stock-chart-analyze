"""GitHub 지연 시 Oracle 신호 선택의 fail-closed 경계 검증."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot import kis_buyloop, signal_feed

NOW = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)


def _doc(age_min: float, code: str, *, oracle: bool = False,
         basis_age_min: float = 60) -> dict:
    out = {
        "generated_at": (NOW - timedelta(minutes=age_min)).isoformat(),
        "signals": [{"code": code, "group": "now", "fresh": True,
                     "entry": 100, "stop": 90}],
    }
    if oracle:
        out.update({
            "source": "oracle-local-brain",
            "contract": signal_feed.CONTRACT,
            "basis_generated_at": (
                NOW - timedelta(minutes=basis_age_min)).isoformat(),
        })
    return out


def test_fresh_github_always_wins_over_local():
    result = signal_feed.select(
        remote_docs=[_doc(10, "AAPL")],
        local_doc=_doc(1, "MSFT", oracle=True),
        fallback_enabled=True, now=NOW)
    assert result["source"] == "github"
    assert result["signals"][0]["code"] == "AAPL"


def test_delayed_github_uses_fresh_local_only_when_explicitly_enabled():
    remote = _doc(25, "AAPL")
    local = _doc(2, "MSFT", oracle=True)
    shadow = signal_feed.select(
        remote_docs=[remote], local_doc=local,
        fallback_enabled=False, now=NOW)
    live = signal_feed.select(
        remote_docs=[remote], local_doc=local,
        fallback_enabled=True, now=NOW)
    assert shadow["source"] == "github"
    assert shadow["why"] == "local-shadow-disabled"
    assert live["source"] == "oracle"
    assert live["signals"][0]["code"] == "MSFT"


def test_both_stale_or_invalid_is_fail_closed():
    stale = signal_feed.select(
        remote_docs=[_doc(46, "AAPL")],
        local_doc=_doc(13, "MSFT", oracle=True),
        fallback_enabled=True, now=NOW)
    assert stale["source"] == "none" and stale["signals"] == []

    invalid = _doc(1, "MSFT", oracle=True, basis_age_min=1441)
    no_basis = signal_feed.select(
        remote_docs=[], local_doc=invalid,
        fallback_enabled=True, now=NOW)
    assert no_basis["source"] == "none" and no_basis["signals"] == []

    shadow_only = signal_feed.select(
        remote_docs=[_doc(46, "AAPL")],
        local_doc=_doc(2, "MSFT", oracle=True),
        fallback_enabled=False, now=NOW)
    assert shadow_only["source"] == "none"
    assert shadow_only["signals"] == []


def test_bad_time_duplicate_or_wrong_contract_is_rejected():
    bad_time = {"generated_at": "not-a-time", "signals": [{"code": "AAPL"}]}
    duplicate = _doc(1, "AAPL")
    duplicate["signals"].append({"code": "AAPL", "group": "now"})
    wrong = _doc(1, "MSFT", oracle=True)
    wrong["contract"] = "other"
    future = _doc(-6, "NVDA", oracle=True)
    result = signal_feed.select(
        remote_docs=[bad_time, duplicate], local_doc=wrong,
        fallback_enabled=True, now=NOW)
    assert result["source"] == "none" and result["signals"] == []
    future_result = signal_feed.select(
        remote_docs=[], local_doc=future,
        fallback_enabled=True, now=NOW)
    assert future_result["source"] == "none"
    assert future_result["signals"] == []


def test_buyloop_consumes_selector_result_without_direct_network_logic():
    selected = {
        "signals": [{"code": "AAPL"}],
        "source": "oracle", "age_min": 1.5,
        "why": "github-delayed-local-fallback",
        "fallback_enabled": True,
    }
    with mock.patch("bot.signal_feed.load_selected", return_value=selected):
        assert kis_buyloop._fetch_signals() == [{"code": "AAPL"}]


def test_local_selector_has_no_order_plane_imports():
    source = Path(signal_feed.__file__).read_text(encoding="utf-8")
    for forbidden in (
            "bot.kis", "kis_orders", "kis_buy", "ledger",
            "place_buy", "place_sell"):
        assert forbidden not in source


def main():
    tests = [
        test_fresh_github_always_wins_over_local,
        test_delayed_github_uses_fresh_local_only_when_explicitly_enabled,
        test_both_stale_or_invalid_is_fail_closed,
        test_bad_time_duplicate_or_wrong_contract_is_rejected,
        test_buyloop_consumes_selector_result_without_direct_network_logic,
        test_local_selector_has_no_order_plane_imports,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("\nGitHub/Oracle 신호 선택 경계 검증 통과.")


if __name__ == "__main__":
    main()
