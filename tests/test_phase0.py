"""Phase 0 운영 안전장치 검증 — 2차 전략 검토 실행 메모의 '오늘 바로 넣을 것'.

  1) 손절 후 재진입 쿨다운(당일)          — churn 차단
  2) 하루 신규 진입 상한(3개)             — 신호 몰림 차단
  3) 일일 실현손실 −2% 서킷브레이커       — 꼬리위험 차단
  4) pending 주문 게이트 재검사(신호 부패) — 추천 기준 증빙 유지
  5) 어닝 D-1 보유 관리(MFE 사다리)       — 갭 무방비 해소

실행: python -m tests.test_phase0
"""
from __future__ import annotations

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


def _item(code, price, stop, atr=None):
    it = {"code": code, "name": code, "ccy": "KRW", "price": price,
          "stop": stop, "target": price + 2 * (price - stop), "earnings_d": None,
          "tactic": {"mode": "full", "stop_pct": (price - stop) / price * 100}}
    if atr:
        it["atr"] = atr
    return it


def _row(code, price, **kw):
    r = {"code": code, "name": code, "ccy": "KRW", "sr": {"price": price}}
    r.update(kw)
    return r


def _run(rows, items, tmp):
    return ap.update(rows, {"now": items}, out_dir=tmp)


def main() -> int:
    fails = []

    # 1) 손절 후 당일 재진입 금지
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        it = _item("CD", 10_000, 9_900)              # atr 없음 → 터치 즉시 손절
        _run([_row("CD", 10_000)], [it], tmp)
        out = _run([_row("CD", 9_800)], [], tmp)     # 손절
        assert not out["positions"], "손절 안 됨"
        out = _run([_row("CD", 10_000)], [it], tmp)  # 같은 날 신호 재등장
        if out["positions"]:
            fails.append("손절 당일 재진입 차단 실패")
        else:
            print("  [PASS] 손절 당일 재진입 차단")

    # 2) 하루 신규 진입 ≤ 3
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        items = [_item(f"D{i}", 10_000, 9_500) for i in range(5)]  # 각 ~20% 비중
        rows = [_row(f"D{i}", 10_000) for i in range(5)]
        out = _run(rows, items, tmp)
        n = len(out["positions"]) + len(out["pending"])
        if n != 3:
            fails.append(f"하루 신규 진입 {n}건 (기대 3)")
        else:
            print(f"  [PASS] 하루 신규 진입 상한: 5개 신호 중 {n}개만 진입")
        if out.get("rule_violations"):
            fails.append(f"규칙 감사 오탐: {out['rule_violations']}")

    # 3) 일일 실현손실 −2% 서킷브레이커
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        _run([_row("CB", 10_000)], [_item("CB", 10_000, 9_900)], tmp)  # 1/3 상한까지 매수
        out = _run([_row("CB", 9_000)], [], tmp)     # −10% 급락 → 실현손실 ≈ −3.3%
        assert not out["positions"], "손절 안 됨"
        out = _run([_row("NEW", 10_000)], [_item("NEW", 10_000, 9_500)], tmp)
        if out["positions"] or out["pending"]:
            fails.append("서킷브레이커 미작동 — 큰 손실 후에도 신규 진입")
        else:
            print("  [PASS] 일일 서킷브레이커: −3.3% 실현 후 신규 진입 중지")

    # 4) pending 신호 부패 재검사
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        it = _item("PB", 50_000, 49_000)
        it["tactic"] = {"mode": "pullback", "pb_price": 49_500, "stop_pct": 2.0}
        out = _run([_row("PB", 50_000)], [it], tmp)
        assert out["pending"], "지정가 주문 생성 실패"
        # 다음 빌드: 같은 종목이 하락추세 veto에 걸림 → 주문 취소돼야 함
        out = _run([_row("PB", 49_800, vetoed=True)], [], tmp)
        if out["pending"]:
            fails.append("신호 부패(veto) 후에도 지정가 주문 생존")
        else:
            note = out["log"][0]["note"]
            ok = "신호 부패" in note
            print(f"  [{'PASS' if ok else 'FAIL'}] pending 재검사 취소 · 사유: {note}")
            if not ok:
                fails.append(f"취소 사유 이상: {note}")

    # 5) 어닝 D-1 보유 관리 — MFE<0.5R 전량 / half_done은 통과 허용
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        _run([_row("EA", 100_000)], [_item("EA", 100_000, 95_000, atr=2_000)], tmp)
        ap._earnings_d = lambda code: 1              # 내일 어닝
        out = _run([_row("EA", 100_500)], [], tmp)   # MFE≈0.1R → 전량 정리
        if out["positions"]:
            fails.append("어닝 D-1 무진행 포지션이 정리 안 됨")
        else:
            note = out["closed"][0]["exits"][-1]["note"]
            print(f"  [PASS] 어닝 D-1 전량 정리 · 사유: {note}")
            if "어닝" not in note:
                fails.append(f"정리 사유 이상: {note}")
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        _run([_row("EB", 100_000)], [_item("EB", 100_000, 95_000, atr=2_000)], tmp)
        _run([_row("EB", 105_000)], [], tmp)         # +1R 1회
        _run([_row("EB", 105_000)], [], tmp)         # 2연속 → 절반+본전
        ap._earnings_d = lambda code: 1
        out = _run([_row("EB", 105_500)], [], tmp)
        if not out["positions"]:
            fails.append("+1R 달성(본전 잠김) 포지션이 어닝에 정리됨(통과 허용 위반)")
        else:
            print("  [PASS] 어닝 D-1: +1R 달성 포지션은 통과 허용")

    # 6) 전일 주문 체결은 일일 카운터에 불포함(이중 카운트 방지)
    #    실측 사고(2026-07-07 12:21): 전일 셀트리온 지정가가 오늘 체결되며
    #    주문일 +1, 체결일 +1로 이중 카운트 → '4건>상한3' 위반 오탐 경보.
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        st = {"v": ap.VERSION, "cash": ap.START, "start": ap.START,
              "pos": {}, "log": [],
              "pending": {"PB": {"name": "PB", "ccy": "KRW", "limit": 49_500,
                                 "stop": 49_000, "target": 52_000, "q": 10,
                                 "created": "2026-07-06", "atr": 500,
                                 "plan": None, "ctx": None, "basis": "t"}},
              "day_ent": {"d": ap._today(), "n": 3}}   # 오늘 결정 이미 만석
        ap._save(st)
        row = {"code": "PB", "name": "PB", "ccy": "KRW",
               "sr": {"price": 49_400}, "turnover": 10_000_000_000}
        out = _run([row], [], tmp)
        n_after = _json.load(open(ap.STATE_PATH))["day_ent"]["n"]
        if not any(p["code"] == "PB" for p in out["positions"]):
            fails.append("전일 주문이 카운터 만석에 체결 차단됨(체결은 허용돼야)")
        elif out.get("rule_violations"):
            fails.append(f"체결 이중 카운트 위반 오탐: {out['rule_violations']}")
        elif n_after != 3:
            fails.append(f"체결이 일일 카운터 증가시킴: {n_after}")
        else:
            print("  [PASS] 전일 주문 체결 — 카운터 불변·위반 오탐 없음")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ Phase 0 안전장치 6종 전부 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
