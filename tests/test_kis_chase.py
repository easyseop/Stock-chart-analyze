"""R5 chase 상태기계 + R4 heartbeat SLA 검증(순수 로직 — 전송 없음).

chase:
  1) 1차 발주 체결 → done
  2) 미체결 → 시간 경과 → 취소 확정 → 더 공격적 재발주(사다리) → 체결 done
  3) 가격 floor(max_slippage) 아래로 절대 안 내려감
  4) 취소 UNKNOWN → manual_lock(자동 재개 금지)
  5) max_chase 소진 → exhausted / max_time 초과 → timeout
  6) 취소 거부(이미 체결) → 현 주문 유지 → 다음 step에서 filled 재확인 done
heartbeat:
  7) 나이·SLA 임계(30/60/120)·기록 없음 fail-closed·entry_allowed

실행: python -m tests.test_kis_chase
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.kis_chase import Chase, ChaseConfig
from bot import heartbeat as hb


class _Env:
    """가짜 브로커 환경 — 시간·체결·주문 스크립트 제어."""

    def __init__(self, qty):
        self.t = 1000.0
        self.filled = 0
        self.qty = qty
        self.places = []          # (key, qty, price)
        self.cancels = []
        self.place_acts = []      # 다음 place 응답 큐(기본 ack)
        self.cancel_acts = []     # 다음 cancel 응답 큐(기본 canceled)

    def deps(self):
        return {
            "place": self._place, "cancel": self._cancel,
            "filled_total": lambda: self.filled,
            "quote": lambda: None, "now": lambda: self.t,
        }

    def _place(self, key, symbol, qty, price):
        self.places.append((key, qty, price))
        act = self.place_acts.pop(0) if self.place_acts else "ack"
        return {"ok": act == "ack", "act": act, "odno": f"od{len(self.places)}"}

    def _cancel(self, key, symbol, odno, qty):
        self.cancels.append((key, odno))
        act = self.cancel_acts.pop(0) if self.cancel_acts else "canceled"
        return {"ok": act == "canceled", "act": act}


def _mk(env, qty=10, ref=100.0, **cfg):
    return Chase("pos", "AAPL", qty, ref, env.deps(),
                 ChaseConfig(**cfg) if cfg else ChaseConfig())


def test_fill_first_try():
    env = _Env(10)
    c = _mk(env)
    assert c.step() == "working" and len(env.places) == 1
    env.filled = 10                       # 전량 체결
    assert c.step() == "done"
    # 1차 가격 = 100×(1−30bp) = 99.70
    assert env.places[0][2] == 99.70
    print("[PASS] 1차 발주(−30bp) → 체결 → done")


def test_reprice_ladder_then_fill():
    env = _Env(10)
    c = _mk(env)
    c.step()                              # 발주1 @99.70
    env.t += 25                           # repost_after(20s) 경과
    c.step()                              # 취소 확정
    assert len(env.cancels) == 1 and c.current is None
    c.step()                              # 발주2 — 더 공격적 (30+40bp = −0.70)
    assert env.places[1][2] == 99.30
    env.filled = 10
    assert c.step() == "done"
    print("[PASS] 미체결 → 취소 → 사다리 재발주(−70bp) → done")


def test_floor_respected():
    env = _Env(10)
    c = _mk(env, max_slippage_bps=100)    # floor = 99.00
    c.attempts = 2                        # 3차 시도라면 30+80=110bp → floor 클램프
    c.step()
    assert env.places[0][2] == 99.00      # floor 아래로 안 내려감
    print("[PASS] 가격 floor(−1%) 하한 준수")


def test_cancel_unknown_manual_lock():
    env = _Env(10)
    c = _mk(env)
    c.step()
    env.t += 25
    env.cancel_acts = ["unknown"]         # 취소 응답 유실
    assert c.step() == "manual_lock"
    assert c.step() == "manual_lock"      # terminal 고정(자동 재개 없음)
    assert len(env.places) == 1           # 재발주 안 함(이중매도 차단)
    print("[PASS] 취소 UNKNOWN → manual_lock 고정·재발주 없음")


def test_exhausted_and_timeout():
    env = _Env(10)
    c = _mk(env, max_chase=2)
    for _ in range(2):                    # 발주→취소 2회
        c.step()
        env.t += 25
        c.step()
    assert c.step() == "exhausted" and len(env.places) == 2
    env2 = _Env(10)
    c2 = _mk(env2, max_time_to_exit_s=60)
    c2.step()
    env2.t += 61
    assert c2.step() == "timeout"
    print("[PASS] max_chase 소진→exhausted · max_time 초과→timeout")


def test_cancel_reject_then_filled():
    """취소 거부 = 이미 체결됐을 가능성 → 주문 유지, 다음 step 체결 확인."""
    env = _Env(10)
    c = _mk(env)
    c.step()
    env.t += 25
    env.cancel_acts = ["reject"]
    assert c.step() == "working" and c.current is not None   # 유지
    env.filled = 10                       # 사실은 체결돼 있었음
    assert c.step() == "done"
    assert len(env.places) == 1           # 이중매도 없음
    print("[PASS] 취소 거부 → 주문 유지 → 체결 재확인 done(이중매도 0)")


def test_heartbeat_sla(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SENTINEL_HEARTBEAT_PATH"] = os.path.join(tmp, "hb.json")
        assert hb.age_s() is None
        assert hb.sla_status(None, True) == hb.HARD_DISABLE   # 기록없음+보유=최악
        assert hb.sla_status(None, False) == hb.P0
        hb.write()
        a = hb.age_s()
        assert a is not None and a < 5
        assert hb.sla_status(10, True) == hb.OK
        assert hb.sla_status(90, True) == hb.P0
        assert hb.sla_status(150, True) == hb.HARD_DISABLE
        assert hb.sla_status(150, False) == hb.P0              # 무보유면 P0까지
        assert hb.entry_allowed(True) is True                  # 방금 기록 → 허용
        del os.environ["SENTINEL_HEARTBEAT_PATH"]
    print("[PASS] heartbeat: 기록·나이·SLA(30/60/120)·fail-closed·entry_allowed")


def main():
    test_fill_first_try()
    test_reprice_ladder_then_fill()
    test_floor_respected()
    test_cancel_unknown_manual_lock()
    test_exhausted_and_timeout()
    test_cancel_reject_then_filled()
    test_heartbeat_sla()
    print("\n모든 chase/SLA 테스트 통과 — 손절 신뢰성 보강(R4·R5).")


if __name__ == "__main__":
    main()
