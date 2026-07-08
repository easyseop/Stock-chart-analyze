"""빠른 차선 안전장치 검증 (RELIABILITY Phase A).

  1) 시세 낡음(F5) — 신규 진입 금지: 캐시 나이 > STALE_ENTRY_MIN이면 안 산다
  2) 시세 낡음 — 지정가 체결 보류: 낡은 가격으로 체결 판단하지 않는다
  3) 매매 분산 락(F8) — 락 미보유 런은 매매·저장을 생략(표시만)
  4) saved_at — 저장마다 갱신(차선 간 최신성 비교의 기준)

실행: python -m tests.test_fastsafe
"""
from __future__ import annotations

import json
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
    ap._price_age_min = lambda code: None          # 기본: 신선(판단 보류)
    ap._trading_lock_status = lambda run_id: "off"  # 기본: 로컬(락 없음)


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

    # 1) 시세 낡음 → 신규 진입 금지
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ap._price_age_min = lambda code: 999        # 전부 낡음
        out = ap.update([_row("ST", 10_000)],
                        {"now": [_item("ST", 10_000, 9_500)]}, out_dir=tmp)
        if out["positions"] or out["pending"]:
            fails.append("낡은 시세로 신규 진입함")
        else:
            print("  [PASS] 시세 낡음 → 신규 진입 금지")
        ap._price_age_min = lambda code: 3          # 신선해지면 진입
        out = ap.update([_row("ST", 10_000)],
                        {"now": [_item("ST", 10_000, 9_500)]}, out_dir=tmp)
        if not (out["positions"] or out["pending"]):
            fails.append("신선한 시세인데 진입 안 함")
        else:
            print("  [PASS] 시세 신선 → 정상 진입")

    # 2) 시세 낡음 → 지정가 체결 보류(주문은 생존)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        st = {"v": ap.VERSION, "cash": ap.START, "start": ap.START,
              "pos": {}, "log": [],
              "pending": {"PB": {"name": "PB", "ccy": "KRW", "limit": 49_500,
                                 "stop": 49_000, "target": 52_000, "q": 10,
                                 "created": ap._today(), "atr": 500,
                                 "plan": None, "ctx": None, "basis": "t"}}}
        ap._save(st)
        ap._price_age_min = lambda code: 999
        out = ap.update([_row("PB", 49_400)], {"now": []}, out_dir=tmp)
        if any(p["code"] == "PB" for p in out["positions"]):
            fails.append("낡은 시세로 지정가 체결됨")
        elif not out["pending"]:
            fails.append("체결 보류여야 하는데 주문이 사라짐")
        else:
            print("  [PASS] 시세 낡음 → 지정가 체결 보류(주문 생존)")

    # 3) 락 미보유 런 — 매매·저장 생략
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ap._trading_lock_status = lambda run_id: "held"
        out = ap.update([_row("LK", 10_000)],
                        {"now": [_item("LK", 10_000, 9_500)]}, out_dir=tmp)
        if out["positions"] or out["pending"]:
            fails.append("락 미보유인데 매매함")
        elif not out.get("lock_skipped"):
            fails.append("락 생략 표시(lock_skipped) 누락")
        elif os.path.exists(ap.STATE_PATH):
            fails.append("락 미보유인데 상태를 저장함(스냅샷 오염 위험)")
        else:
            print("  [PASS] 락 미보유 → 매매·저장 생략 + 표시 플래그")

    # 4) saved_at — 저장마다 기록(차선 간 최신성 판단 기준)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ap.update([_row("SV", 10_000)],
                  {"now": [_item("SV", 10_000, 9_500)]}, out_dir=tmp)
        st = json.load(open(ap.STATE_PATH))
        if not st.get("saved_at", 0) > 0:
            fails.append("saved_at 미기록")
        else:
            print("  [PASS] saved_at 기록(차선 간 최신성 비교 가능)")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 빠른 차선 안전장치 4종 전부 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
