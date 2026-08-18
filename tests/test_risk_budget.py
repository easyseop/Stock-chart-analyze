"""계좌 단위 총 open risk 상한 검증 — 합산 계획손실 제한 + fail-closed.

배경(외부 검토 2026-08-19 P0): 거래당 1% 상한만 있고 합산 상한이 없어
1% 포지션 20개 = 동시 손절 20% 노출이 규칙 위반 없이 가능했다.

  1) 합산 계산: 래칫(stop>=entry)=0 · KRW/USD 환산 · qty<=0 제외
  2) 상한 초과 → 차단, 미만 → 허용 (경계 = 차단)
  3) 계량 불가(무보호·손상·ccy 유실) → 차단(fail-closed)
  4) 원장 읽기 실패·시드 불명 → 차단
  5) cap env: 엄격(0.05)은 존중, >1은 1로 클램프(완화를 기본값으로 안 되돌림),
     쓰레기는 기본값
  6) buyloop 배선: 차단 시 후보 전원 gate=portfolio_risk, 주문 0

실행: python -m tests.test_risk_budget
"""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import risk_budget as R   # noqa: E402

FX = 1000.0    # 계산 검증이 쉬운 환율


def _pos(entry, stop, qty, ccy="USD"):
    return {"entry": entry, "stop": stop, "qty": qty, "ccy": ccy}


def test_open_risk_math():
    positions = {
        "AAPL": _pos(100.0, 90.0, 10),          # (100-90)*10*1000 = 100,000
        "TSLA": _pos(50.0, 60.0, 10),           # 래칫: stop>entry → 0
        "005930": _pos(70000.0, 65000.0, 2, "KRW"),   # 10,000 (환율 없음)
        "CLOSED": _pos(10.0, 9.0, 0),           # qty 0 → 제외
    }
    out = R.open_risk(positions, FX)
    assert out["defined_krw"] == 110000.0, out
    assert out["unknown"] == []
    assert out["rows"][0]["code"] == "AAPL"     # 위험 큰 순 정렬
    print("[PASS] 합산: 래칫 0 · KRW/USD 환산 · qty<=0 제외")


def test_unquantifiable_rows_flagged():
    positions = {
        "NOSTOP": _pos(100.0, 0.0, 10),         # stop<=0 = 무보호
        "NOENTRY": _pos(None, 90.0, 10),
        "BOOLQTY": _pos(100.0, 90.0, True),
        "BADCCY": {"entry": 100.0, "stop": 90.0, "qty": 5, "ccy": None},
        "NAN": _pos(float("nan"), 90.0, 5),
        "OK": _pos(100.0, 95.0, 2),
    }
    out = R.open_risk(positions, FX)
    assert set(out["unknown"]) == {"NOSTOP", "NOENTRY", "BOOLQTY",
                                   "BADCCY", "NAN"}, out["unknown"]
    assert out["defined_krw"] == 10000.0
    print("[PASS] 무보호·손상·ccy 유실 → 계량 불가로 분류")


def _gate(positions, seed=1_000_000.0, env=None):
    with mock.patch("bot.kis_positions.load", lambda: positions), \
            mock.patch("bot.envelope.operating_total_krw", lambda: seed), \
            mock.patch.dict(os.environ, env or {}, clear=False):
        return R.gate(FX)


def test_gate_cap_boundary():
    # 시드 100만 · cap 10% → 위험 10만이 정확히 경계 = 차단
    at_cap = {"A": _pos(100.0, 90.0, 10)}          # 100,000 = 10.0%
    ok, why, snap = _gate(at_cap)
    assert ok is False and "상한" in why, (ok, why)
    under = {"A": _pos(100.0, 91.0, 10)}           # 90,000 = 9.0%
    ok2, why2, _ = _gate(under)
    assert ok2 is True, why2
    print("[PASS] 상한 경계 = 차단 · 미만 = 허용")


def test_gate_fail_closed_paths():
    ok, why, _ = _gate({"X": _pos(100.0, 0.0, 5)})
    assert ok is False and "계량 불가" in why
    def boom():
        raise OSError("disk")
    with mock.patch("bot.kis_positions.load", boom):
        ok2, why2, _ = R.gate(FX)
    assert ok2 is False and "원장 읽기 실패" in why2
    # 시드 0 + 위험 0 → 허용(비율 0 — 시드 검증은 execute_entry 사이징 소관).
    ok3, _, _ = _gate({}, seed=0.0)
    assert ok3 is True
    # 시드 0 + 위험 존재 → 분모 없이 비율 계산 불가 = 차단.
    ok4, why4, _ = _gate({"A": _pos(100.0, 90.0, 10)}, seed=0.0)
    assert ok4 is False and "시드 불명" in why4
    ok5, _, _ = _gate({})                          # 정상 시드 + 빈 원장 → 허용
    assert ok5 is True
    print("[PASS] 무보호·원장실패 차단 · 시드0은 위험 유무로 갈림")


def test_cap_env_clamps():
    strict = {"A": _pos(100.0, 94.0, 10)}          # 60,000 = 6%
    ok, why, _ = _gate(strict, env={"MAX_OPEN_RISK_FRACTION": "0.05"})
    assert ok is False, why                        # 엄격(5%) 존중 → 6%는 차단
    ok2, _, _ = _gate(strict, env={"MAX_OPEN_RISK_FRACTION": "1.7"})
    assert ok2 is True                             # >1은 1로 클램프(완화 존중)
    with mock.patch.dict(os.environ, {"MAX_OPEN_RISK_FRACTION": "abc"}):
        assert R.max_fraction() == R.DEFAULT_MAX_OPEN_RISK_FRACTION
    with mock.patch.dict(os.environ, {"MAX_OPEN_RISK_FRACTION": "-1"}):
        assert R.max_fraction() == R.DEFAULT_MAX_OPEN_RISK_FRACTION
    print("[PASS] cap env: 엄격 존중 · >1은 1 클램프 · 쓰레기는 기본값")


def test_buyloop_blocks_all_candidates_when_over_cap():
    from bot import kis_buyloop
    sig = [{"code": "NVDA", "group": "now", "fresh": True, "entry": 100.0,
            "stop": 93.0, "tactic": "full", "ccy": "USD", "id": "t1",
            "name": "NVIDIA", "stage": 3, "norm": 80.0}]
    with mock.patch.object(kis_buyloop.risk_budget, "gate",
                           lambda fx: (False, "총 open risk 12.0% ≥ 상한 10%", {})), \
            mock.patch.object(kis_buyloop, "_broker_state",
                              lambda fx: ({}, {}, [], set(), {})), \
            mock.patch.object(kis_buyloop.settings, "market_open",
                              lambda ccy: True), \
            mock.patch.object(kis_buyloop.kis_buy, "execute_entry") as buy:
        out = kis_buyloop.run_once(sig, fx=FX)
    rows = [r for r in out if r.get("code") == "NVDA"]
    assert rows and rows[0]["gate"] == "portfolio_risk", out
    assert buy.call_count == 0                     # 주문 함수 자체가 안 불림
    print("[PASS] buyloop: 상한 초과 → 전 후보 portfolio_risk · 주문 0")


def main():
    test_open_risk_math()
    test_unquantifiable_rows_flagged()
    test_gate_cap_boundary()
    test_gate_fail_closed_paths()
    test_cap_env_clamps()
    test_buyloop_blocks_all_candidates_when_over_cap()
    print("\n총 open risk 상한 검증 통과 — 합산 제한·fail-closed·buyloop 차단.")


if __name__ == "__main__":
    main()
