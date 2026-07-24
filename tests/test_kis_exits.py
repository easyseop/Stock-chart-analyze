"""KIS 청산 관리자(kis_exits) 검증 — 순수 판단 함수 + 래칫 기록.

  1) +1R 도달: 절반 매도 + 본전 래칫 (1회만)
  2) 절반익절 후 트레일: 최고가−1.5R로 올리기만(내림 없음)
  3) 타임스탑: 21일 경과 + +1R 미도달 → 전량 매도
  4) +1R 미도달·기간 내 → 아무 행동 없음
  5) kis_positions.raise_stop: 래칫 반영·stop0 보존·내림 무시

실행: python -m tests.test_kis_exits
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_exits as X
from bot import kis_positions as KP


def test_half_and_breakeven():
    xs = {"half": False, "high": 0.0}
    acts = X.decide(entry=100, stop0=90, cur_stop=90, price=110.5, qty=10,
                    xs=xs, opened="2026-07-20", today="2026-07-22")
    kinds = [a[0] for a in acts]
    assert ("sell", 5, "익절 +1R 절반") in acts and "raise" in kinds
    raises = [a for a in acts if a[0] == "raise"]
    assert raises[0][1] == 100                     # 본전 래칫
    assert xs["half"] is True
    # 재호출 시 중복 매도 없음
    acts2 = X.decide(100, 90, 100, 110.5, 5, xs, "2026-07-20", "2026-07-22")
    assert not any(a[0] == "sell" for a in acts2)
    print("[PASS] +1R 절반익절 + 본전 래칫(1회)")


def test_trail_ratchet_only_up():
    xs = {"half": True, "high": 120.0}
    # 최고 120 → 트레일 = 120 − 1.5×10 = 105 (현 손절 100보다 위 → 올림)
    acts = X.decide(100, 90, 100, 118, 5, xs, "2026-07-20", "2026-07-22")
    assert ("raise", 105.0) in acts
    # 가격 하락해도 high 유지 → 내리는 raise 없음
    acts2 = X.decide(100, 90, 105, 106, 5, xs, "2026-07-20", "2026-07-22")
    assert not acts2
    print("[PASS] 트레일 래칫 — 최고가−1.5R, 올리기만")


def test_time_stop():
    xs = {"half": False, "high": 0.0}
    acts = X.decide(100, 90, 90, 103, 7, xs, "2026-07-01", "2026-07-24")
    assert acts and acts[-1][0] == "sell" and acts[-1][1] == 7
    assert "타임스탑" in acts[-1][2]
    # +1R 도달했으면(half) 타임스탑 없음
    xs2 = {"half": True, "high": 111.0}
    acts2 = X.decide(100, 90, 100, 103, 7, xs2, "2026-07-01", "2026-07-24")
    assert not any("타임스탑" in a[2] for a in acts2 if a[0] == "sell")
    print("[PASS] 타임스탑 21일 — +1R 미도달만 전량 정리")


def test_no_action_zone():
    xs = {"half": False, "high": 0.0}
    assert X.decide(100, 90, 90, 105, 10, xs, "2026-07-20", "2026-07-22") == []
    assert X.decide(0, 0, 0, 100, 10, {"half": False}, "", "") == []
    print("[PASS] +1R 미도달·기간 내 → 무행동, 무효 입력 방어")


def test_raise_stop_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        KP.PATH = os.path.join(tmp, "kis_positions.jsonl")
        KP.record("ZZZ", stop=90.0, ccy="USD", entry=100.0, qty=10,
                  opened="2026-07-20")
        KP.raise_stop("ZZZ", 100.0)
        KP.raise_stop("ZZZ", 95.0)                 # 내림 시도 → 무시돼야
        st = KP.load()["ZZZ"]
        assert st["stop"] == 100.0 and st["stop0"] == 90.0
    print("[PASS] raise_stop: 래칫 반영·stop0 보존·내림 무시")


def main():
    test_half_and_breakeven()
    test_trail_ratchet_only_up()
    test_time_stop()
    test_no_action_zone()
    test_raise_stop_ledger()
    print("\nKIS 청산 관리자 검증 통과 — 익절/래칫/타임스탑.")


if __name__ == "__main__":
    main()
