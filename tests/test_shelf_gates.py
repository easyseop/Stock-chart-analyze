"""B(매물대) 진입 게이트 검증 — 추락 배제·이력 하한·섀도 태그.

실측(2026-08-18) 라이브 신호 점검에서 나온 두 구멍:
  · 범위 게이트에 **하한이 없어** WLFC(범위 위치 0.02 · 6개월 −72% ·
    평균 보유자 −64%)가 "저점권"으로 통과했다. 지지가 아니라 자유낙하.
  · 상장 기간 하한이 없어 RHLD(상장 1.5년 · 1년 +126% 뒤 고점 대비 −44%)가
    실제 매수 신호로 나왔다. `min(len(d), 252)`로 짧은 이력이 조용히
    "52주 범위"가 된다.

  1) 범위 하한 미만 → 추락으로 판정, 신호 없음
  2) 이력 부족(<2년) → 신호 없음
  3) 정상 구간은 기존대로 통과
  4) 섀도 태그(history_bars·holder_pnl·runup252)는 **판정에 쓰지 않는다**

실행: python -m tests.test_shelf_gates
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                       # noqa: E402
from scanner import analyze         # noqa: E402


def _frame(bars: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2018-01-01", periods=bars, freq="B")
    return pd.DataFrame({"Open": price, "High": price * 1.01,
                         "Low": price * 0.99, "Close": price,
                         "Volume": 1_000_000.0}, index=idx)


def _supply(price=100.0, val=95.0, vah=110.0, poc=99.0, overhead=0.3, pnl=-0.05):
    return {"price": price, "long": {"val": val, "vah": vah},
            "long_poc": poc, "pnl": {"overhead": overhead, "pnl": pnl}}


_VOL = {"mult": 2.0}


def test_falling_knife_rejected_by_range_floor():
    d = _frame(800)
    out = analyze._shelf_signal(d, _supply(), _VOL, range_pos=0.02)
    assert out["ok"] is False and "추락" in out["reason"], out
    print(f"[PASS] 범위 {0.02:.2f} → '{out['reason']}' (WLFC형 배제)")


def test_short_history_rejected():
    d = _frame(300)                                  # ≈1.2년 < 504봉
    out = analyze._shelf_signal(d, _supply(), _VOL, range_pos=0.35)
    assert out["ok"] is False and "이력 부족" in out["reason"], out
    assert out["history_bars"] == 300
    print(f"[PASS] 300봉 → '{out['reason']}' (RHLD형 배제)")


def test_normal_case_still_passes_gates():
    """게이트 두 개가 정상 구간을 막지 않는다(과잉 차단 방지)."""
    d = _frame(800)
    out = analyze._shelf_signal(d, _supply(), _VOL, range_pos=0.35)
    for banned in ("추락", "이력 부족", "저점권"):
        assert banned not in str(out.get("reason", "")), out
    print("[PASS] 정상 범위·충분한 이력은 두 게이트를 통과")


def test_shadow_tags_present_but_not_gating():
    """섀도 태그는 기록만 — 값이 극단이어도 판정을 바꾸지 않는다."""
    d = _frame(800)
    hot = analyze._shelf_signal(d, _supply(pnl=0.80), _VOL, range_pos=0.35)
    cold = analyze._shelf_signal(d, _supply(pnl=-0.40), _VOL, range_pos=0.35)
    assert hot["profile_pnl_proxy"] == 0.80 and cold["profile_pnl_proxy"] == -0.40
    # 이름 계약: 실보유자 손익처럼 읽히는 옛 키는 더 이상 없다(외부검토 P2).
    assert "holder_pnl" not in hot
    assert hot["profile_method"] == "ohlcv-uniform-approx"
    assert hot.get("reason") == cold.get("reason"), (hot, cold)   # 판정 동일
    assert "runup252" in hot and "history_bars" in hot
    print("[PASS] 섀도 태그 기록 · 판정에는 미반영")


def test_thresholds_are_configurable_and_sane():
    assert 0 < config.SHELF_MIN_RANGE_POS < config.SHELF_LOW_ZONE
    assert config.SHELF_MIN_HISTORY_BARS >= 252      # 최소 1년 이상
    print(f"[PASS] 임계값 정합 — 범위 {config.SHELF_MIN_RANGE_POS}"
          f"~{config.SHELF_LOW_ZONE} · 이력 {config.SHELF_MIN_HISTORY_BARS}봉")


def main():
    test_falling_knife_rejected_by_range_floor()
    test_short_history_rejected()
    test_normal_case_still_passes_gates()
    test_shadow_tags_present_but_not_gating()
    test_thresholds_are_configurable_and_sane()
    print("\nB 게이트 검증 통과 — 추락·짧은 이력 배제, 섀도 태그는 관측 전용.")


if __name__ == "__main__":
    main()
