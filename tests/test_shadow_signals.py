"""섀도 신호(B1 태그·B2·A ablation) 계약 검증 — 관측 전용, 주문 경로 격리.

배경(외부검토 2026-08-19): B의 저조가 표본 탓인지 설계 탓인지 가르려면
단일 변수 실험이 필요하다. B1 = 기존 B에 200일선 태그만(재구성용),
B2 = 추세 눌림목 별도 계열, A ablation = 게이트 하나에서만 떨어진 후보 기록.
셋 다 **자본 0·주문 0** — 어기면 실험이 아니라 배포다.

  1) B2 신호는 group=shelf_shadow_b2 · shadow=True · orderable=False
  2) 매수루프 _shelf_cands·_now_signals가 섀도 그룹을 절대 소비하지 않음
  3) B1 태그(trend_above_200)가 shelf 신호에 존재(값: True/False/None)
  4) A ablation은 정확히 1개 게이트 탈락만 · 게이트명 화이트리스트
  5) 이력 부족 종목은 A 후보에서 제외되나 수집·결과행은 유지(P1-1)

실행: python -m tests.test_shadow_signals
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                        # noqa: E402
from scanner import gates, screener  # noqa: E402
from bot import kis_buyloop          # noqa: E402


def _frame(bars=600, price=100.0, ma_price=None):
    idx = pd.date_range("2018-01-01", periods=bars, freq="B")
    closes = np.full(bars, price)
    if ma_price is not None:                     # 마지막 220봉 이전을 낮게 → 상승 기울기
        closes[:-220] = ma_price
    return pd.DataFrame({"Open": closes, "High": closes * 1.01,
                         "Low": closes * 0.99, "Close": closes,
                         "Volume": 2e6}, index=idx)


def _result(code="TST", *, bars=600, price=100.0, ma50=None, ma200=None,
            slope=None, checks=None, group_reasons=None, stage=3,
            range_pos=0.30, runup63=0.05, bear=0.0):
    return {
        "code": code, "name": code, "ccy": "USD",
        "sr": {"price": price}, "norm": 70.0, "bars": bars,
        "entry": price, "risk": {"stop": price * 0.95, "target": price * 1.2,
                                 "rr": 2.0},
        "range_pos": range_pos, "turnover": 1e9,
        "transition_stage": stage, "entry_kind": "now", "vetoed": False,
        "ext": {"ma50": ma50, "ma200": ma200, "ma200_slope": slope,
                "runup63": runup63, "ma120_stretch": 0.0},
        "rs": {"rel": 0.0},
        "shelf": {"ok": False, "watch": False, "checks": checks or {},
                  "runup252": 0.1},
        "module_scores": {}, "verdict_label": "", "gauge": 0,
    }


def test_b2_shadow_shape_and_isolation():
    r = _result(ma50=99.0, ma200=90.0, slope=0.05,
                checks={"상단마감": True, "거래량": True})
    frames = {"TST": {"D": _frame()}}
    payload = json.loads(screener._signals_json([r], frames))
    b2 = [s for s in payload["signals"] if s["group"] == "shelf_shadow_b2"]
    assert len(b2) == 1, [s["group"] for s in payload["signals"]]
    row = b2[0]
    assert row["shadow"] is True and row["orderable"] is False
    assert row["stop"] < row["entry"] < row["target"]
    # 매수루프 소비자 격리 — 섀도는 어떤 경로로도 후보가 되지 않는다.
    assert kis_buyloop._shelf_cands(payload["signals"]) == []
    assert kis_buyloop._now_signals(payload["signals"]) == []
    print("[PASS] B2: 형태 계약 + 매수루프 소비 0")


def test_b2_requires_every_condition():
    frames = {"TST": {"D": _frame()}}
    bad = [
        _result(ma50=99.0, ma200=110.0, slope=0.05,      # 200일선 아래
                checks={"상단마감": True, "거래량": True}),
        _result(ma50=99.0, ma200=90.0, slope=-0.01,      # 기울기 하락
                checks={"상단마감": True, "거래량": True}),
        _result(ma50=80.0, ma200=90.0, slope=0.05,       # 50일선 ±5% 밖
                checks={"상단마감": True, "거래량": True}),
        _result(ma50=99.0, ma200=90.0, slope=0.05,       # 반등 미확인
                checks={"상단마감": False, "거래량": True}),
        _result(ma50=None, ma200=90.0, slope=0.05,       # 이력 부족(ma 없음)
                checks={"상단마감": True, "거래량": True}),
    ]
    for r in bad:
        payload = json.loads(screener._signals_json([r], frames))
        assert not [s for s in payload["signals"]
                    if s["group"] == "shelf_shadow_b2"], r["ext"]
    print("[PASS] B2: 조건 하나라도 빠지면 신호 없음(5반례)")


def test_b2_and_b1_reject_nonfinite_observation_inputs():
    frames = {"TST": {"D": _frame()}}
    bad = _result(ma50=float("nan"), ma200=90.0, slope=0.05,
                  checks={"상단마감": True, "거래량": True})
    payload = json.loads(screener._signals_json([bad], frames))
    assert not [s for s in payload["signals"]
                if s["group"] == "shelf_shadow_b2"]
    shelf = _result(ma50=99.0, ma200=float("inf"), slope=0.05)
    shelf["shelf"] = {"ok": True, "entry": 100, "stop": 95,
                      "target": 110, "rr": 2, "checks": {}}
    payload2 = json.loads(screener._signals_json([shelf], frames))
    rows = [s for s in payload2["signals"] if s["group"] == "shelf"]
    assert rows and rows[0]["trend_above_200"] is None
    assert "NaN" not in json.dumps(payload, allow_nan=False)
    print("[PASS] B1/B2 NaN·inf 관측값은 false 신호/비표준 JSON으로 승격 안 됨")


def test_a_ablation_single_gate_only():
    rows = [
        _result("RP", range_pos=0.70),                      # 저점권만 탈락
        _result("RUN", runup63=0.40),                       # 급등만 탈락
        _result("BOTH", range_pos=0.70, runup63=0.40),      # 2개 탈락 → 제외
        _result("PASS"),                                    # 통과 → 제외
    ]
    frames = {r["code"]: {"D": _frame()} for r in rows}
    payload = json.loads(screener._signals_json(rows, frames))
    ab = payload.get("a_ablation") or []
    codes = {e["code"]: e["gate"] for e in ab}
    assert codes.get("RP") == "rp" and codes.get("RUN") == "runup", codes
    assert "BOTH" not in codes and "PASS" not in codes
    assert all(e["gate"] in ("rp", "runup", "consensus") for e in ab)
    print("[PASS] A ablation: 단일 게이트 탈락만 · 게이트명 화이트리스트")


def test_short_history_excluded_from_a_but_kept_in_results():
    short = _result("YOUNG", bars=120)
    reasons = gates.exclusion_reasons(short)
    assert any("이력 부족" in x for x in reasons), reasons
    assert gates.classify(short)["group"] is None
    # bars 키가 없는 구형 행은 판정하지 않는다(오탐 제외 방지).
    legacy = _result("OLD")
    legacy.pop("bars")
    assert not any("이력 부족" in x for x in gates.exclusion_reasons(legacy))
    print("[PASS] 이력 부족 → A 후보 제외 · bars 없는 구형 행은 미판정")


def test_b1_tag_present_on_shelf_rows():
    r = _result(ma50=99.0, ma200=90.0, slope=0.05,
                checks={"터치": True, "회복": True, "상단마감": True,
                        "거래량": True, "신저가아님": True})
    r["shelf"] = {"ok": True, "watch": False, "entry": 100.0, "stop": 95.0,
                  "target": 110.0, "rr": 2.0, "checks": r["shelf"]["checks"] if False else {},
                  "poc": 99.0, "val": 95.0, "vah": 110.0, "overhead": 0.3}
    frames = {"TST": {"D": _frame()}}
    payload = json.loads(screener._signals_json([r], frames))
    shelf_rows = [s for s in payload["signals"] if s["group"] == "shelf"]
    assert shelf_rows and "trend_above_200" in shelf_rows[0]
    assert shelf_rows[0]["trend_above_200"] is True     # 100 > 90
    print("[PASS] B1 태그: shelf 신호에 trend_above_200 기록")


def main():
    test_b2_shadow_shape_and_isolation()
    test_b2_requires_every_condition()
    test_b2_and_b1_reject_nonfinite_observation_inputs()
    test_a_ablation_single_gate_only()
    test_short_history_excluded_from_a_but_kept_in_results()
    test_b1_tag_present_on_shelf_rows()
    print("\n섀도 신호 계약 통과 — 관측 전용·주문 격리·단일변수 기록.")


if __name__ == "__main__":
    main()
