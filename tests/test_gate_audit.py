"""CI용 추천 불변식 스모크 — 정상 결과 통과·손절 역전 결과 차단."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import gates


def test_valid_passes_and_invalid_fails():
    good = {"code": "GOOD", "entry": 100.0, "entry_kind": "now",
            "sr": {"price": 100.0}, "risk": {"stop": 95.0, "target": 120.0}}
    gates.audit([good], {"now": [], "watch": []})

    bad = {"code": "BAD", "entry": 100.0, "entry_kind": "now",
           "sr": {"price": 100.0}, "risk": {"stop": 101.0, "target": 120.0}}
    try:
        gates.audit([bad], {"now": [], "watch": []})
    except RuntimeError:
        print("[PASS] audit: 정상 통과·손절≥진입 위반 차단")
        return
    raise AssertionError("gates.audit가 손절 역전 결과를 허용함")


def main():
    test_valid_passes_and_invalid_fails()
    print("\n추천 불변식 CI 스모크 통과.")


if __name__ == "__main__":
    main()
