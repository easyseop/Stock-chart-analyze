"""Oracle 독립 소형 분석기의 주문 분리·장애 지속성 검증."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

from bot import signal_feed
from scanner import oracle_brain

NOW = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)


def _remote() -> dict:
    return {
        "generated_at": (NOW - timedelta(minutes=8)).isoformat(),
        "_age_min": 8.0,
        "signals": [
            {"code": "AAPL", "name": "Apple", "ccy": "USD",
             "group": "now", "fresh": True, "stage": 3, "norm": 91},
            {"code": "AAPL", "name": "Apple", "ccy": "USD",
             "group": "shelf", "fresh": True, "stage": 0, "norm": 91},
            {"code": "MSFT", "name": "Microsoft", "ccy": "USD",
             "group": "watch", "fresh": False, "stage": 2, "norm": 80},
        ],
    }


def test_candidate_pool_survives_remote_outage_but_expires():
    with tempfile.TemporaryDirectory() as td:
        pool = os.path.join(td, "candidates.json")
        with mock.patch.object(
                oracle_brain, "_fresh_remote_basis", return_value=_remote()):
            rows, basis = oracle_brain._candidate_pool(now=NOW, pool_path=pool)
        assert [row["code"] for row in rows] == ["AAPL", "MSFT"]
        assert basis == _remote()["generated_at"]

        with mock.patch.object(
                oracle_brain, "_fresh_remote_basis", return_value=None):
            cached, cached_basis = oracle_brain._candidate_pool(
                now=NOW + timedelta(hours=1), pool_path=pool)
            expired, expired_basis = oracle_brain._candidate_pool(
                now=NOW + timedelta(hours=25), pool_path=pool)
        assert [row["code"] for row in cached] == ["AAPL", "MSFT"]
        assert cached_basis == basis
        assert expired == [] and expired_basis == ""


def test_shadow_run_writes_valid_atomic_local_feed_without_trading():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "signals.json")
        pool = os.path.join(td, "candidates.json")
        state = os.path.join(td, "discovery.json")
        lock = os.path.join(td, "brain.lock")
        remote = _remote()
        signal_json = json.dumps({
            "version": 1,
            "generated_at": NOW.isoformat(),
            "signals": [{
                "code": "AAPL", "name": "Apple", "ccy": "USD",
                "group": "now", "fresh": True, "entry": 100, "stop": 90,
            }],
        })
        with mock.patch.object(oracle_brain, "_now", return_value=NOW), \
                mock.patch.object(
                    oracle_brain, "_fresh_remote_basis", return_value=remote), \
                mock.patch.object(
                    oracle_brain.settings, "market_open", return_value=False), \
                mock.patch.object(
                    oracle_brain.universe, "load", return_value=[]), \
                mock.patch.object(oracle_brain.cache, "update"), \
                mock.patch.object(
                    oracle_brain.cache, "frames", return_value={"D": object()}), \
                mock.patch.object(
                    oracle_brain.data, "fetch_benchmark", return_value=None), \
                mock.patch.object(
                    oracle_brain, "analyze",
                    side_effect=lambda _frames, meta, bench=None: {
                        "code": meta["code"], "name": meta["name"],
                        "ccy": meta["ccy"]}), \
                mock.patch.object(
                    oracle_brain.screener, "_paper_picks",
                    return_value={"now": [], "watch": []}), \
                mock.patch.object(oracle_brain.gates, "audit") as audit, \
                mock.patch.object(
                    oracle_brain.screener, "_signals_json",
                    return_value=signal_json):
            result = oracle_brain.run(
                force=True, max_symbols=2, discovery_symbols=0,
                output_path=out, pool_path=pool, state_path=state,
                lock_path=lock)
        payload = json.loads(Path(out).read_text(encoding="utf-8"))
        assert result["ok"] is True and result["analyzed"] == 2
        assert payload["source"] == "oracle-local-brain"
        assert payload["contract"] == signal_feed.CONTRACT
        assert payload["activation"] == "external-fallback-gate"
        assert payload["basis_generated_at"] == remote["generated_at"]
        audit.assert_called_once()
        assert os.stat(out).st_mode & 0o777 == 0o644
        selected = signal_feed.select(
            remote_docs=[], local_doc=payload,
            fallback_enabled=True, now=NOW + timedelta(minutes=1))
        assert selected["source"] == "oracle"


def test_expired_basis_cannot_be_refreshed_by_discovery_or_watchlist():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "signals.json")
        pool = os.path.join(td, "candidates.json")
        state = os.path.join(td, "discovery.json")
        lock = os.path.join(td, "brain.lock")
        Path(pool).write_text(json.dumps({
            "version": 1,
            "saved_at": (NOW - timedelta(hours=25)).isoformat(),
            "basis_generated_at": (NOW - timedelta(hours=25)).isoformat(),
            "candidates": [{"code": "OLD", "name": "Expired", "ccy": "USD"}],
        }), encoding="utf-8")
        with mock.patch.object(oracle_brain, "_now", return_value=NOW), \
                mock.patch.object(
                    oracle_brain, "_fresh_remote_basis", return_value=None), \
                mock.patch.object(
                    oracle_brain.universe, "load", return_value=[
                        {"code": "AAPL", "name": "Apple", "ccy": "USD"}]), \
                mock.patch.object(
                    oracle_brain.config, "STOCKS", [
                        {"code": "MSFT", "name": "Microsoft", "ccy": "USD"}]), \
                mock.patch.object(
                    oracle_brain.data, "fetch_benchmark",
                    side_effect=AssertionError(
                        "expired basis must stop before market-data fetch")):
            result = oracle_brain.run(
                force=True, max_symbols=2, discovery_symbols=1,
                output_path=out, pool_path=pool, state_path=state,
                lock_path=lock)
        assert result == {
            "ok": False,
            "status": "no-safe-candidate-basis",
            "analyzed": 0,
        }
        assert not Path(out).exists()


def test_brain_source_has_no_kis_or_order_plane_imports():
    source = Path(oracle_brain.__file__).read_text(encoding="utf-8")
    for forbidden in (
            "from bot import kis", "import bot.kis", "kis_orders",
            "kis_buy", "ledger.record", "place_buy", "place_sell",
            "autopaper.update"):
        assert forbidden not in source


def test_brain_transitive_import_graph_has_no_order_plane():
    program = """
import json, sys
import scanner.oracle_brain
blocked = (
    "bot.kis", "bot.ledger", "bot.costbook", "bot.rollout",
    "bot.ownership", "bot.sentinel", "bot.envelope",
    "bot.daily_loss", "bot.killswitch",
)
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == item or name.startswith(item + ".") for item in blocked)
)))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[1],
        check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == []


def main():
    tests = [
        test_candidate_pool_survives_remote_outage_but_expires,
        test_shadow_run_writes_valid_atomic_local_feed_without_trading,
        test_expired_basis_cannot_be_refreshed_by_discovery_or_watchlist,
        test_brain_source_has_no_kis_or_order_plane_imports,
        test_brain_transitive_import_graph_has_no_order_plane,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("\nOracle 독립 소형 분석기 검증 통과.")


if __name__ == "__main__":
    main()
