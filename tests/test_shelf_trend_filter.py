"""B1(200일선 필터) 회귀 고정 — 슬리브 B 진입 게이트.

근거(2026-08-26 실측): 진입일 200일선 아래에서 시작한 B 청산 9건이 **전부**
손실(평균 −6.00%)이고 MFE 중앙값이 0.26R이었다 — 오르다 반납한 게 아니라
애초에 오르지 않았다. 200일선 위 5건은 4승(평균 +3.52% · MFE 중앙 1.91R).
Fisher 양측 p=0.0050. 같은 8월·같은 200일선 아래에서 A는 13건 중 9승이라
시장 탓이 아니라 B 신호의 결함이다(p=0.0047).
"""
import json

import config
import pytest

from scanner import gates, screener


def _row(price=101.0, ma200=95.0, **over):
    r = {"code": "TST", "name": "TST", "ccy": "USD", "turnover": 5e7,
         "shelf": {"ok": True, "entry": 101, "stop": 96, "target": 110,
                   "reason": "매물대 지지 반등"},
         "rs": {"rel": 0.0}, "sr": {"price": price},
         "ext": {"ma120_stretch": 0.0, "ma200": ma200}}
    for key, value in over.items():
        if key in ("ext", "sr", "rs", "shelf"):
            r[key] = {**r[key], **value}
        else:
            r[key] = value
    return r


def test_above_200ma_passes():
    assert gates.classify_shelf(_row(price=101.0, ma200=95.0))["group"] == "shelf"


def test_below_200ma_rejected():
    out = gates.classify_shelf(_row(price=94.0, ma200=95.0))
    assert out["group"] is None
    assert out["reasons"] == ["200일선 아래(B1)"]


def test_boundary_is_inclusive():
    """정확히 200일선 위는 통과 — 경계에서 후보를 잃지 않는다."""
    assert gates.classify_shelf(_row(price=95.0, ma200=95.0))["group"] == "shelf"


@pytest.mark.parametrize("ma200", [
    None, float("nan"), float("inf"), float("-inf"), 0, -1, "", "abc",
])
def test_unmeasurable_trend_fails_closed(ma200):
    """판정불가는 거절한다 — 매수 게이트에서 모르는 것을 사지 않는다."""
    out = gates.classify_shelf(_row(ma200=ma200))
    assert out["group"] is None
    assert out["reasons"] == ["200일선 판정불가(이력부족)"]


def test_bool_is_not_a_number():
    """True는 float(True)==1.0이라 조용히 '가격 1.0'으로 둔갑할 수 있다."""
    assert gates.finite_number(True) is None
    assert gates.classify_shelf(_row(ma200=True))["group"] is None
    assert gates.classify_shelf(_row(price=True))["group"] is None


def test_missing_price_fails_closed():
    row = _row()
    row["sr"] = {}
    assert gates.classify_shelf(row)["group"] is None


def test_hard_exclusions_come_first():
    """잡주가 '200일선 아래'로 보고되면 제외 사유 통계가 오염된다."""
    junk = _row(price=94.0, ma200=95.0, turnover=1.0)     # 저유동 + 추세 미달
    out = gates.classify_shelf(junk)
    assert out["group"] is None
    assert "저유동성(잡주)" in out["reasons"]
    assert "200일선 아래(B1)" not in out["reasons"]


def test_switch_off_skips_gate_but_keeps_observation(monkeypatch):
    """스위치를 꺼도 관측(B0 섀도 모집단)은 계속된다 — 껐다 켜며 비교하려면."""
    below = _row(price=94.0, ma200=95.0)
    monkeypatch.setattr(config, "SHELF_REQUIRE_TREND_200", False)
    assert gates.classify_shelf(below)["group"] == "shelf"
    assert gates.shelf_trend_rejection(below) == "200일선 아래(B1)"
    monkeypatch.setattr(config, "SHELF_REQUIRE_TREND_200", True)
    assert gates.classify_shelf(below)["group"] is None


def test_rejection_population_excludes_other_failures():
    """다른 사유로 이미 탈락한 후보는 B0 섀도에 넣지 않는다.

    섞으면 섀도가 '필터의 효과'가 아니라 잡동사니를 재게 된다.
    """
    assert gates.shelf_trend_rejection(_row(turnover=1.0)) is None       # 저유동
    assert gates.shelf_trend_rejection(_row(rs={"rel": 0.5})) is None    # 이미 폭등
    assert gates.shelf_trend_rejection(
        _row(shelf={"ok": False, "reason": "반등 미확인"})) is None
    assert gates.shelf_trend_rejection(_row(price=101.0)) is None        # 통과분
    assert gates.shelf_trend_rejection(
        _row(price=94.0, ma200=95.0)) == "200일선 아래(B1)"


