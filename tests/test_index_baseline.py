"""지수 기준선 검증 — 오염된 벤치마크가 통계 분모로 들어가는 것을 막는다.

실측(2026-08-21): 야후 meta의 `regularMarketPreviousClose`가 코스피·코스닥·
나스닥 전부 None이라 폴백 `chartPreviousClose`가 상시 경로였고, 그 값은 직전
*거래일* 종가를 보장하지 않는다(같은 시각 코스닥이 08-20 종가 840.89 대신
08-19의 824.46 반환). 기준선은 세션 시작 때 한 번 고정되므로 아침에 하루 밀린
값을 잡으면 그날이 통째로 오염된다 — 코스피 실제 +0.88%가 +7.12%로 발행됐고,
`capture_stats()`가 그 값을 분모로 써서 누적 통계까지 끌고 갔다.

  1) 날짜가 붙은 일봉에서 '직전 거래일' 종가를 골라 쓴다
  2) meta의 낡은 chartPreviousClose가 있어도 무시한다(그게 이번 사고의 원인)
  3) 봉이 부족·날짜 역전이면 None — 아무 값이나 기준선으로 쓰지 않는다
  4) 장중 실시간가는 쓰되, 마지막 봉과 크게 어긋나면 봉을 믿는다
  5) 비상식적 등락률은 숫자로 확정하지 않고 미확정(None)
  6) 이미 고정된 기준선이 검증값과 어긋나면 세션 도중에도 정정한다
  7) 소급 정정: 전일종가 기준 행만·계좌 수익률 불변·멱등

실행: python -m tests.test_index_baseline
"""
from __future__ import annotations

import io
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha                                    # noqa: E402
from scripts import alpha_repair_index as repair         # noqa: E402

DAY = 86400
BASE = 1755000000                       # 임의 고정 기준(테스트는 시계 비의존)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _chart(closes, *, gmtoffset=32400, live=None, extra_meta=None):
    """야후 v8 응답 모양. closes는 하루 간격 종가."""
    meta = {"gmtoffset": gmtoffset, "exchangeTimezoneName": "Asia/Seoul"}
    if live is not None:
        meta["regularMarketPrice"] = live
    meta.update(extra_meta or {})
    stamps = [BASE + i * DAY for i in range(len(closes))]
    return {"chart": {"result": [{
        "meta": meta, "timestamp": stamps,
        "indicators": {"quote": [{"close": list(closes)}]}}]}}


def _patch(payload):
    return mock.patch("urllib.request.urlopen",
                      lambda *a, **k: _Resp(json.dumps(payload).encode()))


def test_previous_close_comes_from_dated_bars():
    with _patch(_chart([6869.83, 6471.17, 6852.58, 6912.95], live=6912.95)):
        q = alpha._yahoo_quote("^KS11")
    assert q["previous_close"] == 6852.58, q          # 직전 거래일
    assert q["current"] == 6912.95, q
    assert q["previous_close_date"] < q["session_date"], q
    pct = (q["current"] / q["previous_close"] - 1) * 100
    assert abs(pct - 0.881) < 0.01, pct
    print(f"[PASS] 일봉 기준선 — 직전 거래일 {q['previous_close_date']} "
          f"→ {pct:+.2f}%")


def test_stale_chart_previous_close_is_ignored():
    """이번 사고의 핵심 반례 — meta가 하루 밀린 값을 줘도 쓰지 않는다."""
    payload = _chart([6869.83, 6471.17, 6852.58, 6912.95], live=6912.95,
                     extra_meta={"regularMarketPreviousClose": None,
                                 "chartPreviousClose": 6471.17})
    with _patch(payload):
        q = alpha._yahoo_quote("^KS11")
    assert q["previous_close"] == 6852.58, q
    bad = (q["current"] / 6471.17 - 1) * 100
    assert abs(bad - 6.83) < 0.05, bad               # 옛 코드였다면 이 값
    print(f"[PASS] 낡은 chartPreviousClose 무시 — 오염값 {bad:+.2f}% 회피")


