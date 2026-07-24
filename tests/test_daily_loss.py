"""KIS 일일 손실 래치 — −2% 신규매수 차단·재시작 유지·KST 자정 해제."""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import daily_loss as D
from bot import kis_buy


def test_threshold_latch_and_next_day_reset():
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(D, "LATCH_PATH", os.path.join(tmp, "loss.json")):
        ok = D.status(seed_total=15_000, realized=-299, day="2026-07-24")
        assert ok["allowed"] and not ok["latched"]
        hit = D.status(seed_total=15_000, realized=-300, day="2026-07-24")
        assert not hit["allowed"] and hit["latched"] and hit["threshold"] == -300
        # 손익 입력이 나중에 좋아져도 같은 날은 래치가 풀리지 않는다.
        still = D.status(seed_total=15_000, realized=500, day="2026-07-24")
        assert still["latched"] and not still["allowed"]
        # KST 날짜가 바뀌면 전일 래치만 무효화한다.
        tomorrow = D.status(seed_total=15_000, realized=0, day="2026-07-25")
        assert tomorrow["allowed"] and not tomorrow["latched"]
        print("[PASS] −2% 도달 래치·재계산 불해제·KST 자정 자동 리셋")


def test_invalid_seed_fails_closed():
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(D, "LATCH_PATH", os.path.join(tmp, "loss.json")):
        s = D.status(seed_total=0, realized=0, day="2026-07-24")
        assert not s["allowed"] and "SEED" in s["why"]
        print("[PASS] 시드 미설정은 신규매수 fail-closed")


def test_buy_chain_uses_daily_gate():
    with mock.patch.dict(os.environ, {"ALLOW_BUY": "1"}), \
         mock.patch.object(kis_buy.kill, "allows", return_value=True), \
         mock.patch.object(kis_buy.daily_loss, "entry_allowed",
                           return_value=(False, "일일 손실 래치")):
        d = kis_buy.execute_entry(
            "x", "AAPL", price_usd=100, per_share_risk_usd=5,
            krw_per_usd=1400)
    assert not d.ok and d.gate == "daily_loss"
    print("[PASS] kis_buy 게이트 체인에서 신규매수만 일일 손실 차단")


def main():
    test_threshold_latch_and_next_day_reset()
    test_invalid_seed_fails_closed()
    test_buy_chain_uses_daily_gate()
    print("\nKIS 일일 손실 서킷브레이커 검증 통과.")


if __name__ == "__main__":
    main()
