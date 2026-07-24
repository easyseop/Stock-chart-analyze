"""트레일링 스탑(잔량 ATR 래칫) + 소프트/하드 집행 라이프사이클 검증.

시나리오: 진입 100k → +1R(105k) 2연속 확인 후 절반 익절+본전 → 급등 130k
(트레일 124k 래칫) → 하락 123.5k×2(소프트 트레일 확정) 청산. 검증 포인트:
  1) +1R이 '한 번 터치'로는 발동하지 않음(2연속 폴링 확인 — 스파이크 방지)
  2) 잔량이 2R(110k)에서 잘리지 않고 계속 감(트레일 무제한)
  3) 트레일 = max(본전, 최고가−3×ATR)로 한 방향(위로만) 래칫
  4) 손절/트레일도 소프트(2연속)·하드(−0.5ATR 즉시) 이중 집행
  5) 구(舊) 포지션(atr0 없음)은 기존 터치 즉시 + 2R 목표 규칙 유지(회귀 방지)

실행: python -m tests.test_trail
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner.autopaper as ap


def _fresh(tmp: str) -> None:
    ap.STATE_PATH = os.path.join(tmp, "autopaper.json")
    for p in (ap.STATE_PATH, ap.STATE_PATH + ".bak"):
        if os.path.exists(p):
            os.remove(p)
    ap._state_branch_snapshot = lambda: None
    ap._trading_lock_status = lambda run_id: "off"
    ap._market_open = lambda ccy: True
    ap._earnings_d = lambda code: None      # 어닝 개입 차단(트레일만 검증)


ITEM = {"code": "TR", "name": "트레일주", "ccy": "KRW", "price": 100_000,
        "stop": 95_000, "target": 110_000, "earnings_d": None,
        "atr": 2_000,                       # 3×ATR=6,000 트레일 / 0.5×ATR=1,000 하드
        "tactic": {"mode": "full", "stop_pct": 5.0}}


def _step(price: float, tmp: str, item=None) -> dict:
    r = {"code": "TR", "name": "트레일주", "ccy": "KRW", "sr": {"price": price}}
    return ap.update([r], {"now": [item] if item else []}, out_dir=tmp)


def main() -> int:
    import tempfile
    fails = []

    # ── 시나리오 1: 풀 라이프사이클(2연속 확인 + 트레일 무제한) ──
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        out = _step(100_000, tmp, dict(ITEM))
        pos = {p["code"]: p for p in out["positions"]}
        assert "TR" in pos, "진입 실패"
        q0 = pos["TR"]["q"]
        print(f"  진입 {q0}주 @100,000 (손절 95,000 · ATR 2,000)")

        # +1R 첫 터치 — 아직 안 팔려야 함(2연속 확인)
        out = _step(105_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["q"] != q0:
            fails.append("+1R 1회 터치에 절반 익절됨(2연속 확인 위반)")
        # +1R 2연속 → 절반 익절 + 본전
        out = _step(105_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["q"] != q0 - q0 // 2:
            fails.append(f"절반 익절 수량 이상: {pos['TR']['q']}")
        if pos["TR"]["stop"] != 100_000:
            fails.append(f"본전 스탑 아님: {pos['TR']['stop']}")
        print(f"  +1R 2연속 확인: 절반 익절 → 잔량 {pos['TR']['q']}주 · 스탑 100,000")

        # 2R 통과 — 안 팔리고 트레일만 래칫
        out = _step(110_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if "TR" not in pos:
            fails.append("2R에서 팔림(트레일 무제한 회귀)")
        elif pos["TR"]["stop"] != 104_000:
            fails.append(f"트레일 래칫 오류: {pos['TR']['stop']} (기대 104,000)")

        # 고점 130k → 트레일 124k, 되밀림 126k에도 유지(한 방향)
        _step(130_000, tmp)
        out = _step(126_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["stop"] != 124_000:
            fails.append(f"래칫 위반: {pos['TR']['stop']} (기대 124,000)")
        print(f"  2R 통과·고점 130,000 → 트레일 {pos['TR']['stop']:,} 유지")

        # 소프트 트레일: 123.5k(스탑 124k 이하, 하드 123k 위) 1회차 → 보유 유지
        out = _step(123_500, tmp)
        if not any(p["code"] == "TR" for p in out["positions"]):
            fails.append("트레일 1회 터치에 즉시 청산(소프트 확인 위반)")
        # 2회차 → 청산
        out = _step(123_500, tmp)
        if any(p["code"] == "TR" for p in out["positions"]):
            fails.append("트레일 2연속인데 청산 안 됨")
        closed = out["closed"][0] if out.get("closed") else {}
        r = closed.get("r", 0)
        notes = [e["note"] for e in closed.get("exits", [])]
        if not any("트레일" in n for n in notes):
            fails.append(f"청산 사유에 트레일 없음: {notes}")
        if not r > 1.5:
            fails.append(f"성과 {r}R ≤ 1.5R (2R 캡 대비 이득 없음)")
        print(f"  소프트 트레일(2연속) 청산 · 최종 {r:+.2f}R · {notes}")

    # ── 시나리오 2: 하드 손절(−0.5ATR 이탈 즉시) ──
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        _step(100_000, tmp, dict(ITEM))
        out = _step(93_000, tmp)            # 하드선 94,000(=95k−0.5×2k) 이탈
        if any(p["code"] == "TR" for p in out["positions"]):
            fails.append("하드선 이탈인데 즉시 청산 안 됨")
        else:
            note = out["closed"][0]["exits"][-1]["note"]
            print(f"  하드 손절 즉시 집행 확인 · 사유 {note}")
            if "급락" not in note:
                fails.append(f"하드 손절 사유 이상: {note}")

    # ── 시나리오 3: 소프트 손절(스탑~하드 사이 2연속) ──
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        _step(100_000, tmp, dict(ITEM))
        out = _step(94_500, tmp)            # 스탑 95k 이하, 하드 94k 위 — 1회차
        if not any(p["code"] == "TR" for p in out["positions"]):
            fails.append("소프트 손절 1회 터치에 즉시 청산됨")
        out = _step(94_500, tmp)            # 2회차 → 집행
        if any(p["code"] == "TR" for p in out["positions"]):
            fails.append("소프트 손절 2연속인데 청산 안 됨")
        else:
            print("  소프트 손절(2연속 확인) 집행 확인")

    # ── 시나리오 4: 구 포지션(atr0 없음) — 터치 즉시 손절 + 2R 목표 유지 ──
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        legacy = dict(ITEM)
        legacy.pop("atr")
        _step(100_000, tmp, legacy)
        _step(105_000, tmp)                 # r1 1회
        _step(105_000, tmp)                 # r1 2회 → 절반+본전
        out = _step(110_000, tmp)           # 2R → 구 규칙이면 전량 익절
        if any(p["code"] == "TR" for p in out["positions"]):
            fails.append("구 포지션(atr0 없음)이 2R에서 안 팔림")
        else:
            print("  구 포지션(ATR 없음): 2R 익절 규칙 유지 확인")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전부 통과 — 2연속 확인·트레일 래칫·하드/소프트·구버전 호환 모두 정상.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
