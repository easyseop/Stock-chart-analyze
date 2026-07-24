"""종목당 1/3 상한 검증 — '어영부영' 없이 규칙이 실제로 지켜지는지 증명.

사용자 요청: "매매할땐 한 종목에 총시드의 1/3 이상을 들어가지 않도록 기준잡아줘.
제발 어영부영하지말고 제대로 기준이 들어갔는지 확인해줬으면해."

세 전술(즉시/반반/눌림) 모두에 대해, 사이징이 아주 큰 수량을 원하도록
'손절폭을 극단적으로 좁게'(리스크 사이징이 100%+를 원함) 만든 뒤,
어떤 종목의 '투입 원가(보유+대기 합산)'도 총자산의 1/3을 넘지 않음을 검증한다.

실행:  python -m tests.test_pos_cap          (또는 pytest tests/test_pos_cap.py)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner.autopaper as ap


def _fresh_state(tmp: str) -> None:
    """테스트마다 깨끗한 상태 + 장 항상 열림으로 강제."""
    ap.STATE_PATH = os.path.join(tmp, "autopaper.json")
    for p in (ap.STATE_PATH, ap.STATE_PATH + ".bak", ap.STATE_PATH + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    ap._state_branch_snapshot = lambda: None
    ap._market_open = lambda ccy: True     # 시간/요일 무관하게 체결 경로 활성화


def _run(item: dict, tmp: str) -> dict:
    """단일 종목 픽으로 한 스텝 진행하고 out(paper_auto.json 내용) 반환."""
    r = {"code": item["code"], "name": item["name"], "ccy": item["ccy"],
         "sr": {"price": item["price"]}}
    return ap.update([r], {"now": [item]}, out_dir=tmp)


def _cost_by_code(out: dict) -> dict:
    """out에서 종목별 투입 원가(보유 평단×수량 + 대기 지정가×수량, 원화)."""
    fx = ap.FX
    krw = lambda v, ccy: v * fx if ccy == "USD" else v
    cost = {}
    for p in out["positions"]:
        cost[p["code"]] = cost.get(p["code"], 0) + krw(p["avg"], p["ccy"]) * p["q"]
    for o in out["pending"]:
        cost[o["code"]] = cost.get(o["code"], 0) + krw(o["limit"], o["ccy"]) * o["q"]
    return cost


# 손절폭 1%(=리스크 사이징이 계좌의 100%를 원함) → 반드시 1/3 상한이 물려야 함
CASES = [
    # 즉시(full) — 전량 시장가
    {"code": "FULL_KR", "name": "풀KR", "ccy": "KRW", "price": 10_000,
     "stop": 9_900, "target": 10_200, "earnings_d": None,
     "tactic": {"mode": "full", "stop_pct": 1.0}},
    {"code": "FULL_US", "name": "풀US", "ccy": "USD", "price": 200,
     "stop": 198, "target": 204, "earnings_d": None,
     "tactic": {"mode": "full", "stop_pct": 1.0}},
    # 반반(half) — 절반 시장가 + 절반 눌림 지정가 (pos+pending 동시 존재)
    {"code": "HALF_US", "name": "반반US", "ccy": "USD", "price": 200,
     "stop": 196, "target": 208, "earnings_d": None,
     "tactic": {"mode": "half", "pb_price": 198, "stop_pct": 2.0}},
    {"code": "HALF_KR", "name": "반반KR", "ccy": "KRW", "price": 50_000,
     "stop": 49_500, "target": 51_000, "earnings_d": None,
     "tactic": {"mode": "half", "pb_price": 49_800, "stop_pct": 1.0}},
    # 눌림(pullback) — 지정가만
    {"code": "PB_KR", "name": "눌림KR", "ccy": "KRW", "price": 50_000,
     "stop": 49_000, "target": 52_000, "earnings_d": None,
     "tactic": {"mode": "pullback", "pb_price": 49_500, "stop_pct": 2.0}},
]


def test_single_stock_never_exceeds_one_third(tmp_dir: str) -> list[str]:
    fails = []
    for case in CASES:
        _fresh_state(tmp_dir)
        out = _run(dict(case), tmp_dir)
        equity = out["equity"]
        cap = equity / 3
        cost = _cost_by_code(out)
        # 1) 이 종목의 투입 원가가 1/3 이하인지
        c = cost.get(case["code"], 0)
        pct = c / equity * 100 if equity else 0
        ok = c <= cap * 1.005      # 반올림·환율 오차 0.5% 여유
        # 2) 뭔가 실제로 체결/주문됐는지(빈 통과가 아님을 보장)
        placed = c > 0
        # 3) 엔진 자체 감사도 위반 0이어야 함
        no_viol = not out.get("cap_violations")
        tag = "PASS" if (ok and placed and no_viol) else "FAIL"
        if tag == "FAIL":
            fails.append(f"{case['code']}: 투입 {c:,.0f}원 = {pct:.1f}% "
                         f"(cap {cap:,.0f}원, placed={placed}, viol={not no_viol})")
        print(f"  [{tag}] {case['code']:8s} {case['tactic']['mode']:8s} "
              f"투입 {c:>13,.0f}원 = {pct:5.1f}% ≤ 33.3% "
              f"(cap {cap:,.0f}원)")
    return fails


def test_reported_cap_metadata(tmp_dir: str) -> list[str]:
    _fresh_state(tmp_dir)
    out = _run(dict(CASES[0]), tmp_dir)
    fails = []
    if abs(out.get("pos_cap_pct", 0) - 33.3) > 0.1:
        fails.append(f"pos_cap_pct={out.get('pos_cap_pct')} (기대 33.3)")
    if out.get("cap_violations") != []:
        fails.append(f"cap_violations={out.get('cap_violations')} (기대 [])")
    print(f"  [{'PASS' if not fails else 'FAIL'}] 메타데이터 "
          f"pos_cap_pct={out.get('pos_cap_pct')} viol={out.get('cap_violations')}")
    return fails


def test_audit_catches_violation() -> list[str]:
    """감사가 '무조건 통과'하는 껍데기가 아님을 증명 — 일부러 1/3 초과
    포지션을 심으면 _audit_caps가 잡아내야 한다."""
    st = {"cash": 0, "start": ap.START, "pos": {
        "BAD": {"name": "위반", "ccy": "KRW", "q": 500, "avg": 100_000}},
        "pending": {}}
    equity = 100_000_000                 # 500×100,000 = 5천만원 = 50% > 33.3%
    viol = ap._audit_caps(st, equity)
    ok = len(viol) == 1 and viol[0]["code"] == "BAD"
    print(f"  [{'PASS' if ok else 'FAIL'}] 위반 주입 감지: {viol}")
    return [] if ok else [f"감사가 위반을 못 잡음: {viol}"]


def main() -> int:
    import tempfile
    print(f"POS_CAP = {ap.POS_CAP:.6f} (= 1/3 = {1/3:.6f})")
    assert abs(ap.POS_CAP - 1 / 3) < 1e-9, "POS_CAP이 1/3이 아님!"
    all_fails = []
    with tempfile.TemporaryDirectory() as tmp:
        print("\n[테스트1] 3전술 × 단일종목 — 어떤 종목도 1/3 초과 금지:")
        all_fails += test_single_stock_never_exceeds_one_third(tmp)
        print("\n[테스트2] 출력 메타데이터(비중 상한·위반 목록):")
        all_fails += test_reported_cap_metadata(tmp)
    print("\n[테스트3] 감사 실효성 — 위반 주입 시 반드시 잡힘:")
    all_fails += test_audit_catches_violation()
    print()
    if all_fails:
        print("❌ 실패:")
        for f in all_fails:
            print("   -", f)
        return 1
    print("✅ 전부 통과 — 어떤 전술·통화에서도 종목당 투입이 총자산의 1/3을 넘지 않음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
