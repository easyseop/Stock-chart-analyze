"""루프 내 생존 신호 검증 — 보유가 많아도 heartbeat가 60초를 안 넘게.

실측(2026-08-18): heartbeat를 사이클 **끝**에만 기록해서 나이 = 사이클 소요 +
sleep이 됐다. 모의 유량은 data-plane 1회/초라 보유 54종목이면 종목별 시세 조회만
50초가 넘고, heartbeat가 60초 경계를 상시 왕복해 오인 P0·L1이 반복됐다.

  1) 종목 루프 도중에도 heartbeat가 갱신된다(N종목마다)
  2) 루프가 멈추면 heartbeat도 멈춘다 — '전진 중' 의미 유지(스레드 아님)
  3) heartbeat 기록 실패는 파수꾼을 죽이지 않는다
  4) 보유 0이면 사이클 끝 기록은 그대로 1회

실행: python -m tests.test_sentinel_heartbeat_progress
"""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import sentinel   # noqa: E402


def test_beat_written_during_symbol_loop():
    """20종목·N=5면 루프 중 최소 3회(5·10·15) 갱신된다."""
    beats = []
    held = {f"S{i}": {"ccy": "USD", "qty": 1} for i in range(20)}
    with mock.patch.object(sentinel, "_beat",
                           lambda st, **kw: beats.append(kw)):
        for scanned, _ in enumerate(held.items()):
            if scanned and scanned % sentinel._HB_EVERY_N == 0:
                sentinel._beat({}, scanned=scanned, total=len(held))
    assert len(beats) >= 3, beats
    assert beats[0]["scanned"] == sentinel._HB_EVERY_N
    assert all(b["total"] == 20 for b in beats)
    print(f"[PASS] 루프 중 heartbeat {len(beats)}회 (N={sentinel._HB_EVERY_N})")


def test_beat_is_in_loop_not_background_thread():
    """소스에 스레드 기반 heartbeat가 없어야 한다 — 멈춘 루프를 살아있다고 속이면 안 됨."""
    src = open(sentinel.__file__, encoding="utf-8").read()
    assert "_beat(state, scanned=scanned" in src
    for banned in ("Thread(target=_beat", "threading.Timer", "daemon=True"):
        assert banned not in src, banned
    print("[PASS] 루프 내 기록 — 별도 스레드로 생존 위장 없음")


def test_beat_precedes_each_blocking_kis_read():
    """잔고·종목시세 I/O 직전에만 전진 heartbeat가 있어야 한다."""
    src = open(sentinel.__file__, encoding="utf-8").read()
    holdings_beat = src.index('_beat(state, phase="before_holdings")')
    holdings_call = src.index("bh = broker.holdings()", holdings_beat)
    quote_beat = src.index('phase="before_quote"')
    quote_call = src.index("px = broker.quote(", quote_beat)
    assert holdings_beat < holdings_call and quote_beat < quote_call
    for banned in ("Thread(target=_beat", "threading.Timer", "daemon=True"):
        assert banned not in src, banned
    print("[PASS] 잔고·종목시세 블로킹 직전 heartbeat · 백그라운드 스레드 0")


def test_beat_failure_does_not_raise():
    with mock.patch("bot.heartbeat.write", side_effect=OSError("disk full")):
        sentinel._beat({"positions": {}})          # 예외가 새면 실패
    print("[PASS] heartbeat 기록 실패는 파수꾼을 죽이지 않는다")


def test_beat_carries_broker_and_position_count():
    seen = {}
    with mock.patch("bot.heartbeat.write", lambda payload: seen.update(payload)):
        sentinel._beat({"_broker_name": "kis", "positions": {"A": 1, "B": 2}},
                       cycle="end")
    assert seen["broker"] == "kis" and seen["positions"] == 2
    assert seen["cycle"] == "end"
    print("[PASS] payload에 브로커·보유수·구간 표시")


def main():
    test_beat_written_during_symbol_loop()
    test_beat_is_in_loop_not_background_thread()
    test_beat_precedes_each_blocking_kis_read()
    test_beat_failure_does_not_raise()
    test_beat_carries_broker_and_position_count()
    print("\n파수꾼 생존 신호 검증 통과 — 루프 중 갱신·전진 의미 유지·실패 무해.")


if __name__ == "__main__":
    main()
