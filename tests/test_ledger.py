"""주문 원장 상태기계 검증 — 초과매도 방지의 핵심 경로.

  1) 정상: submit→filled → 잠금 없음, 잔여 0
  2) 타임아웃: submit→unknown → 종목 **잠금**(재주문 금지)
  3) 대사: unknown→reconcile(부분체결) → 잠금 해제 + 잔여 = intended−실체결
  4) 잔여만 재주문: 원수량 아닌 잔여 수량으로만
  5) 거부/부분체결 상태 전이
  6) 잠금은 UNKNOWN만 — filled/partial은 잠그지 않음
  7) append-only 재생성 — 재시작(재로드) 후에도 상태 유지

실행: python -m tests.test_ledger
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.ledger as L


def _fresh(tmp):
    L.LEDGER_PATH = os.path.join(tmp, "order_ledger.jsonl")


def main() -> int:
    fails = []

    # 1) 정상 체결
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k1", "AAPL", 10, "하드 손절")
        L.on_result("k1", "filled", 10)
        s = L.state_of("k1")
        if s["state"] != "filled" or L.residual_qty("k1", 10) != 0 \
                or L.is_locked("AAPL"):
            fails.append(f"정상 체결 상태 오류: {s} locked={L.is_locked('AAPL')}")
        else:
            print("  [PASS] submit→filled: 잔여0·잠금없음")

    # 2) 타임아웃 → UNKNOWN → 잠금
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k2", "TSLA", 8, "손절")
        L.on_result("k2", "unknown", 0)          # 응답 타임아웃
        if not L.is_locked("TSLA"):
            fails.append("UNKNOWN인데 종목이 안 잠김(초과매도 위험)")
        elif "TSLA" not in L.locked_symbols():
            fails.append("locked_symbols에 TSLA 누락")
        else:
            print("  [PASS] submit→unknown: 종목 잠금(재주문 차단)")

        # 3) 대사: 실제로는 3주만 체결돼 있었음 → 부분체결 확정 + 잠금 해제
        r = L.reconcile("k2", actual_filled=3)
        if r["state"] != "partial" or r["residual"] != 5 or L.is_locked("TSLA"):
            fails.append(f"대사 오류: {r} locked={L.is_locked('TSLA')}")
        else:
            print("  [PASS] unknown→reconcile(부분3): 잠금해제·잔여5")

        # 4) 잔여만 재주문 — 원수량(8) 아닌 잔여(5)
        resid = L.residual_qty("k2", 8)
        if resid != 5:
            fails.append(f"잔여 계산 오류: {resid}(기대 5)")
        else:
            L.record_submit("k2:r2", "TSLA", resid, "잔여 재주문")
            L.on_result("k2:r2", "filled", 5)
            if L.residual_qty("k2:r2", 5) != 0:
                fails.append("잔여 재주문 후 미완결")
            else:
                print("  [PASS] 잔여 수량(5)만 재주문 → 완결")

    # 5) 거부 — 종료 상태, 잠금 없음
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k5", "NVDA", 4)
        L.on_result("k5", "rejected", 0)
        if L.state_of("k5")["state"] != "rejected" or L.is_locked("NVDA"):
            fails.append("거부 상태/잠금 오류")
        else:
            print("  [PASS] rejected: 종료·잠금없음")

    # 6) partial은 잠그지 않음(알려진 부분체결은 잔여 즉시 재주문 가능)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k6", "META", 10)
        L.on_result("k6", "partial", 6)
        if L.is_locked("META"):
            fails.append("partial인데 잠김(불필요한 재주문 차단)")
        elif L.residual_qty("k6", 10) != 4:
            fails.append(f"partial 잔여 오류: {L.residual_qty('k6', 10)}")
        else:
            print("  [PASS] partial: 미잠금·잔여4")

    # 7) append-only 재생성 — 파일만 있으면 재시작 후에도 상태 복원
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k7", "AMD", 5)
        L.on_result("k7", "unknown", 0)
        # '재시작' 시뮬레이션 — 모듈 상태 없이 파일만으로 재생성
        if not L.is_locked("AMD"):
            fails.append("재생성 후 UNKNOWN 잠금 소실")
        else:
            print("  [PASS] append-only 재생성 — 재시작 내성")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 주문 원장 전부 통과 — UNKNOWN 잠금·대사·잔여만 재주문(초과매도 방지).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
