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
    assert "BUYLOOP_POLL_SECONDS=60" in buy
    assert "--poll 300" not in buy
    assert "SENTINEL_POLL_SECONDS=20" in sent
    assert "PORTFOLIO_REFRESH_SECONDS=60" in portfolio


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
    print("[PASS] 오라클 서비스 주기 설정·폭주 방지 경계")
