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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_exits as X
from bot import ledger as L
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


class _SeqBroker:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def quote(self, code, ccy):
        return 111.0

    def place_sell(self, code, qty, reason, key):
        self.calls.append((code, qty, key))
        return self.results.pop(0)


def _manage_env(tmp):
    KP.PATH = os.path.join(tmp, "kis_positions.jsonl")
    L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
    X.STATE_PATH = os.path.join(tmp, "exits.json")
    KP.record("ACK", stop=90.0, ccy="USD", entry=100.0, qty=10,
              opened="2026-07-20", pos_key="kb:ack")
    return {"ACK": {"code": "ACK", "q": 10, "ccy": "USD", "stop": 90.0}}


def test_half_ack_is_not_fill():
    """접수(ACK, filled=0)만으로 half/본전 손절을 확정하면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(X.notify, "send"):
        held = _manage_env(tmp)
        broker = _SeqBroker([{"state": "ack", "filled": 0}])
        X.manage(broker, held, "2026-07-25")
        state1 = X._load()["ACK"]
        assert state1["half"] is False
        assert KP.load()["ACK"]["stop"] == 90.0
        orders = L.orders_for("ACK", side="SELL")
        assert len(orders) == 1 and orders[0]["state"] == "ack"

        # 다음 대사에서 목표 5주 체결이 확인된 뒤에만 본전으로 올린다.
        L.on_result(orders[0]["key"], "filled", 5, open_order=False)
        X.manage(broker, held, "2026-07-25")
        state2 = X._load()["ACK"]
        assert state2["half"] is True
        assert KP.load()["ACK"]["stop"] == 100.0
        assert len(broker.calls) == 1
    print("[PASS] 절반익절 ACK≠체결 — 5주 체결 대사 후에만 half·본전 확정")


def test_half_partial_retries_only_residual():
    """부분체결 2주는 보존하고 목표 5주 중 잔여 3주만 새 키로 재시도."""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(X.notify, "send"):
        held = _manage_env(tmp)
        broker = _SeqBroker([
            {"state": "partial", "filled": 2, "open": False},
            {"state": "filled", "filled": 3},
        ])
        X.manage(broker, held, "2026-07-25")
        assert X._load()["ACK"]["half"] is False
        assert KP.load()["ACK"]["stop"] == 90.0
        X.manage(broker, held, "2026-07-25")
        assert [c[1] for c in broker.calls] == [5, 3]
        assert X._load()["ACK"]["half"] is True
        assert KP.load()["ACK"]["stop"] == 100.0
    print("[PASS] 절반익절 부분체결 2주 + 잔여 3주만 재시도·확정")


def test_open_buy_does_not_block_stop_ratchet_but_defers_sell():
    """BUY 잔량은 보호선 상향을 막지 않되, 취소 대사 없는 매도와 경합하지 않는다."""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(X.notify, "send"):
        held = _manage_env(tmp)
        L.record_submit(
            "buy:open", "ACK", 2, "추가매수",
            meta={"side": "BUY", "market": "US"})
        # 이미 half가 확정된 상태면 BUY가 열려 있어도 트레일 보호선은 올린다.
        X._save({"ACK": {"half": True, "half_stop_raised": True,
                         "high": 120.0}})
        broker = _SeqBroker([])
        X.manage(broker, held, "2026-07-25")
        assert KP.load()["ACK"]["stop"] == 105.0
        assert not broker.calls

        # half 미확정 상태의 실제 매도는 BUY 취소 대사를 증명할 수 없으면 보류한다.
        X._save({"ACK": {"half": False, "high": 0.0}})
        X.manage(broker, held, "2026-07-25")
        assert X._load()["ACK"]["half"] is False
        assert not broker.calls
    print("[PASS] BUY 잔량 중 래칫은 계속·매도는 취소 대사 전 보류")


def test_time_btgt_retry_keys_are_session_capped_and_legacy_compatible():
    """거절된 time/btgt는 파생 키로 다음 세션에만 재시도한다."""
    import datetime
    from zoneinfo import ZoneInfo
    with tempfile.TemporaryDirectory() as tmp:
        L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
        base = "xe:TAP:time:2026-07-20"
        stamp = datetime.datetime(2026, 8, 10, 10, 0,
                                  tzinfo=ZoneInfo("America/New_York")).timestamp()
        # 배포 전 고정 키도 attempts=1로 세어 다음은 #2다.
        L._append({"ev": "submit", "key": base, "symbol": "TAP",
                   "intended": 80, "filled": 0, "state": "submitted",
                   "reason": "time", "meta": {"side": "SELL"}, "ts": stamp})
        L.on_result(base, "rejected", 0)
        xs = {}
        with mock.patch.object(X, "EXIT_RETRY_MAX_PER_SESSION", 1):
            key, capped, notice = X._next_exit_retry(
                "TAP", {"opened": "2026-07-20"}, xs, "time", "2026-08-11",
                now_ts=stamp + 3600)
            assert key is None and capped and notice
            key, capped, notice = X._next_exit_retry(
                "TAP", {"opened": "2026-07-20"}, xs, "time", "2026-08-12",
                now_ts=stamp + 86400)
            assert key == base + "#2" and capped and not notice

    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(X.notify, "send") as send:
        KP.PATH = os.path.join(tmp, "positions.jsonl")
        L.LEDGER_PATH = os.path.join(tmp, "orders.jsonl")
        X.STATE_PATH = os.path.join(tmp, "exits.json")
        KP.record("BTG", stop=90, ccy="USD", entry=100, qty=7,
                  opened="2026-07-20", sleeve="B", target=110,
                  pos_key="kb:btg")
        held = {"BTG": {"code": "BTG", "q": 7, "ccy": "USD", "stop": 90}}
        broker = _SeqBroker([False, False])
        same_session = datetime.datetime(2026, 8, 10, 23, 30,
                                         tzinfo=ZoneInfo("America/New_York")).timestamp()
        next_session = datetime.datetime(2026, 8, 11, 9, 30,
                                         tzinfo=ZoneInfo("America/New_York")).timestamp()
        with mock.patch.object(X, "EXIT_RETRY_MAX_PER_SESSION", 1), \
                mock.patch.object(X.time, "time", return_value=same_session):
            X.manage(broker, held, "2026-08-11")  # KST 자정 전후여도 NY 8/10
            X.manage(broker, held, "2026-08-12")
            assert len(broker.calls) == 1 and broker.calls[0][2].endswith("#1")
            assert len([c for c in send.call_args_list if "재시도 상한" in c.args[0]]) == 1
        with mock.patch.object(X, "EXIT_RETRY_MAX_PER_SESSION", 1), \
                mock.patch.object(X.time, "time", return_value=next_session):
            X.manage(broker, held, "2026-08-12")
            assert len(broker.calls) == 2              # 다음 세션에만 다시 시도
            assert len([c for c in send.call_args_list if "재시도 상한" in c.args[0]]) == 2
    print("[PASS] time/btgt 파생키·과거 고정키 승계·세션당 상한·다음세션 리셋")


def main():
    test_half_proposal_and_fail_retry()
    test_trail_ratchet_only_up()
    test_time_stop()
    test_no_action_zone()
    test_raise_stop_ledger()
    test_sleeve_b_exits()
    test_half_ack_is_not_fill()
    test_half_partial_retries_only_residual()
    test_open_buy_does_not_block_stop_ratchet_but_defers_sell()
    test_time_btgt_retry_keys_are_session_capped_and_legacy_compatible()
    print("\nKIS 청산 관리자 검증 통과 — 익절/래칫/타임스탑/B청산.")


if __name__ == "__main__":
    main()
