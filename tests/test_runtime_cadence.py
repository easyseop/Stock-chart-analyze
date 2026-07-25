"""오라클 상시 서비스의 갱신 주기·폭주 방지 설정 검증."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def test_service_cadence_contract():
    buy = (ROOT / "infra/server/buyloop.service").read_text(encoding="utf-8")
    sent = (ROOT / "infra/server/sentinel.service").read_text(encoding="utf-8")
    portfolio = (ROOT / "infra/server/portfolio-web.service").read_text(encoding="utf-8")
    brain = (ROOT / "infra/server/oracle-brain.service").read_text(encoding="utf-8")
    brain_timer = (ROOT / "infra/server/oracle-brain.timer").read_text(encoding="utf-8")
    brain_oracle = (
        ROOT / "infra/server/oracle-brain.oracle-ubuntu.conf"
    ).read_text(encoding="utf-8")
    autodeploy = (ROOT / "infra/server/autodeploy.sh").read_text(encoding="utf-8")
    assert "BUYLOOP_POLL_SECONDS=60" in buy
    assert "ORACLE_SIGNAL_FALLBACK_ENABLED=0" in buy
    assert "--poll 300" not in buy
    assert "SENTINEL_POLL_SECONDS=20" in sent
    assert "PORTFOLIO_REFRESH_SECONDS=60" in portfolio
    assert "OnUnitInactiveSec=5min" in brain_timer
    assert "MemoryMax=420M" in brain and "CPUWeight=10" in brain
    assert "EnvironmentFile=/etc/stock/kis.env" not in brain
    assert "StateDirectory=stock-oracle-brain" in brain
    assert "CacheDirectory=stock-oracle-brain" in brain
    assert "SCANNER_CACHE_DIR=/var/cache/stock-oracle-brain" in brain
    assert "ReadWritePaths=/home/" not in brain
    assert "/home/ubuntu" not in brain
    assert "WorkingDirectory=/home/ubuntu/Stock-chart-analyze" in brain_oracle
    assert "bot.signal_feed" in autodeploy
    assert "scanner.oracle_brain" not in autodeploy
    for forbidden in ("bot.kis_buyloop", "bot.sentinel", "kis_orders"):
        assert forbidden not in brain


def test_required_watchdog_cannot_be_reported_as_optional():
    path = ROOT / "infra/server/health_beacon.sh"
    beacon = path.read_text(encoding="utf-8")
    assert 'BEACON_REQUIRED_UNITS:-sentinel buyloop watchdog' in beacon
    assert '*" $u "*) down=$((down+1));;' in beacon
    assert "for u in $ALL_UNITS" in beacon
    env = {
        **os.environ,
        "BEACON_ENV": str(ROOT / "tests" / "does-not-exist.env"),
        "NTFY_HEALTH_TOPIC": "unit-test",
        "BEACON_UNITS": "portfolio-web sentinel",
        "BEACON_REQUIRED_UNITS": "sentinel watchdog",
        "BEACON_PRINT_UNITS_ONLY": "1",
    }
    result = subprocess.run(
        ["bash", str(path)], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True)
    assert result.stdout.strip().split() == [
        "portfolio-web", "sentinel", "watchdog"]


def test_oracle_cache_path_is_systemd_managed_and_configurable():
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "SCANNER_CACHE_DIR": "/var/cache/stock-oracle-brain",
    }
    out = subprocess.check_output(
        [sys.executable, "-c",
         "from scanner.cache import CACHE_DIR; print(CACHE_DIR)"],
        cwd=ROOT, env=env, text=True)
    assert out.strip() == "/var/cache/stock-oracle-brain"


def test_sentinel_poll_setting_is_bounded():
    env = {**os.environ, "PYTHONPATH": str(ROOT), "SENTINEL_POLL_SECONDS": "1"}
    out = subprocess.check_output(
        [sys.executable, "-c", "from bot.sentinel import POLL_SEC; print(POLL_SEC)"],
        cwd=ROOT, env=env, text=True)
    assert out.strip() == "5"
    env["SENTINEL_POLL_SECONDS"] = "999"
    out = subprocess.check_output(
        [sys.executable, "-c", "from bot.sentinel import POLL_SEC; print(POLL_SEC)"],
        cwd=ROOT, env=env, text=True)
    assert out.strip() == "60"


def test_portfolio_refresh_default_and_bounds():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("PORTFOLIO_REFRESH_SECONDS", None)
    command = (
        "from bot.portfolio_web import PORTFOLIO_REFRESH_SECONDS; "
        "print(PORTFOLIO_REFRESH_SECONDS)"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", command], cwd=ROOT, env=env, text=True)
    assert out.strip() == "60"
    env["PORTFOLIO_REFRESH_SECONDS"] = "1"
    out = subprocess.check_output(
        [sys.executable, "-c", command], cwd=ROOT, env=env, text=True)
    assert out.strip() == "5"
    env["PORTFOLIO_REFRESH_SECONDS"] = "999"
    out = subprocess.check_output(
        [sys.executable, "-c", command], cwd=ROOT, env=env, text=True)
    assert out.strip() == "300"


if __name__ == "__main__":
    test_service_cadence_contract()
    test_sentinel_poll_setting_is_bounded()
    test_portfolio_refresh_default_and_bounds()
    test_required_watchdog_cannot_be_reported_as_optional()
    test_oracle_cache_path_is_systemd_managed_and_configurable()
    print("[PASS] 오라클 서비스 주기 설정·폭주 방지 경계")
