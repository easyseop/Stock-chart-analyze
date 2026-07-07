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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.sentinel as sn


class FakeBroker:
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
    sn._market_open = lambda ccy: True
    sn._notify = lambda text: NOTES.append(text)
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

    # ④ 피드 stale — 알고 있던 포지션으로 보호 지속
    with tempfile.TemporaryDirectory() as tmp:
        NOTES = []
        _setup(tmp, [dict(POS)], age=5)
        bk = FakeBroker({"TT": 150.0})          # 손절 위 — 매도 없음
        st = {}
        sn.check_once(bk, st)                   # 신선한 피드로 스냅샷 확보
        sn._fetch_positions = lambda: ([], 999)  # 피드가 낡음(빈 목록 무시돼야)
        bk.prices["TT"] = 98.0                   # 그 사이 급락
        sn.check_once(bk, st)
        if len(bk.sells) != 1:
            fails.append("stale 피드에서 보호 실패 — 기존 손절선 미집행")
        elif not any("낡음" in n for n in NOTES):
            fails.append("stale 경보 미발송")
        else:
            print("  [PASS] 피드 stale에도 기존 손절선으로 보호 + 경보 1회")

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
    notify.send = lambda t: sent_msgs.append(t) or True
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
