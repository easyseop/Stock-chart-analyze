#!/usr/bin/env python3
"""진행 중인 세션의 성과 기준점을 지금으로 다시 잡는다(주문 없음·상태 1파일).

용도: 오염된 구간이 이미 세션 누적(wealth)에 곱해져 남은 경우. 그 구간의 올바른
값은 사후 복원이 불가능하므로(틱별 원본 미보존) 지어내지 않는다. 대신 **계좌와
지수를 같은 순간에 함께 0%로** 다시 잡아, 재기준 이후 비교만 정확하게 만든다.

왜 계좌만 재기준하면 안 되나: 지수는 세션 시작 기준을 유지한 채 계좌만 지금을
0%로 만들면, 서로 다른 시점을 뺀 값이 화면에 "지수 대비 성과"로 표시된다.
그 불일치가 이 프로젝트가 반복해서 겪은 거짓 비교의 원인이다.

안전장치:
  · 기본은 미리보기(dry-run) — 실제 변경은 --apply 필요.
  · 변경 전 상태 파일을 타임스탬프 백업.
  · 그날은 누적 기록(days)에 넣지 않도록 표시 — 반쪽 세션을 장기 통계에 섞지 않음.
  · 다음 틱이 스스로 새 기준점을 잡는다(이 스크립트는 시세를 조회하지 않는다).

사용:
  python scripts/alpha_reanchor.py --mkt US
  python scripts/alpha_reanchor.py --mkt US --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="진행 중 세션 성과 기준점 재설정")
    ap.add_argument("--mkt", default="US", choices=("US", "KR"))
    ap.add_argument("--apply", action="store_true", help="실제 저장(기본 미리보기)")
    args = ap.parse_args(argv)

    st = alpha._load()
    day = (st.get("day") or {}).get(args.mkt)
    if not day:
        print(f"✗ {args.mkt} 진행 중인 세션이 없습니다 — 변경 없음")
        return 1
    series = day.get("series") or []
    last = series[-1] if series else None
    print(f"대상: {args.mkt} {day.get('date')} · 표본 {len(series)}개 "
          f"· basis={day.get('basis')}")
    if last:
        print(f"  현재 표시: 계좌 {last[1]}% · 지수 {last[2]}%")
    print(f"  이상 구간 기록: {len(day.get('anomaly_log') or [])}건 "
          f"· 회계보류={bool(day.get('accounting_pending'))}")
    print("\n재설정하면: 계좌·지수 모두 다음 틱을 0%로 잡고 그 이후만 비교합니다.")
    print("  (지금까지의 세션 표시는 버려집니다 — 복원 불가 구간이라 추정하지 않음)")
    if not args.apply:
        print("\n미리보기입니다. 실제로 적용하려면 --apply 를 붙이세요.")
        return 0

    path = alpha.STATE_PATH
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)

    # alpha.tick()과 같은 파일을 쓰므로 잠금 안에서 **다시 읽고** 수정한다.
    #   종전에는 잠금 없이 오래된 사본을 저장해 동시 진행 중이던 다른 시장의
    #   틱을 통째로 덮어썼다(Codex P2-2 재현).
    session_date = str(day.get("date") or "")
    with alpha.state_lock():
        st = alpha._load()
        st.get("day", {}).pop(args.mkt, None)
        st.get("carry", {}).pop(args.mkt, None)
        st.setdefault("reanchored", {})[args.mkt] = {
            "at": time.time(), "date": session_date,
            "dropped_samples": len(series),
            "was": (last[1] if last else None),
        }
        alpha._save(st)
    print(f"✓ {args.mkt} 세션 기준점 재설정 · 표본 {len(series)}개 폐기")
    print(f"  백업: {backup}")
    print("  다음 틱(최대 5분)에 계좌·지수가 함께 0%로 다시 시작합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
