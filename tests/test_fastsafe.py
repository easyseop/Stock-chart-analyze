"""빠른 차선 안전장치 검증 (RELIABILITY Phase A).

  1) 시세 낡음(F5) — 신규 진입 금지: 캐시 나이 > STALE_ENTRY_MIN이면 안 산다
  2) 시세 낡음 — 지정가 체결 보류: 낡은 가격으로 체결 판단하지 않는다
  3) 매매 분산 락(F8) — 락 미보유 런은 매매·저장을 생략(표시만)
  4) saved_at — 저장마다 갱신(차선 간 최신성 비교의 기준)
  5) 코드 push 배포 — 모의매매·상태저장·알림 없이 표시 스냅샷만 생성

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
    ap._state_branch_snapshot = lambda: None
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

    # 5) 코드 push 재배포 — 매매·상태·락·알림과 완전 분리
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ap.update([_row("HOLD", 10_000)],
                  {"now": [_item("HOLD", 10_000, 9_500)]}, out_dir=tmp)
        before = open(ap.STATE_PATH, "rb").read()
        ap._trading_lock_status = lambda run_id: (_ for _ in ()).throw(
            AssertionError("배포 전용 런이 매매 락에 접근함"))
        from bot import notify
        original_send = notify.send
        notify.send = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("배포 전용 런이 알림을 전송함"))
        os.environ["AUTOPAPER_READ_ONLY"] = "1"
        try:
            out = ap.update([_row("HOLD", 9_000), _row("DEPLOY", 10_000)],
                            {"now": [_item("DEPLOY", 10_000, 9_500)]},
                            out_dir=tmp)
        finally:
            os.environ.pop("AUTOPAPER_READ_ONLY", None)
            notify.send = original_send
        after = open(ap.STATE_PATH, "rb").read()
        with open(".github/workflows/daily.yml", encoding="utf-8") as fp:
            workflow = fp.read()
        wiring = (
            "AUTOPAPER_READ_ONLY: ${{ github.event_name == 'push' && '1' || '0' }}"
            in workflow
            and "if: github.event_name != 'push'" in workflow
        )
        codes = {row["code"] for row in out["positions"]}
        if codes != {"HOLD"} or out["pending"]:
            fails.append("코드 push 배포 전용 런이 모의계좌를 변경함")
        elif not out.get("publish_only"):
            fails.append("배포 전용 표시(publish_only) 누락")
        elif before != after:
            fails.append("코드 push 배포 전용 런이 계좌 상태를 저장함")
        elif not wiring:
            fails.append("daily.yml 배포 전용 안전 배선 누락")
        else:
            print("  [PASS] 코드 push → 매매·상태·락·알림 없는 표시 전용")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 빠른 차선 안전장치 5종 전부 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
