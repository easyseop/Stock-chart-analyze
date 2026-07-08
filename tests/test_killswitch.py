"""긴급 정지(KILL_NEW_ENTRIES) 검증 — 신규 진입만 멈추고 보호는 계속.

  1) 스위치 ON → '지금 진입' 추천이 있어도 신규 포지션·지정가 미생성
  2) 스위치 ON 이어도 **기존 지정가 체결(보유 관리)은 계속**된다
  3) 스위치 OFF → 정상 진입(대조군)

실행: python -m tests.test_killswitch
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner.autopaper as ap


def _fresh(tmp: str) -> None:
    ap.STATE_PATH = os.path.join(tmp, "autopaper.json")
    for p in (ap.STATE_PATH, ap.STATE_PATH + ".bak"):
        if os.path.exists(p):
            os.remove(p)
    ap._market_open = lambda ccy: True
    ap._earnings_d = lambda code: None
    ap._price_age_min = lambda code: 3              # 신선(진입 허용 조건)
    ap._trading_lock_status = lambda run_id: "off"


def _item(code, price, stop):
    return {"code": code, "name": code, "ccy": "KRW", "price": price,
            "stop": stop, "target": price + 2 * (price - stop),
            "earnings_d": None,
            "tactic": {"mode": "full", "stop_pct": (price - stop) / price * 100}}


def _row(code, price):
    return {"code": code, "name": code, "ccy": "KRW", "sr": {"price": price},
            "turnover": 10_000_000_000}


def main() -> int:
    fails = []
    os.environ.pop("KILL_NEW_ENTRIES", None)

    # 1) 스위치 ON → 신규 진입 차단
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        os.environ["KILL_NEW_ENTRIES"] = "1"
        out = ap.update([_row("ST", 10_000)],
                        {"now": [_item("ST", 10_000, 9_500)]}, out_dir=tmp)
        if out["positions"] or out["pending"]:
            fails.append("긴급정지 ON인데 신규 진입함")
        else:
            print("  [PASS] 스위치 ON → 신규 진입 차단")

    # 2) 스위치 ON 이어도 기존 지정가 체결(관리)은 계속
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        os.environ["KILL_NEW_ENTRIES"] = "on"
        st = {"v": ap.VERSION, "cash": ap.START, "start": ap.START,
              "pos": {}, "log": [],
              "pending": {"PB": {"name": "PB", "ccy": "KRW", "limit": 49_500,
                                 "stop": 49_000, "target": 52_000, "q": 10,
                                 "created": ap._today(), "atr": 500,
                                 "plan": None, "ctx": None, "basis": "t"}}}
        ap._save(st)
        out = ap.update([_row("PB", 49_400)],          # 지정가 아래 → 체결돼야
                        {"now": [_item("NEW", 10_000, 9_500)]}, out_dir=tmp)
        if not any(p["code"] == "PB" for p in out["positions"]):
            fails.append("긴급정지 중 기존 지정가 체결이 멈춤(관리 중단)")
        elif any(p["code"] == "NEW" for p in out["positions"]) or \
                any(o["code"] == "NEW" for o in out["pending"]):
            fails.append("긴급정지 중 신규 종목이 진입됨")
        else:
            print("  [PASS] 스위치 ON → 기존 체결은 계속, 신규만 차단")

    # 3) 스위치 OFF → 정상 진입(대조군)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        os.environ.pop("KILL_NEW_ENTRIES", None)
        out = ap.update([_row("ST", 10_000)],
                        {"now": [_item("ST", 10_000, 9_500)]}, out_dir=tmp)
        if not (out["positions"] or out["pending"]):
            fails.append("스위치 OFF인데 진입 안 함(대조군 실패)")
        else:
            print("  [PASS] 스위치 OFF → 정상 진입")

    os.environ.pop("KILL_NEW_ENTRIES", None)
    if fails:
        print("\n❌ 실패:", *fails, sep="\n  - ")
        return 1
    print("\n✅ 긴급 정지 전부 통과 — 신규만 멈추고 보유 관리·체결은 계속.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
