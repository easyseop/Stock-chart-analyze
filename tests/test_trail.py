"""트레일링 스탑(잔량 ATR 래칫) 라이프사이클 검증.

시나리오: 진입 100 → +1R(110)에서 절반 익절+본전 스탑 → 급등 130(트레일 래칫)
→ 하락 118(트레일 터치)에서 잔량 청산. 검증 포인트:
  1) 절반 익절 후 잔량이 2R(120)에서 잘리지 않고 계속 감(트레일 무제한)
  2) 트레일 = max(본전, 최고가 − 3×ATR)로 한 방향(위로만) 래칫
  3) 트레일 터치 시 '트레일 스탑'으로 청산, 수익이 +1.5R 초과
  4) 구(舊) 포지션(atr0 없음)은 기존 2R 목표 규칙 유지(회귀 방지)

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
    ap._market_open = lambda ccy: True


ITEM = {"code": "TR", "name": "트레일주", "ccy": "KRW", "price": 100_000,
        "stop": 95_000, "target": 110_000, "earnings_d": None,
        "atr": 2_000,                       # 3×ATR = 6,000 트레일 폭
        "tactic": {"mode": "full", "stop_pct": 5.0}}


def _step(price: float, tmp: str, item=None) -> dict:
    r = {"code": "TR", "name": "트레일주", "ccy": "KRW", "sr": {"price": price}}
    return ap.update([r], {"now": [item] if item else []}, out_dir=tmp)


def main() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        # 1) 진입 @100,000 (손절 95,000 → 1R=5,000, +1R가=105,000)
        out = _step(100_000, tmp, dict(ITEM))
        pos = {p["code"]: p for p in out["positions"]}
        assert "TR" in pos, "진입 실패"
        q0 = pos["TR"]["q"]
        print(f"  진입 {q0}주 @100,000 (손절 95,000 · ATR 2,000)")

        # 2) +1R 도달(105,000) → 절반 익절 + 본전 스탑
        out = _step(105_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["q"] != q0 - q0 // 2:
            fails.append(f"절반 익절 수량 이상: {pos['TR']['q']}")
        if pos["TR"]["stop"] != 100_000:
            fails.append(f"본전 스탑 아님: {pos['TR']['stop']}")
        print(f"  +1R: 절반 익절 → 잔량 {pos['TR']['q']}주 · 스탑 {pos['TR']['stop']:,}")

        # 3) 2R(110,000) 도달해도 안 팔림 — 트레일만 래칫(110,000−6,000=104,000)
        out = _step(110_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if "TR" not in pos:
            fails.append("2R에서 팔려버림 — 트레일 무제한이 아님(캡 회귀)")
        elif pos["TR"]["stop"] != 104_000:
            fails.append(f"트레일 래칫 오류: {pos['TR']['stop']} (기대 104,000)")
        print(f"  2R 통과: 보유 유지 · 트레일 {pos.get('TR', {}).get('stop', 0):,}")

        # 4) 급등 130,000 → 트레일 124,000으로 래칫
        out = _step(130_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["stop"] != 124_000:
            fails.append(f"래칫 오류: {pos['TR']['stop']} (기대 124,000)")
        # 4b) 되밀림 126,000 — 트레일은 내려가면 안 됨(한 방향)
        out = _step(126_000, tmp)
        pos = {p["code"]: p for p in out["positions"]}
        if pos["TR"]["stop"] != 124_000:
            fails.append(f"트레일이 내려감(래칫 위반): {pos['TR']['stop']}")
        print(f"  고점 130,000 → 트레일 {pos['TR']['stop']:,} (되밀림에도 유지)")

        # 5) 트레일 터치(123,000) → 잔량 '트레일 스탑' 청산, 총 R > 1.5
        out = _step(123_000, tmp)
        if any(p["code"] == "TR" for p in out["positions"]):
            fails.append("트레일 터치인데 청산 안 됨")
        closed = out["closed"][0] if out.get("closed") else {}
        r = closed.get("r", 0)
        notes = [e["note"] for e in closed.get("exits", [])]
        if "트레일 스탑" not in notes:
            fails.append(f"청산 사유에 '트레일 스탑' 없음: {notes}")
        if not r > 1.5:
            fails.append(f"트레일 성과 {r}R ≤ 1.5R (2R 캡 대비 이득 없음)")
        print(f"  트레일 터치 → 전량 청산 · 최종 {r:+.2f}R · 사유 {notes}")

    # 6) 회귀: atr0 없는 구 포지션은 2R에서 익절돼야 함
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        legacy = dict(ITEM)
        legacy.pop("atr")                       # ATR 미기록(구 스키마)
        _step(100_000, tmp, legacy)
        _step(105_000, tmp)                     # +1R 절반
        out = _step(110_000, tmp)               # 2R → 구 규칙이면 전량 익절
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
    print("✅ 전부 통과 — 트레일 래칫·무제한 러닝·구버전 호환 모두 정상.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
