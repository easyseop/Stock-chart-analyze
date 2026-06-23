"""점수 종합 — 국면 가중치 적용 후 -100~+100 정규화 → 판정."""
from __future__ import annotations

import config


def normalize(module_scores: dict[str, int], regime_flag: str) -> dict:
    """module_scores: {'trend':s,'rsi':s,'sr':s,'volume':s} (각 -2..+2).

    정규화점수 = 100 × Σ(점수×가중치) / Σ(2×가중치)
    """
    num = 0.0
    den = 0.0
    weights = {}
    for m in config.SCORE_MODULES:
        w = config.REGIME_WEIGHTS[m][regime_flag]
        weights[m] = w
        num += module_scores.get(m, 0) * w
        den += 2 * w
    score = 100 * num / den if den > 0 else 0.0
    return {"score": round(score, 1), "weights": weights}


def verdict(norm_score: float) -> tuple[str, str]:
    """정규화 점수 → (판정, 게이지).

    대칭 경계: |점수|≥50 적극, ≥20 관심, 그 사이 관망.
    +50 → 적극 매수, −50 → 적극 매도 (양끝 포함).
    """
    x = norm_score
    s, w = config.VERDICT_STRONG, config.VERDICT_WEAK
    if x >= s:
        return "적극 매수", "🟢 강세"
    if x >= w:
        return "매수 관심", "🟢 관심"
    if x > -w:
        return "관망", "⚪ 중립"
    if x > -s:
        return "매도 관심", "🔴 주의"
    return "적극 매도/회피", "🔴 공포"
