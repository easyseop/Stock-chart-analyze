"""매도 전용 파수꾼 + advisor stale 가드 검증 (SRE 검토 §3·§4 구현분).

  ① 하드 손절(손절가 −1% 이탈) → 즉시 매도
  ② 소프트 손절(손절가 이하) → 1회차 대기, 2연속 확인 후 매도
  ③ 멱등키 — 같은 손절이 두 번 나가지 않음(재폴링·재시작 내성)
  ④ 피드 stale → 기존 손절선으로 보호 지속(신규 스냅샷 갱신만 보류)
  ⑤ 매수 경로 부재 — 모듈에 buy가 없음(보안 원칙 회귀 방지)
  ⑥ advisor: 신호 낡으면 제안 0건 + 경보 1회

실행: python -m tests.test_sentinel
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.sentinel as sn
import bot.ledger as L


class FakeBroker:
    name = "fake"

    def __init__(self, prices):
        self.prices, self.sells = prices, []

    def quote(self, code, ccy):
        return self.prices.get(code)

    def place_sell(self, code, qty, reason, key):
        self.sells.append((code, qty, reason, key))
        return True


POS = {"code": "TT", "name": "테스트", "ccy": "USD", "q": 10, "stop": 100.0}


def _setup(tmp, feed_positions, age=None):
    sn.SENT_PATH = os.path.join(tmp, "sent.json")
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")   # 원장도 tmp로(전역 오염 방지)
    os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
    sn._market_open = lambda ccy: True
    sn._notify = lambda text, **kw: NOTES.append(text)
    sn._fetch_positions = lambda: (feed_positions, age)


NOTES: list = []


def main() -> int:
    fails = []
    global NOTES

    # ① 하드 손절 즉시
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)])
        bk = FakeBroker({"TT": 98.9})          # 100 − 1% = 99 아래
        st = {}
        sn.check_once(bk, st)
        if len(bk.sells) != 1 or "하드" not in bk.sells[0][2]:
            fails.append(f"하드 손절 실패: {bk.sells}")
        else:
            print("  [PASS] 하드 손절(−1% 이탈) 즉시 매도")

    # ② 소프트 손절 2연속 + ③ 멱등
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)])
        bk = FakeBroker({"TT": 99.5})          # 스탑 이하, 하드 위
        st = {}
        sn.check_once(bk, st)
        if bk.sells:
            fails.append("소프트 1회차에 매도됨(2연속 위반)")
        sn.check_once(bk, st)
        if len(bk.sells) != 1:
            fails.append(f"소프트 2연속 매도 실패: {bk.sells}")
        sn.check_once(bk, st)                   # 3회차 — 멱등키로 중복 방지
        sn.check_once(bk, st)
        if len(bk.sells) != 1:
            fails.append(f"멱등 위반 — 중복 매도: {len(bk.sells)}회")
        else:
            print("  [PASS] 소프트 2연속 확인 + 멱등키 중복 차단")

    # dry-run 판정을 영속 완료로 쓰면 LIVE 전환 뒤 같은 포지션 손절이 영구 누락된다.
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)])
        class DryTruthKis(FakeBroker):
            name = "kis"

            def holdings(self):
                return {"TT": 10}

            def place_sell(self, code, qty, reason, key):
                self.sells.append((code, qty, reason, key))
                return {"state": "dry_run", "filled": 0}

        dry = DryTruthKis({"TT": 98.0})
        with mock.patch.object(sn, "LIVE", False), \
             mock.patch("bot.kis_positions.load", return_value={}), \
             mock.patch("bot.kis_exits.manage"):
            sn.check_once(dry, {})
        if sn._load_sent():
            fails.append("dry-run 판단이 영속 sent 완료로 기록됨")
        else:
            class LiveTruthKis(FakeBroker):
                name = "kis"

                def holdings(self):
                    return {"TT": 10}

            live = LiveTruthKis({"TT": 98.0})
            with mock.patch.object(sn, "LIVE", True), \
                 mock.patch("bot.kis_positions.load", return_value={}), \
                 mock.patch("bot.kis_exits.manage"):
                sn.check_once(live, {})             # 프로세스 재시작과 같은 새 메모리
            if len(live.sells) != 1:
                fails.append("dry-run 뒤 LIVE 전환에서 실제 손절이 멱등키로 막힘")
            else:
                print("  [PASS] dry-run은 영속 완료 아님 → LIVE 전환 뒤 손절 가능")

    # ④ 피드 stale — 알고 있던 포지션으로 보호 지속
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)], age=5)
        bk = FakeBroker({"TT": 150.0})          # 손절 위 — 매도 없음
        st = {}
        sn.check_once(bk, st)                   # 신선한 피드로 스냅샷 확보
        sn._fetch_positions = lambda: ([], 999)  # 피드가 낡음(999분 > 경보임계 60)
        sn._market_open = lambda ccy: True       # 장중 고정(경보는 장중에만)
        bk.prices["TT"] = 98.0                   # 그 사이 급락
        sn.check_once(bk, st)
        if len(bk.sells) != 1:
            fails.append("stale 피드에서 보호 실패 — 기존 손절선 미집행")
        elif not any("정체" in n for n in NOTES):
            fails.append("stale 경보 미발송")
        else:
            print("  [PASS] 피드 stale에도 기존 손절선으로 보호 + 정체 경보 1회")

    # ⑦ 트레일로 손절선이 바뀌어도 같은 포지션엔 재발화 없음(멱등키=포지션 정체성)
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        pos = {"code": "TR", "name": "트레일", "ccy": "USD", "q": 10,
               "stop": 100.0, "opened": "2026-07-01"}
        _setup(tmp, [pos])
        bk = FakeBroker({"TR": 98.0})              # 하드 손절 → 1회 발화
        st = {}
        sn.check_once(bk, st)
        # 손절선이 트레일로 상향되고(110) 가격도 그 아래로 — 옛 키({code}:{stop}:{date})
        #   였다면 stop이 바뀌어 '새 키'가 되어 재발화했을 상황.
        pos2 = dict(pos); pos2["stop"] = 110.0
        sn._fetch_positions = lambda: ([pos2], None)
        bk.prices["TR"] = 108.0                    # 110 −1% = 108.9 아래(하드)
        sn.check_once(bk, st)
        if len(bk.sells) != 1:
            fails.append(f"트레일 변경 후 중복 발화({len(bk.sells)}회) — 정체성 키 실패")
        else:
            print("  [PASS] 트레일로 손절선 변경돼도 같은 포지션 재발화 없음")

    # ⑧ 주문 UNKNOWN → 종목 잠금 → 대사 → 잔여만 재주문(초과매도 방지 end-to-end)
    class UnknownThenFill:
        name = "mock"

        def __init__(self, price):
            self.price, self.calls, self.phase = price, [], "unknown"

        def quote(self, code, ccy):
            return self.price

        def place_sell(self, code, qty, reason, key):
            self.calls.append((code, qty, key))
            if self.phase == "unknown":
                self.phase = "filled"
                return {"state": "unknown", "filled": 0}   # 첫 주문 타임아웃
            return {"state": "filled", "filled": qty}

        def order_status(self, key):
            return 3          # 대사: 첫 주문은 실제로 3주만 체결돼 있었다(부분)

    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        pos = {"code": "UN", "name": "언논", "ccy": "USD", "q": 10,
               "stop": 100.0, "opened": "2026-07-02"}
        _setup(tmp, [pos])
        bk = UnknownThenFill(98.0)                # 하드 손절 발화가
        st = {}
        sn.check_once(bk, st)                     # #1: 발주→UNKNOWN→잠금
        if not any("UNKNOWN" in n for n in NOTES):
            fails.append("UNKNOWN 경보 미발송")
        # 잠금 중엔 재주문 없어야(같은 사이클 반복해도)
        sn.check_once(bk, st)                     # #2: 대사→부분3 확정→잔여7 재주문→체결
        qtys = [c[1] for c in bk.calls]
        if qtys != [10, 7]:
            fails.append(f"잔여 재주문 오류 — 주문 수량 흐름 {qtys}(기대 [10,7])")
        elif len(bk.calls) != 2:
            fails.append(f"주문 횟수 오류: {len(bk.calls)}(초과매도 위험)")
        else:
            sn.check_once(bk, st)                 # #3: 이미 완결 → 추가 주문 없음
            if len(bk.calls) != 2:
                fails.append("완결 후 추가 주문 발생(초과매도)")
            else:
                print("  [PASS] UNKNOWN→잠금→대사→잔여만(10→7) 재주문, 초과매도 없음")

    # ⑨ KIS 잔고 조회 실패 — 공개 paper feed 수량으로 실계좌 매도 금지
    class FailedKis(FakeBroker):
        name = "kis"

        def holdings(self):
            return None

    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)])
        bk = FailedKis({"TT": 98.0})
        st = {}
        with mock.patch("bot.kis_exits.manage"):
            sn.check_once(bk, st)
        if bk.sells:
            fails.append(f"KIS 잔고 실패인데 feed 수량으로 매도함: {bk.sells}")
        elif not any("잔고 조회 실패" in n for n in NOTES):
            fails.append("KIS 잔고 실패 P0 경보 누락")
        else:
            print("  [PASS] KIS 잔고 실패 → 공개 feed 수량 매도 금지·P0 경보")

    # ⑩ KIS 잔고가 포지션 기록보다 먼저 보이면 BUY 원장의 손절선으로 보호 유지
    class TruthKis(FakeBroker):
        name = "kis"

        def __init__(self, prices, holdings):
            super().__init__(prices)
            self.current_holdings = holdings
            self.holding_calls = 0

        def holdings(self):
            self.holding_calls += 1
            return dict(self.current_holdings)

    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [])
        L.record_submit("kb:alk", "ALK", 8, meta={
            "side": "BUY", "market": "US", "stop": 88.0, "ccy": "USD",
            "pos_key": "kb:alk", "opened": "2026-07-25", "name": "Alaska"})
        L.bind_broker_order("kb:alk", "OD-BUY")
        L.on_result("kb:alk", "ack", 0, open_order=True)
        bk = TruthKis({"ALK": 100.0}, {"ALK": 8})
        with mock.patch("bot.kis_positions.load", return_value={}), \
             mock.patch("bot.kis_exits.manage"):
            sn.check_once(bk, {})
        if bk.sells:
            fails.append("임시 손절선 위인데 매도됨")
        elif not any("대사 중" in n and "88" in n for n in NOTES):
            fails.append(f"ACK 대사 중 임시 손절 알림 누락: {NOTES}")
        else:
            print("  [PASS] 잔고 선반영·포지션 기록 지연 → BUY 원장 손절선으로 보호")

    # ⑪ 손절과 BUY 잔량 경합 — 취소 확인 뒤 실잔고 재조회 수량만 SELL
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        pos = {"code": "ALK", "name": "Alaska", "ccy": "USD", "q": 8,
               "stop": 100.0, "opened": "2026-07-25"}
        _setup(tmp, [pos])
        L.record_submit("kb:alk", "ALK", 8, meta={
            "side": "BUY", "market": "US", "stop": 100.0, "ccy": "USD"})
        L.bind_broker_order("kb:alk", "OD-BUY")
        L.on_result("kb:alk", "partial", 3, open_order=True)
        bk = TruthKis({"ALK": 98.0}, {"ALK": 3})
        with mock.patch("bot.kis_pending.cancel_open_buys_for_protection",
                        side_effect=[False, True]), \
             mock.patch("bot.kis_positions.load", return_value={}), \
             mock.patch("bot.kis_positions.close"), \
             mock.patch("bot.kis_exits.manage"):
            sn.check_once(bk, {})                 # 취소 접수/확인 전 — SELL 금지
            if bk.sells:
                fails.append("BUY 취소 확인 전에 손절 SELL 동시 발주")
            sn.check_once(bk, {})                 # 확인 뒤 잔고 3주 재조회 → 3주만
        if [s[1] for s in bk.sells] != [3]:
            fails.append(f"BUY 취소 뒤 실잔고 수량 매도 오류: {bk.sells}")
        elif bk.holding_calls < 3:                # 사이클 2회 + 취소확인 뒤 재조회
            fails.append("BUY 취소 확인 뒤 KIS 실잔고 재조회 누락")
        else:
            print("  [PASS] BUY 잔량 취소확인 → KIS 실잔고 재조회 → 3주만 손절")

    # ⑫ 공개 paper feed 실패/빈 목록이어도 로컬 kis_positions 손절선으로 보호 지속
    #    (KIS는 autopaper 미러가 아니다 — feed는 참고 손절선일 뿐, 보호의 전제가 아님)
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [])                       # feed 404/빈 positions와 동일한 결과
        bk = TruthKis({"TT": 98.0}, {"TT": 10})
        krec = {"TT": {"name": "테스트", "ccy": "USD", "stop": 100.0,
                       "opened": "2026-07-25", "sleeve": "A", "pos_key": "kb:tt"}}
        with mock.patch("bot.kis_positions.load", return_value=krec), \
             mock.patch("bot.kis_exits.manage"):
            sn.check_once(bk, {})             # 98.0 < 99.0(하드) → 즉시 매도여야
        if [s[0] for s in bk.sells] != ["TT"]:
            fails.append(f"빈 paper feed에서 로컬 stop 보호 실패: {bk.sells}")
        else:
            print("  [PASS] paper feed 빈 목록 → 로컬 kis_positions 손절선으로 보호")

    # ⑫ KIS 주문 직전 보유 재조회 — 사이클 수량 8이어도 실제 3주만 전송
    with tempfile.TemporaryDirectory() as tmp:
        L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
        kb = object.__new__(sn._KisBroker)
        kb.quote = lambda *_: 100.0
        with mock.patch.object(sn, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.sellable_holdings", return_value={"ALK": 3}), \
             mock.patch("bot.kis_positions.load", return_value={}), \
             mock.patch("bot.kis_orders.place_sell",
                        return_value={"act": "ack"}) as place:
            out = kb.place_sell("ALK", 8, "손절", "sell:alk#1")
        sent_qty = place.call_args.args[2]
        if sent_qty != 3 or place.call_args.kwargs["hldg_before"] != 3:
            fails.append(f"주문 직전 잔고 clamp 실패: qty={sent_qty}")
        elif out.get("qty") != 3:
            fails.append(f"실제 전송 수량 반환 누락: {out}")
        else:
            print("  [PASS] 주문 직전 KIS 보유 재조회·요청 8→실보유 3주 clamp")

        with mock.patch.object(sn, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.sellable_holdings", return_value=None), \
             mock.patch("bot.kis_orders.place_sell") as blocked_place:
            blocked = kb.place_sell("ALK", 8, "손절", "sell:alk#2")
        if blocked["state"] != "rejected" or blocked_place.called:
            fails.append(f"주문 직전 매도가능수량 실패인데 발주됨: {blocked}")
        else:
            print("  [PASS] 주문 직전 매도가능수량 조회 실패 → 전송 전 차단")

    # ⑤ 매수 경로 부재(보안 원칙)
    src = open(os.path.join(os.path.dirname(sn.__file__), "sentinel.py"),
               encoding="utf-8").read()
    if "def place_buy" in src or "def buy" in src:
        fails.append("파수꾼에 매수 경로 존재(매도 전용 원칙 위반)")
    else:
        print("  [PASS] 매수 경로 부재(매도 전용)")

    # ⑥ advisor stale 가드
    import types
    from bot import advisor, notify
    import bot.settings as cfg
    sent_msgs = []
    notify.send = lambda t, **kw: sent_msgs.append(t) or True
    orig_open = cfg.market_open
    cfg.market_open = lambda ccy: True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            advisor.STATE_PATH = os.path.join(tmp, "alert_state.json")
            sig_path = os.path.join(tmp, "signals.json")
            json.dump({"generated_at": "2026-01-01T00:00:00+09:00",
                       "signals": [{"group": "now", "id": "X", "code": "X",
                                    "name": "X", "ccy": "USD", "entry": 100,
                                    "stop": 95, "fresh": True, "stage": 3,
                                    "norm": 50, "tactic": {}}]},
                      open(sig_path, "w"))
            args = types.SimpleNamespace(signals=sig_path, holdings=os.path.join(tmp, "h.json"),
                                         dry_run=False)
            advisor.run_once(args)
            stale_alerts = [m for m in sent_msgs if "낡음" in m]
            buys = [m for m in sent_msgs if "매수 제안" in m]
            if buys:
                fails.append("낡은 신호로 매수 제안 발송됨")
            elif len(stale_alerts) != 1:
                fails.append(f"stale 경보 {len(stale_alerts)}회(기대 1)")
            else:
                advisor.run_once(args)          # 같은 날 재실행 — 경보 중복 금지
                if len([m for m in sent_msgs if "낡음" in m]) != 1:
                    fails.append("stale 경보 하루 1회 초과")
                else:
                    print("  [PASS] advisor: 낡은 신호 → 제안 0건 + 경보 하루 1회")
    finally:
        cfg.market_open = orig_open

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 파수꾼·stale 가드 전부 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
