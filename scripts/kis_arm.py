#!/usr/bin/env python3
"""IS2 arming — 사용자 기보유 심볼(baseline denylist) 캡처. 읽기 전용·주문 없음.

봇이 매수를 시작하기 전 1회 실행한다("무장"). NASD/NYSE/AMEX 잔고를 읽어
사용자 기보유 심볼을 baseline으로 고정 저장 — 그 심볼은 봇의 **영구 매수 금지**
(같은 종목 매수→병합→소유 경계 소실 차단, IS2). 규칙:
  · 조회가 하나라도 실패하면 캡처하지 않는다(fail-closed — 빈 denylist 사고 방지).
  · baseline은 재실행해도 **줄지 않는다**(새 외부 심볼 병합만).
  · 모의 새 계좌면 보유 0 → 빈 baseline(정상 arming — "깨끗한 계좌" 확정).

사용: (kis_probe.py와 동일한 KIS_* 환경변수 세팅 후)
  python scripts/kis_arm.py
  python scripts/kis_arm.py --show     # 캡처 없이 현재 baseline만 표시
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis, ownership  # noqa: E402

EXCGS = ("NASD", "NYSE", "AMEX")


def main() -> int:
    ap = argparse.ArgumentParser(description="IS2 baseline 캡처(읽기 전용)")
    ap.add_argument("--show", action="store_true", help="현재 baseline만 표시")
    args = ap.parse_args()

    if args.show:
        b = ownership.baseline()
        print("baseline: " + ("미캡처(매수 전면 거부 상태)" if b is None
                              else (", ".join(sorted(b)) or "(빈 목록 — 깨끗한 계좌)")))
        return 0

    if not (kis.enabled() and kis.account()):
        print("✗ KIS 키/계좌 환경변수 필요(kis_probe.py와 동일)")
        return 1
    print(f"env={kis.ENV} — {'/'.join(EXCGS)} 잔고 읽는 중(주문 없음)…")

    rows: list[dict] = []
    for ex in EXCGS:
        d = kis.overseas_balance(excg=ex)
        if d is None or d.get("rt_cd") != "0":
            print(f"✗ {ex} 잔고 조회 실패 — fail-closed: 캡처하지 않음(재시도 요망)")
            return 1
        rows += (d.get("output1") or [])
        time.sleep(0.7)                       # 모의 2/s 유량

    # 국내(KR) 기보유도 캡처 — 잔고대사 사용자-주식 배제 가드가 KR에도 살아있게.
    #   (누락 시 국내 대사가 사용자 기보유 KR주를 봇 것으로 오인할 위험 — 스트레스테스트)
    dk = kis.domestic_balance()
    if dk is None or dk.get("rt_cd") != "0":
        print("✗ 국내 잔고 조회 실패 — fail-closed: 캡처하지 않음(재시도 요망)")
        return 1
    rows += (dk.get("output1") or [])

    if not ownership.capture_baseline(rows):
        print("✗ baseline 저장 실패")
        return 1
    b = ownership.baseline() or set()
    print(f"✓ arming 완료 — baseline {len(b)}종목"
          + (f": {', '.join(sorted(b))}" if b else " (빈 목록 — 깨끗한 계좌 확정)"))
    print("  이 심볼들은 봇 매수 영구 금지. preflight의 IS2 경고가 해소됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
