"""패턴 품질(Phase 0) 검증 — 수치 정확성 + '기록 전용' 불변(행동 불개입).

  1) clear_space_R: (저항-진입)/R 정확 계산·최근접 선택·상방 없음=no_overhead
  2) close_location/wick: 강마감·약마감·일자봉 가드
  3) atr_contract: 수축 구간 <1 / 데이터 부족 None
  4) extension_atr 부호·quality 합산
  5) analyze 배선: 결과에 pattern 포함 + 점수/verdict는 pattern과 무관(기록 전용)
  6) 분위수 리포트: 합성 신호로 버킷 통계 정확성

실행: python -m tests.test_pattern_quality
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import pattern_quality as PQ


def _df(rows):
    """rows: (O,H,L,C,V) 리스트 → OHLCV DataFrame."""
    idx = pd.date_range("2025-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["Open", "High", "Low", "Close", "Volume"])


def _flat(n, px=100.0, amp=1.0):
    """변동폭 amp의 평평한 구간."""
    return [(px, px + amp, px - amp, px, 1000)] * n


def test_clear_space():
    d = _df(_flat(80))
    levels = {"strong": [{"price": 110.0}, {"price": 130.0}]}
    sr = {"box_high": 120.0, "poc": 90.0}          # POC는 아래라 제외
    p = PQ.compute(d, entry=100.0, stop=95.0, sr=sr, levels=levels)
    assert p["nearest_resistance"] == 110.0        # 최근접(110<120<130)
    assert abs(p["clear_space_R"] - 2.0) < 1e-9    # (110-100)/5
    assert p["resistance_type"] == "swing_cluster"
    # 상방 저항 없음 → no_overhead
    p2 = PQ.compute(d, entry=200.0, stop=195.0, sr=sr, levels=levels)
    assert p2["no_overhead"] and p2["clear_space_R"] is None
    # 손절폭 0/음수 → 계산 안 함
    p3 = PQ.compute(d, entry=100.0, stop=100.0, sr=sr, levels=levels)
    assert p3["clear_space_R"] is None and not p3["no_overhead"]
    print("[PASS] clear_space_R: 최근접·R환산·no_overhead·risk 가드")


def test_candle_quality():
    rows = _flat(30) + [(100, 110, 100, 108, 1000)]   # 강마감(위치 0.8)
    p = PQ.compute(_df(rows), entry=100, stop=95)
    assert abs(p["close_location"] - 0.8) < 1e-9
    assert abs(p["upper_wick_ratio"] - 0.2) < 1e-9
    assert p["lower_wick_ratio"] == 0.0
    rows2 = _flat(30) + [(108, 110, 100, 101, 1000)]  # 긴 윗꼬리 약마감
    p2 = PQ.compute(_df(rows2), entry=100, stop=95)
    assert p2["close_location"] < 0.2 and p2["upper_wick_ratio"] > 0.15
    rows3 = _flat(30) + [(100, 100, 100, 100, 0)]     # 일자봉 가드
    p3 = PQ.compute(_df(rows3), entry=100, stop=95)
    assert p3["close_location"] == 0.5
    print("[PASS] close_location/wick: 강·약마감·일자봉 가드")


def test_atr_contract_and_extension():
    rows = []
    for _ in range(70):                                # 넓은 변동(±5)
        rows.append((100, 105, 95, 100, 1000))
    for _ in range(20):                                # 수축(±1)
        rows.append((100, 101, 99, 100, 1000))
    p = PQ.compute(_df(rows), entry=100, stop=95)
    assert p["atr_contract"] is not None and p["atr_contract"] < 1.0
    assert abs(p["extension_atr"]) < 0.5               # MA20≈100·종가 100
    short = PQ.compute(_df(_flat(30)), entry=100, stop=95)
    assert short["atr60"] is None and short["atr_contract"] is None
    print("[PASS] atr_contract(수축<1)·extension·데이터부족 None")


def test_quality_and_badge():
    # 3/3 구성: 넓은 변동→수축(contract<0.8) + 강마감(0.9) + 6R 공간
    rows = ([(100, 105, 95, 100, 1000)] * 70
            + [(100, 101, 99, 100, 1000)] * 19
            + [(100, 101, 99, 100.8, 1000)])
    p = PQ.compute(_df(rows), entry=100, stop=95,
                   levels={"strong": [{"price": 130.0}]})
    assert p["atr_contract"] < 0.8 and p["close_location"] >= 0.6
    assert p["quality"] == 3
    # 평탄(수축비=1)·중립마감(0.5)이면 공간 1점만 — 합산이 과대평가 안 함
    p1 = PQ.compute(_df(_flat(80, amp=0.5)), entry=100, stop=95,
                    levels={"strong": [{"price": 130.0}]})
    assert p1["quality"] == 1
    assert "기록용" in PQ.badge(p)
    assert PQ.badge(None) == ""
    print("[PASS] quality 합산(3/3·1/3)·badge(기록용 명시)")


def test_analyze_wiring_record_only():
    """analyze 결과에 pattern이 실리고, 점수/판정은 pattern과 무관해야 한다."""
    from tests.sample_data import make
    from scanner import data as sdata
    from scanner.analyze import analyze
    from unittest import mock
    frames = sdata.frames_from_daily(make("box"))
    meta = {"code": "TEST", "name": "테스트", "ccy": "USD"}
    r1 = analyze(frames, meta)
    assert "pattern" in r1 and "quality" in r1["pattern"]
    # pattern 계산을 최악값으로 바꿔도 verdict/norm 불변(기록 전용 증명)
    worst = {k: None for k in r1["pattern"]}
    worst.update({"quality": 0, "no_overhead": False, "resistance_type": ""})
    with mock.patch("scanner.analyze.pq.compute", return_value=worst):
        r2 = analyze(frames, meta)
    assert r2["norm"] == r1["norm"] and r2["verdict_label"] == r1["verdict_label"]
    assert r2["entry"] == r1["entry"] and r2["risk"]["stop"] == r1["risk"]["stop"]
    print("[PASS] analyze 배선: pattern 기록 + 점수/판정/타점 완전 불변")


def test_quantile_report():
    from scanner.backtest import Signal, pattern_quantile_report
    sigs = []
    # cs_r 낮을수록 나쁜 성과가 되도록 합성(단조) — 리포트가 그대로 드러내는지
    for i in range(40):
        cs = 0.5 + i * 0.1                      # 0.5 ~ 4.4
        r = -0.5 if cs < 1.5 else (0.5 if cs < 2.5 else 1.5)
        sigs.append(Signal(code="T", date=None, kind="transition", r=r,
                           reason="", vol_mult=1.5, rsi=50, dist_pct=1.0,
                           cs_r=cs, mfe=max(r, 0.0) + 0.2, mae=min(r, 0.0)))
    rows = pattern_quantile_report(sigs, "cs_r")
    assert len(rows) == 4 and rows[0]["n"] == 10
    assert rows[0]["avg_r"] < rows[-1]["avg_r"]          # Q1 < Q4 (단조 노출)
    assert rows[-1]["hit1R"] > rows[0]["hit1R"]
    assert pattern_quantile_report(sigs, "atr_ct") == []  # 전부 None → 표본부족
    print("[PASS] 분위수 리포트: 버킷·단조·None 제외")


def main():
    test_clear_space()
    test_candle_quality()
    test_atr_contract_and_extension()
    test_quality_and_badge()
    test_analyze_wiring_record_only()
    test_quantile_report()
    print("\n모든 패턴 품질 테스트 통과 — Phase 0(기록 전용) 무결.")


if __name__ == "__main__":
    main()
