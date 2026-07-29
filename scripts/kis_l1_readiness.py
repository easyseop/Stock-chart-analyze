#!/usr/bin/env python3
"""Oracle KIS L1 하향 전 읽기 전용 GO/NO-GO 점검기."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import l1_readiness  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="주문·L1 변경 없이 L1 하향 전 기술 게이트를 점검합니다.")
    parser.add_argument(
        "--evidence",
        help="관찰 증거 JSON 경로(기본 L1_READINESS_EVIDENCE_PATH)")
    parser.add_argument(
        "--broker", action="store_true",
        help="KIS 잔고·미체결을 읽기 전용으로 조회해 수량을 대조")
    parser.add_argument(
        "--scope", choices=sorted(l1_readiness.SCOPES), default="strict",
        help="strict=모든 기능 증거, l0=제한적 mock 신규매수 핵심 게이트")
    parser.add_argument(
        "--json", action="store_true", help="기계 판독용 JSON 출력")
    args = parser.parse_args(argv)

    evidence = l1_readiness.load_evidence(args.evidence)
    snapshot = l1_readiness.collect_runtime(
        fetch_broker=args.broker, evidence=evidence)
    report = l1_readiness.evaluate(snapshot, evidence, scope=args.scope)
    print(json.dumps(report, ensure_ascii=False, indent=2)
          if args.json else l1_readiness.render_text(report))
    return 0 if report["ready_for_operator_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