def _payload(rows):
    frames = {r["code"]: {"D": None} for r in rows}
    return json.loads(screener._signals_json(rows, frames))


def test_rejected_candidate_becomes_b0_shadow():
    sigs = _payload([_row(price=94.0, ma200=95.0)])["signals"]
    assert not [s for s in sigs if s["group"] == "shelf"]
    b0 = [s for s in sigs if s["group"] == "shelf_shadow_b0"]
    assert len(b0) == 1
    assert b0[0]["b0_reject_reason"] == "200일선 아래(B1)"
    assert b0[0]["trend_above_200"] is False
    assert b0[0]["shadow"] is True and b0[0]["orderable"] is False
    # 진입·손절·목표는 필터 도입 **전과 동일** — 그래야 비교가 성립한다.
    assert (b0[0]["entry"], b0[0]["stop"], b0[0]["target"]) == (101.0, 96.0, 110.0)


def test_b0_shadow_is_structurally_unorderable():
    """매수루프는 group=='shelf' 완전일치만 받는다 — 섀도는 주문 경로 밖."""
    from bot import kis_buyloop
    payload = _payload([_row(price=94.0, ma200=95.0)])
    assert kis_buyloop._shelf_cands(payload["signals"]) == []
    assert kis_buyloop._now_signals(payload["signals"]) == []


def test_passing_candidate_emits_no_b0_shadow():
    sigs = _payload([_row(price=101.0, ma200=95.0)])["signals"]
    assert [s for s in sigs if s["group"] == "shelf"]
    assert not [s for s in sigs if s["group"] == "shelf_shadow_b0"]


def test_fastlane_path_without_frames_still_gates():
    """fastlane·oracle_brain은 `_signals_json(results)`를 frames 없이 부른다.

    그 경로에서도 게이트가 살아 있어야 한다. B2 섀도처럼 frames가 필요한
    블록과 달리 B1 게이트와 B0 섀도는 frames에 의존하지 않는다.
    """
    sigs = json.loads(screener._signals_json([_row(price=94.0, ma200=95.0)]))["signals"]
    assert not [s for s in sigs if s["group"] == "shelf"]
    assert [s for s in sigs if s["group"] == "shelf_shadow_b0"]
    sigs_ok = json.loads(screener._signals_json([_row(price=101.0)]))["signals"]
    assert [s for s in sigs_ok if s["group"] == "shelf"]


def test_analyze_supplies_ext_ma200():
    """게이트의 입력원이 끊기면 fail-closed가 **정상 후보를 전멸**시킨다.

    `analyze`가 `ext.ma200`을 채우는 계약을 여기서 고정한다. 200봉 미만이면
    None이고, 그것이 판정불가 → 거절로 이어지는 것은 의도된 동작이다.
    """
    import pandas as pd
    from scanner import data
    from scanner.analyze import analyze as _analyze

    def _daily(n):
        # 완전 선형 램프는 지표(라운드넘버·레벨)에서 NaN을 만든다. 결정적이되
        #   변동이 있는 계열을 쓴다 — 난수를 쓰면 테스트가 흔들린다.
        idx = pd.date_range("2023-01-02", periods=n, freq="B")
        close = [100.0]
        for i in range(1, n):
            close.append(close[-1] * (1 + ((i * 7919) % 13 - 6) / 500))
        s = pd.Series(close, index=idx)
        return pd.DataFrame(
            {"Open": s * 0.995, "High": s * 1.01, "Low": s * 0.99, "Close": s,
             "Volume": [1e6 + ((i * 104729) % 50000) for i in range(n)]},
            index=idx)

    meta = {"code": "TST", "name": "TST", "ccy": "USD"}
    long_r = _analyze(data.frames_from_daily(_daily(260)), meta)
    short_r = _analyze(data.frames_from_daily(_daily(120)), meta)
    assert gates.finite_number((long_r.get("ext") or {}).get("ma200"),
                               positive=True) is not None
    assert (short_r.get("ext") or {}).get("ma200") is None
