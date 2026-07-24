"""파수꾼 브로커-진실 보호 검증 — KIS 실보유를 손절(수량=브로커 진실).

  1) kis_positions 저장소 record/load/close
  2) feed에 없어도 KIS 보유를 매수루프 기록 손절선으로 보호(무보호 공백 제거)
  3) 매도 수량 = 브로커 실보유(feed q 아님 — 초과매도 방지)
  4) 손절선 불명 KIS 보유 → 무보호 P0(새 것만)
  5) 브로커 조회 실패(None) → 공개 feed 수량으로 실계좌 매도 금지

실행: python -m tests.test_kis_brokertruth
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.sentinel as sn        # noqa: E402
import bot.ledger as L           # noqa: E402
import bot.kis_positions as KP   # noqa: E402

NOTES: list = []


class FakeKisBroker:
    name = "kis"

    def __init__(self, prices, holds):
        self.prices, self.holds, self.sells = prices, holds, []

    def quote(self, code, ccy):
        return self.prices.get(code)

    def place_sell(self, code, qty, reason, key):
        self.sells.append((code, qty, reason, key))
        return {"state": "ack", "filled": 0}

    def holdings(self):
        return self.holds


def _setup(tmp, feed, age=None):
    sn.SENT_PATH = os.path.join(tmp, "sent.json")
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    KP.PATH = os.path.join(tmp, "kispos.jsonl")
    os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
    sn._market_open = lambda ccy: True
    sn._notify = lambda text, **kw: NOTES.append(text)
    sn._fetch_positions = lambda: (feed, age)


def test_store():
    with tempfile.TemporaryDirectory() as tmp:
        KP.PATH = os.path.join(tmp, "k.jsonl")
        KP.record("AAPL", stop=100.0, ccy="USD", entry=105.0, qty=5)
        assert KP.load()["AAPL"]["stop"] == 100.0
        KP.close("AAPL")
        assert "AAPL" not in KP.load()
    print("[PASS] kis_positions 저장소 record/load/close")


def test_protects_kis_only_position():
    global NOTES
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, feed=[])                     # feed 비었음
        KP.record("AAPL", stop=100.0, ccy="USD", qty=5, opened="2026-07-13")
        br = FakeKisBroker({"AAPL": 98.0}, {"AAPL": 5})   # 손절선 아래
        sn.check_once(br, {})
        assert br.sells and br.sells[0][0] == "AAPL", br.sells
        assert br.sells[0][1] == 5               # 브로커 보유 전량
    print("[PASS] feed에 없어도 KIS 보유를 진입 손절선으로 보호")


def test_sell_qty_is_broker_truth():
    global NOTES
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        # feed는 10주라 하지만 브로커 실보유는 3주 → 3만 판다(초과매도 방지)
        feed = [{"code": "AAPL", "name": "t", "ccy": "USD", "q": 10,
                 "stop": 100.0, "opened": "2026-07-13"}]
        _setup(tmp, feed=feed)
        br = FakeKisBroker({"AAPL": 98.0}, {"AAPL": 3})
        sn.check_once(br, {})
        assert br.sells and br.sells[0][1] == 3, br.sells   # feed 10 아님
    print("[PASS] 매도 수량 = 브로커 실보유(3), feed 원수량(10) 아님")


def test_unprotected_alert():
    global NOTES
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, feed=[])                     # 손절선 소스 없음
        br = FakeKisBroker({"TSLA": 50.0}, {"TSLA": 2})
        st = {}
        sn.check_once(br, st)
        assert not br.sells                      # 손절선 없으니 매도 안 함
        assert any("무보호" in n or "손절선 불명" in n for n in NOTES), NOTES
        # 다음 사이클엔 같은 무보호 재알림 안 함(폭주 방지)
        NOTES = []
        sn.check_once(br, st)
        assert not any("손절선 불명" in n for n in NOTES)
    print("[PASS] 손절선 불명 KIS 보유 → 무보호 P0(새 것만)")


def test_holdings_none_blocks_public_feed_qty():
    global NOTES
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        feed = [{"code": "AAPL", "name": "t", "ccy": "USD", "q": 4,
                 "stop": 100.0, "opened": "2026-07-13"}]
        _setup(tmp, feed=feed)
        br = FakeKisBroker({"AAPL": 98.0}, None)   # 잔고 조회 실패
        sn.check_once(br, {})
        assert not br.sells                         # paper feed q=4로 실주문 금지
        assert any("잔고 조회 실패" in n for n in NOTES)
    print("[PASS] 브로커 조회 실패 → 공개 feed 수량 매도 금지·P0 경보")


def main():
    test_store()
    test_protects_kis_only_position()
    test_sell_qty_is_broker_truth()
    test_unprotected_alert()
    test_holdings_none_blocks_public_feed_qty()
    print("\n브로커-진실 보호 검증 통과 — KIS 실보유 손절·초과매도 방지·무보호 경보.")


if __name__ == "__main__":
    main()
