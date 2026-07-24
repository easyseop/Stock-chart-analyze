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


def test_half_proposal_and_fail_retry():
    """코덱스 P0 반영: half 확정은 주문 성공 후 — 실패 시 다음 사이클 재제안."""
    xs = {"half": False, "high": 0.0}
    acts = X.decide(entry=100, stop0=90, cur_stop=90, price=110.5, qty=10,
                    xs=xs, opened="2026-07-20", today="2026-07-22")
    assert acts == [("half_sell", 5)]
    assert xs["half"] is False                     # decide는 영구상태 안 바꿈
    # 주문 '실패' 시나리오: half 미확정 → 재호출하면 다시 제안(재시도 보장)
    acts2 = X.decide(100, 90, 90, 110.5, 10, xs, "2026-07-20", "2026-07-22")
    assert acts2 == [("half_sell", 5)]
    # 주문 '성공' 후(호출부가 half 확정) → 더는 절반매도 제안 없음
    xs["half"] = True
    acts3 = X.decide(100, 90, 100, 110.5, 5, xs, "2026-07-20", "2026-07-22")
    assert not any(a[0] == "half_sell" for a in acts3)
    # 1주 보유 — 매도 없이 래칫만(half_done)
    xs1 = {"half": False, "high": 0.0}
    assert X.decide(100, 90, 90, 110.5, 1, xs1, "2026-07-20",
                    "2026-07-22") == [("half_done",)]
    print("[PASS] +1R 제안-확정 분리 · 실패 시 재시도 · 1주 half_done")


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


def test_sleeve_b_exits():
    """B 전용 청산(코덱스 P1 반영): 목표(VAH) 전량 익절 + 타임스탑, A규칙 미적용."""
    assert X.decide_b(110.0, 111.0, 7, "2026-07-20", "2026-07-22") == \
        [("sell", 7, "B 목표(VAH) 도달")]
    assert X.decide_b(110.0, 105.0, 7, "2026-07-20", "2026-07-22") == []
    acts = X.decide_b(0.0, 105.0, 7, "2026-07-01", "2026-07-24")
    assert acts and "타임스탑" in acts[0][2]
    print("[PASS] B 전용 청산 — VAH 목표·타임스탑, +1R/트레일 미적용")


def main():
    test_half_proposal_and_fail_retry()
    test_trail_ratchet_only_up()
    test_time_stop()
    test_no_action_zone()
    test_raise_stop_ledger()
    test_sleeve_b_exits()
    print("\nKIS 청산 관리자 검증 통과 — 익절/래칫/타임스탑/B청산.")


if __name__ == "__main__":
    main()
