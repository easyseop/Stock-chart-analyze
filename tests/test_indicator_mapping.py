"""전략 지표가 계산에서 매수 게이트까지 실제로 연결되는지 검증."""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from scanner import analyze, gates, scoring


EXPECTED = {
    "trend", "rs", "newhigh", "market",
    "volume", "sr", "rsi", "trendline",
}


def _frames() -> tuple[dict, pd.DataFrame]:
    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    close = np.linspace(100, 130, 300) + np.sin(np.arange(300) / 8) * 3
    daily = pd.DataFrame({
        "Open": close - 0.5, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": np.linspace(1_000_000, 2_000_000, 300),
    }, index=idx)

    def bars(rule: str) -> pd.DataFrame:
        aliases = ("ME", "M") if rule == "M" else (rule,)
        for alias in aliases:
            try:
                return daily.resample(alias).agg({
                    "Open": "first", "High": "max", "Low": "min",
                    "Close": "last", "Volume": "sum",
                }).dropna()
            except ValueError:
                continue
        raise AssertionError(f"지원되지 않는 resample rule: {rule}")

    return {"D": daily, "W": bars("W-FRI"), "M": bars("M")}, daily


def test_analysis_maps_all_directional_and_risk_indicators():
    frames, bench = _frames()
    result = analyze.analyze(
        frames, {"code": "TEST", "name": "Test", "ccy": "USD"}, bench=bench)
    assert set(result["module_scores"]) == EXPECTED
    assert set(result["weights"]) == EXPECTED
    assert result["regime"]["adx"] > 0                 # ADX → 국면/가중치
    assert result["risk"]["atr"] > 0                   # ATR → 손절·수량
    assert "atr_contract" in result["pattern"]         # 검증 전 품질지표도 기록


def test_every_scored_indicator_influences_at_least_one_regime():
    assert set(config.SCORE_MODULES) == EXPECTED
    zero = {name: 0 for name in EXPECTED}
    for name in EXPECTED:
        assert set(config.REGIME_WEIGHTS[name]) == {"추세장", "횡보장", "전환"}
        assert any(config.REGIME_WEIGHTS[name][regime] > 0
                   for regime in ("추세장", "횡보장", "전환"))
        assert any(
            scoring.normalize({**zero, name: 2}, regime)["score"] >
            scoring.normalize(zero, regime)["score"]
            for regime in ("추세장", "횡보장", "전환"))


def test_extreme_bearish_consensus_is_an_active_entry_veto():
    scores = dict.fromkeys(EXPECTED, -1)
    scores["volume"] = 1
    scores["trendline"] = 1
    assert gates.consensus_bear({"module_scores": scores}) == 0.75
    assert config.CONSENSUS_VETO_ACTIVE is True
    assert config.CONSENSUS_BEAR_VETO == 0.75


if __name__ == "__main__":
    test_analysis_maps_all_directional_and_risk_indicators()
    test_every_scored_indicator_influences_at_least_one_regime()
    test_extreme_bearish_consensus_is_an_active_entry_veto()
    print("[PASS] 8개 방향성 지표 + ADX/ATR/패턴 품질 매핑")