def test_insufficient_or_broken_bars_yield_none():
    with _patch(_chart([6912.95])):                  # 봉 1개 = 기준선 없음
        assert alpha._yahoo_quote("^KS11") is None
    with _patch(_chart([])):
        assert alpha._yahoo_quote("^KS11") is None
    with _patch({"chart": {"result": [{}]}}):        # 형식 파손
        assert alpha._yahoo_quote("^KS11") is None
    with _patch(_chart([None, None, 100.0])):        # 결측 봉만 있고 기준 부족
        assert alpha._yahoo_quote("^KS11") is None
    print("[PASS] 봉 부족·파손 → None (아무 값이나 기준선으로 쓰지 않음)")


def test_live_price_used_but_only_when_consistent():
    with _patch(_chart([100.0, 110.0], live=112.0)):  # 장중 — 봉보다 최신
        assert alpha._yahoo_quote("X")["current"] == 112.0
    with _patch(_chart([100.0, 110.0], live=1.0)):    # 다른 세션·오염값
        assert alpha._yahoo_quote("X")["current"] == 110.0
    with _patch(_chart([100.0, 110.0], live="nope")):
        assert alpha._yahoo_quote("X")["current"] == 110.0
    print("[PASS] 실시간가는 봉과 정합할 때만 채택")


def test_absurd_index_move_is_not_published_as_a_number():
    assert alpha._sane_idx_pct(0.88, name="코스피") == 0.88
    assert alpha._sane_idx_pct(-14.9, name="코스피") == -14.9
    assert alpha._sane_idx_pct(15.01, name="코스피") is None
    assert alpha._sane_idx_pct(-99.0, name="코스피") is None
    assert alpha._sane_idx_pct(float("nan"), name="코스피") is None
    assert alpha._sane_idx_pct(float("inf"), name="코스피") is None
    assert alpha._sane_idx_pct(None) is None
    assert alpha._sane_idx_pct("x") is None
    print(f"[PASS] ±{alpha.IDX_DAILY_SANE_PCT}% 초과 → 미확정(숫자 확정 금지)")


def test_locked_baseline_self_corrects_mid_session():
    """세션 도중이라도 검증된 전일종가와 어긋난 기준선은 정정한다."""
    st = {"day": {"KR": {
        "date": "2026-08-21", "basis": "previous_close",
        "pl0": 0.0, "a_pl0": 0.0, "b_pl0": 0.0,
        "idx0": {"코스피": 6471.17},          # ← 하루 밀린 오염 기준선
        "series": [], "series_v2": [], "holding_start_codes": []}}}
    agg = {"KR": {"A": {"pl": 0.0, "cost": 1000.0},
                  "B": {"pl": 0.0, "cost": 1000.0}}}
    out = alpha.session_update(
        st, "KR", agg, {"코스피": 6912.95}, "15:30", "2026-08-21",
        idx_previous_close={"코스피": 6852.58})
    assert st["day"]["KR"]["idx0"]["코스피"] == 6852.58   # 정정됨
    assert abs(out["idx"]["코스피"] - 0.881) < 0.01, out["idx"]
    print(f"[PASS] 기준선 자가정정 6471.17 → 6852.58 "
          f"(지수 {out['idx']['코스피']:+.2f}%)")


def test_correct_baseline_is_left_alone():
    """정상일에는 아무 일도 일어나지 않아야 한다(불필요한 변경 금지)."""
    st = {"day": {"KR": {
        "date": "2026-08-21", "basis": "previous_close",
        "pl0": 0.0, "a_pl0": 0.0, "b_pl0": 0.0,
        "idx0": {"코스피": 6852.58},
        "series": [], "series_v2": [], "holding_start_codes": []}}}
    agg = {"KR": {"A": {"pl": 0.0, "cost": 1000.0},
                  "B": {"pl": 0.0, "cost": 1000.0}}}
    alpha.session_update(st, "KR", agg, {"코스피": 6912.95}, "15:30",
                         "2026-08-21", idx_previous_close={"코스피": 6852.58})
    assert st["day"]["KR"]["idx0"]["코스피"] == 6852.58
    print("[PASS] 정상 기준선은 그대로 — 변경 없음")


