#!/usr/bin/env python3
"""수익 매도 사후추적 일봉을 갱신하고 읽기 전용 JSON을 발행한다."""
from __future__ import annotations

import argparse
import json

from bot import post_exit


def main() -> int:
    parser = argparse.ArgumentParser(description="익절 사후추적 공개 일봉 갱신")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = post_exit.refresh_published(limit=args.limit)
    summary = payload.get("summary") or {}
    refresh = payload.get("refresh") or {}
    output = {
        "generated_at": payload.get("generated_at"),
        "profitable_exits": summary.get("profitable_exits", 0),
        "tracked_exits": summary.get("tracked_exits", 0),
        "symbols": refresh.get("symbols", 0),
        "failed_symbols": refresh.get("failed_symbols", []),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2)
          if args.json else
          f"익절 사후추적 {output['tracked_exits']}/{output['profitable_exits']}건 "
          f"· 종목 {output['symbols']} · 실패 {len(output['failed_symbols'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
