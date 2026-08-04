#!/usr/bin/env python3
"""오염된 마감 기록 1일을 성과 누적에서 제거한다(주문 없음·읽기 후 1파일 쓰기).

용도: 매도 회계 지연으로 유령 손실이 각인된 채 마감된 날을 누적 통계에서 뺀다.
  (2026-08-03 미장: 브로커 평가액만 먼저 줄고 원장 현금흐름이 늦게 붙어 계좌
   수익률이 -8.69%로 기록됨 — 실제 손실이 아니라 표시 오류.)

안전장치:
  · 기본은 미리보기(dry-run) — 실제 삭제는 --apply 필요.
  · 삭제 전 alpha 상태 파일을 타임스탬프 백업으로 남긴다.
  · 지수·carry·nav 기준선은 건드리지 않는다(그날 계좌 수익률 항목만 제거).
  · 지운 날은 되살리지 않는다 — 백업에서 복원해야 한다.

사용:
  python scripts/alpha_drop_day.py --date 2026-08-03 --mkt US
  python scripts/alpha_drop_day.py --date 2026-08-03 --mkt US --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="성과 누적에서 하루 제거")
    ap.add_argument("--date", required=True, help="제거할 날짜(YYYY-MM-DD)")
    ap.add_argument("--mkt", default="US", choices=("US", "KR"))
    ap.add_argument("--apply", action="store_true", help="실제 저장(기본은 미리보기)")
    args = ap.parse_args(argv)

    st = alpha._load()
    days = st.get("days") or []
    hit = [row for row in days
           if str(row.get("d")) == args.date and str(row.get("mkt")) == args.mkt]
    if not hit:
        print(f"✗ {args.mkt} {args.date} 기록이 없습니다 — 변경 없음")
        return 1
    for row in hit:
        print(f"제거 대상: {row.get('mkt')} {row.get('d')} "
              f"계좌 {row.get('acct')}% · 지수 {row.get('idx')}%")
    if not args.apply:
        print(f"\n미리보기입니다. 실제로 지우려면 --apply 를 붙이세요.")
        return 0

    path = alpha.STATE_PATH
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    st["days"] = [row for row in days
                  if not (str(row.get("d")) == args.date
                          and str(row.get("mkt")) == args.mkt)]
    alpha._save(st)
    print(f"✓ {len(hit)}건 제거 · 백업 {backup}")
    print(f"  남은 누적 일수: {len(st['days'])}일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