# ── 소급 정정 ──────────────────────────────────────────────────
BARS = [("2026-08-18", 6869.83), ("2026-08-19", 6471.17),
        ("2026-08-20", 6852.58), ("2026-08-21", 6912.95)]


def test_true_daily_pct_needs_a_real_predecessor():
    assert abs(repair.true_daily_pct(BARS, "2026-08-21") - 0.881) < 0.01
    assert repair.true_daily_pct(BARS, "2026-08-18") is None   # 첫 봉 = 기준 없음
    assert repair.true_daily_pct(BARS, "2026-08-15") is None   # 휴장·범위 밖
    assert repair.true_daily_pct([("a", 0.0), ("b", 10.0)], "b") is None
    print("[PASS] 소급 정정 — 기준 없는 날은 0%가 아니라 None")


def _bars_map():
    return {sym: BARS for mkt in ("US", "KR") for sym, _n in alpha.IDX[mkt]}


def test_repair_skips_first_sample_rows():
    row = {"d": "2026-08-21", "mkt": "KR", "basis": "first_sample",
           "acct": -1.0, "idx": -0.3}
    plan = repair.plan_row(row, _bars_map())
    assert plan and plan.get("skip"), plan
    print("[PASS] 리베이스(first_sample) 행은 건너뜀 — 의도된 세션기준 보존")


def test_repair_fixes_corruption_without_touching_returns():
    row = {"d": "2026-08-21", "mkt": "KR", "basis": "previous_close",
           "acct": -1.65, "a": 0.21, "b": -3.48, "idx": 7.12,
           "indices": {"코스피": 7.12}, "daily_indices": {"코스피": 7.12}}
    plan = repair.plan_row(row, _bars_map())
    assert plan["corrupted"] is True
    repair.apply_plan(row, plan)
    assert abs(row["idx"] - 0.881) < 0.01, row["idx"]
    assert row["acct"] == -1.65 and row["a"] == 0.21 and row["b"] == -3.48
    assert repair.plan_row(row, _bars_map()) is None      # 멱등
    print("[PASS] 오염 정정 7.12 → 0.881 · 계좌 수익률 불변 · 재실행 무변화")


def test_repair_labels_backfill_separately():
    """비어 있던 자리를 채우는 건 '오염'이 아니다 — 오염일 수가 부풀면 안 된다."""
    row = {"d": "2026-08-21", "mkt": "KR", "basis": "previous_close",
           "acct": 1.0, "idx": 0.881,
           "indices": {"코스피": 0.881}, "daily_indices": {"코스피": 0.881}}
    plan = repair.plan_row(row, _bars_map())
    assert plan is not None and plan["corrupted"] is False, plan
    assert all(c["kind"] == "보강" for c in plan["changes"]), plan["changes"]
    print("[PASS] 결측 보강은 '정정'과 분리 집계")


def main():
    test_previous_close_comes_from_dated_bars()
    test_stale_chart_previous_close_is_ignored()
    test_insufficient_or_broken_bars_yield_none()
    test_live_price_used_but_only_when_consistent()
    test_absurd_index_move_is_not_published_as_a_number()
    test_locked_baseline_self_corrects_mid_session()
    test_correct_baseline_is_left_alone()
    test_true_daily_pct_needs_a_real_predecessor()
    test_repair_skips_first_sample_rows()
    test_repair_fixes_corruption_without_touching_returns()
    test_repair_labels_backfill_separately()
    print("\n지수 기준선 검증 통과 — 날짜 검증·이상치 차단·소급 정정 안전.")


if __name__ == "__main__":
    main()
