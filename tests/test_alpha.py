"""성과 vs 지수(알파) 추적 검증 — 네트워크·KIS 없이 순수 계산만.

  1) 집계: 시장×슬리브 분리 + 사용자 기보유(baseline) 제외
  2) 세션 기준점: 첫 틱=기준, 이후 계좌%/지수%가 세션시작 대비로 계산
  3) 장중 신규 매수(플로우)가 계좌%를 왜곡하지 않음
  4) 캡처 통계: 상승/하락 캡처·지수 이긴 날, 표본<5면 미표시

실행: python -m tests.test_alpha
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha


def _row(code, mkt, cost, pl, ccy=None):
    return {"code": code, "market": mkt, "ccy": ccy or ("KRW" if mkt == "KR" else "USD"),
            "buy_amt": cost, "pl_amt": pl}


def test_aggregate():
    rows = [_row("AAA", "US", 1000, 10), _row("BBB", "US", 500, -5),
            _row("CCC", "KR", 2000, 20), _row("BASE", "KR", 999, 99)]
    agg = alpha.aggregate(rows, b_codes={"BBB"}, baseline={"BASE"})
    assert agg["US"]["A"] == {"cost": 1000, "pl": 10}
    assert agg["US"]["B"] == {"cost": 500, "pl": -5}
    assert agg["KR"]["A"] == {"cost": 2000, "pl": 20}
    assert agg["KR"]["B"]["cost"] == 0          # baseline은 어디에도 안 들어감
    print("[PASS] 집계: 시장×슬리브 분리 + 기보유 제외")


def test_session_and_flow_neutral():
    st = {}
    agg1 = {"US": {"A": {"cost": 1000.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}},
            "KR": {"A": {"cost": 0.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}}}
    r1 = alpha.session_update(
        st, "US", agg1, {"나스닥": 20000.0, "S&P500": 6000.0},
        "22:30", "2026-07-24")
    assert r1["acct"] == 0.0 and r1["idx"]["나스닥"] == 0.0     # 기준점
    # 지수 +1%, 계좌 pl +20 (2%)
    agg2 = {"US": {"A": {"cost": 1000.0, "pl": 20.0}, "B": {"cost": 0.0, "pl": 0.0}},
            "KR": agg1["KR"]}
    r2 = alpha.session_update(
        st, "US", agg2, {"나스닥": 20200.0, "S&P500": 6030.0},
        "23:30", "2026-07-24")
    assert abs(r2["acct"] - 2.0) < 1e-9 and abs(r2["idx"]["나스닥"] - 1.0) < 1e-9
    assert abs(r2["idx"]["S&P500"] - .5) < 1e-9
    # 장중 신규 매수(B에 cost 500, pl 0 추가) → 계좌%가 크게 안 튐(분모만 증가)
    agg3 = {"US": {"A": {"cost": 1000.0, "pl": 20.0}, "B": {"cost": 500.0, "pl": 0.0}},
            "KR": agg1["KR"]}
    r3 = alpha.session_update(
        st, "US", agg3, {"나스닥": 20200.0, "S&P500": 6030.0},
        "23:35", "2026-07-24")
    assert abs(r3["acct"] - (20.0 / 1500.0 * 100)) < 1e-9      # 왜곡 없음(희석만)
    assert len(st["day"]["US"]["series"]) == 3
    rich = st["day"]["US"]["series_v2"][-1]
    assert rich["A"] == 2.0 and rich["B"] == 0.0
    assert set(rich["indices"]) == {"나스닥", "S&P500"}
    print("[PASS] 세션 기준점 + A/B·복수지수 + 플로우 중립")


def test_nav_twr_uses_previous_close_and_removes_trade_flows():
    st = {"carry": {"US": {"date": "2026-07-23", "nav_last": {
        "account": {"value": 1000.0, "flow": 1000.0},
        "A": {"value": 1000.0, "flow": 1000.0},
        "B": {"value": 0.0, "flow": 0.0},
    }}}}
    agg = {"US": {"A": {"cost": 0.0, "pl": 0.0},
                  "B": {"cost": 0.0, "pl": 0.0}},
           "KR": {"A": {"cost": 0.0, "pl": 0.0},
                  "B": {"cost": 0.0, "pl": 0.0}}}
    nav1 = {
        "account": {"value": 1120.0, "flow": 1100.0},
        "A": {"value": 1120.0, "flow": 1100.0},
        "B": {"value": 0.0, "flow": 0.0},
    }
    first = alpha.session_update(
        st, "US", agg, {"나스닥": 20200.0}, "22:35", "2026-07-24",
        nav=nav1, idx_previous_close={"나스닥": 20000.0})
    assert abs(first["acct"] - 2.0) < 1e-9
    assert abs(first["idx"]["나스닥"] - 1.0) < 1e-9
    assert st["day"]["US"]["basis"] == "previous_close"
    # 500원어치를 같은 가격에 매도: 보유평가 -500, 순유입 flow -500 → 수익률 불변.
    nav2 = {
        "account": {"value": 620.0, "flow": 600.0},
        "A": {"value": 620.0, "flow": 600.0},
        "B": {"value": 0.0, "flow": 0.0},
    }
    second = alpha.session_update(
        st, "US", agg, {"나스닥": 20200.0}, "22:40", "2026-07-24",
        nav=nav2, idx_previous_close={"나스닥": 20000.0})
    assert abs(second["acct"] - 2.0) < 1e-9
    print("[PASS] NAV/TWR: 전일종가 기준 + 매수·매도 현금흐름 제거")


def _nav(value, flow):
    return {"account": {"value": value, "flow": flow},
            "A": {"value": value, "flow": flow},
            "B": {"value": 0.0, "flow": 0.0}}


def _flat_agg():
    return {"US": {"A": {"cost": 0.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}},
            "KR": {"A": {"cost": 0.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}}}


def test_unaccounted_sell_does_not_carve_phantom_cliff():
    """2026-08-03 재현: 매도로 브로커 평가액만 먼저 줄고 원장 회계는 지연.

    종전에는 그 구간이 계좌 -6.2%p 절벽으로 각인되고, 회계가 세션 안에 못
    따라잡으면 마감 기록까지 영구 오염됐다.
    """
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    base = alpha.session_update(
        st, "US", agg, idx, "22:35", "2026-08-03", nav=_nav(100.0, 0.0))
    assert abs(base["acct"]) < 1e-9

    # 6.2원어치 손절 매도 — 평가액은 즉시 반영, 회계(flow)는 아직.
    lag = alpha.session_update(
        st, "US", agg, idx, "22:40", "2026-08-03",
        nav=_nav(93.8, 0.0), accounting_settled=False)
    assert abs(lag["acct"]) < 1e-9, f"유령 절벽 발생: {lag['acct']}"
    assert st["day"]["US"]["accounting_pending"] is True

    # 회계가 붙으면 원래 기준선에서 한 번에 계산 → 손실 0.
    done = alpha.session_update(
        st, "US", agg, idx, "22:45", "2026-08-03",
        nav=_nav(93.8, -6.2), accounting_settled=True)
    assert abs(done["acct"]) < 1e-9, f"정산 후에도 오차: {done['acct']}"
    assert st["day"]["US"]["accounting_pending"] is False

    # 정산 뒤의 진짜 시세 하락은 그대로 반영돼야 한다(과잉 억제 금지).
    real = alpha.session_update(
        st, "US", agg, idx, "22:50", "2026-08-03", nav=_nav(92.862, -6.2))
    assert abs(real["acct"] - (-1.0)) < 1e-6, real["acct"]
    print("[PASS] 미회계 매도는 유령 절벽 0 · 정산 후 실제 등락은 그대로")


def test_unaccounted_buy_does_not_fabricate_gain():
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    alpha.session_update(
        st, "US", agg, idx, "22:35", "2026-08-03", nav=_nav(100.0, 0.0))
    lag = alpha.session_update(
        st, "US", agg, idx, "22:40", "2026-08-03",
        nav=_nav(130.0, 0.0), accounting_settled=False)
    assert abs(lag["acct"]) < 1e-9, f"유령 이익 발생: {lag['acct']}"
    done = alpha.session_update(
        st, "US", agg, idx, "22:45", "2026-08-03",
        nav=_nav(130.0, 30.0), accounting_settled=True)
    assert abs(done["acct"]) < 1e-9
    print("[PASS] 미회계 매수도 공짜 이익으로 잡히지 않음")


def test_pending_close_is_not_written_into_cumulative_days():
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    alpha.session_update(
        st, "US", agg, idx, "22:35", "2026-08-03", nav=_nav(100.0, 0.0))
    alpha.session_update(
        st, "US", agg, idx, "22:40", "2026-08-03",
        nav=_nav(93.8, 0.0), accounting_settled=False)
    sent = []
    with mock.patch.object(alpha.notify, "send_photo", return_value=False), \
            mock.patch.object(alpha.notify, "send",
                              side_effect=lambda *a, **k: sent.append(a[0])):
        alpha._close_alert(st, "US", st["day"]["US"])
    # 미정산 날은 숫자 대신 **품질 행**으로 남는다(사라지면 P1-3 재발).
    assert len(st["days"]) == 1 and st["days"][0]["quality"] == "pending"
    assert st["days"][0]["acct"] is None
    assert any("정산 대기" in text for text in sent), sent
    # 다음 세션이 정산 상태로 마감하면 정상 기록된다.
    st2 = {"carry": st.get("carry")}
    alpha.session_update(
        st2, "US", agg, idx, "22:35", "2026-08-04", nav=_nav(93.8, -6.2))
    alpha.session_update(
        st2, "US", agg, idx, "22:40", "2026-08-04", nav=_nav(94.738, -6.2))
    with mock.patch.object(alpha.notify, "send_photo", return_value=True), \
            mock.patch.object(alpha.notify, "send"):
        alpha._close_alert(st2, "US", st2["day"]["US"])
    ok_rows = [r for r in st2["days"] if r.get("quality") == "ok"]
    assert len(ok_rows) == 1 and ok_rows[0]["d"] == "2026-08-04"
    assert ok_rows[0]["acct"] is not None
    print("[PASS] 미정산 마감일은 품질 행으로 보존 · 정산일은 숫자로 기록")


def test_observed_cliffs_are_rejected_regardless_of_cause():
    """실측된 3개 절벽을 원인 불문 차단한다.

    2026-07 -17%p · 08-03 -6.2%p · 08-04 -4.9%p. 원인은 매번 달랐으므로
    원인별 가드가 아니라 '한 틱 변동폭' 자체를 검증해야 재발이 끊긴다.
    """
    for label, drop in (("-17%", 0.17), ("08-03 -6.2%", 0.062),
                        ("08-04 -4.9%", 0.049)):
        st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
        with mock.patch.object(alpha.notify, "send"):
            alpha.session_update(
                st, "US", agg, idx, "22:31", "2026-08-04", nav=_nav(100.0, 0.0))
            # 원인 불문(회계는 정상이라고 보고됨) — 값만 절벽.
            hit = alpha.session_update(
                st, "US", agg, idx, "22:36", "2026-08-04",
                nav=_nav(100.0 * (1 - drop), 0.0), accounting_settled=True)
        # 절벽은 누적되지 않고, hold 동안은 확정 숫자 대신 검증 중(None)이다.
        assert hit["acct"] is None, f"{label} 절벽이 통과됨: {hit['acct']}"
        assert st["day"]["US"]["anomaly_pending"], label
        assert st["day"]["US"]["wealth"]["account"] == 1.0, label  # 누적 없음
    print("[PASS] 실측 절벽 3종(-17%·-6.2%·-4.9%) 원인 불문 차단")


def test_normal_moves_still_accumulate():
    """과잉 억제 금지 — 정상 범위 등락은 그대로 반영돼야 한다."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    alpha.session_update(
        st, "US", agg, idx, "22:31", "2026-08-04", nav=_nav(100.0, 0.0))
    a = alpha.session_update(
        st, "US", agg, idx, "22:36", "2026-08-04", nav=_nav(101.0, 0.0))
    assert abs(a["acct"] - 1.0) < 1e-9, a["acct"]
    b = alpha.session_update(
        st, "US", agg, idx, "22:41", "2026-08-04", nav=_nav(99.99, 0.0))
    assert abs(b["acct"] - (-0.01)) < 1e-6, b["acct"]   # 복리 누적 유지
    assert not st["day"]["US"].get("anomaly_pending")
    # 매수·매도 현금흐름은 절벽으로 오인되지 않는다(외부흐름 제거가 먼저).
    c = alpha.session_update(
        st, "US", agg, idx, "22:46", "2026-08-04", nav=_nav(149.99, 50.0))
    assert abs(c["acct"] - (-0.01)) < 1e-6, c["acct"]
    print("[PASS] 정상 등락·현금흐름은 과잉 억제 없이 그대로 반영")


def test_persistent_anomaly_is_quarantined_not_confirmed_as_zero():
    """Codex P1-1: 지속 이상을 0%로 확정하면 실제 폭락도 삭제된다.

    실제 손익인지 데이터 오류인지 이 자리에서 증명할 수 없으므로 **미확정
    (None)** 으로 격리하고, 이후 추적만 새 기준선에서 재개한다.
    """
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    sent = []
    with mock.patch.object(alpha.notify, "send",
                           side_effect=lambda *a, **k: sent.append(a[0])):
        alpha.session_update(
            st, "US", agg, idx, "22:31", "2026-08-04", nav=_nav(100.0, 0.0))
        for i in range(alpha._ANOMALY_HOLD_TICKS):
            out = alpha.session_update(
                st, "US", agg, idx, f"22:{36 + i}", "2026-08-04",
                nav=_nav(90.0, 0.0))
            # 보류(hold) 중엔 확정 숫자를 내보내지 않는다 — '검증 중' 표시
            #   (Codex V4 P2-1: 의심 입력이 포함된 값이 15분간 확정처럼 보임).
            assert out["acct"] is None, (i, out["acct"])
            assert "account" in st["day"]["US"]["pending_keys"]
        final = alpha.session_update(
            st, "US", agg, idx, "22:50", "2026-08-04", nav=_nav(90.0, 0.0))
    assert final["acct"] is None, final["acct"]        # 0%로 확정하지 않는다
    assert st["day"]["US"]["unresolved"] == ["A", "account"]
    assert st["day"]["US"]["nav_prev"]["account"]["value"] == 90.0  # 추적 재개용
    assert st["day"]["US"]["series"][-1][1] is None    # 차트에도 숫자 아님
    # 격리 사실이 별도 경보로 나간다(보류 알림에 삼켜지지 않음 — P2-3).
    assert any("미확정" in text for text in sent), sent
    assert len(st["day"]["US"]["anomaly_log"]) >= 2    # hold + quarantine
    print("[PASS] 지속 이상은 0% 확정 대신 미확정 격리 · 격리 경보 별도 발송")


def test_one_sleeve_anomaly_does_not_erase_other_keys():
    """Codex P1-2: A만 이상인데 계좌·B의 정상 수익까지 삭제되던 문제.

    입력은 nav_inputs 불변식 account=A+B를 지키는 현실적 구성(Codex P2-4):
    A는 계좌의 10%만 차지 — A -15%여도 계좌는 -0.6%로 정상 범위.
    """
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    nav0 = {"account": {"value": 200.0, "flow": 0.0},
            "A": {"value": 20.0, "flow": 0.0},
            "B": {"value": 180.0, "flow": 0.0}}
    nav1 = {"account": {"value": 198.8, "flow": 0.0},   # = 17 + 181.8
            "A": {"value": 17.0, "flow": 0.0},          # -15% (한계 초과)
            "B": {"value": 181.8, "flow": 0.0}}         # +1% (정상)
    with mock.patch.object(alpha.notify, "send"):
        alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-04",
                             nav=nav0)
        out = alpha.session_update(st, "US", agg, idx, "22:36", "2026-08-04",
                                   nav=nav1)
    # A hold 동안 A와 (A를 가중 포함한) account는 '검증 중' — 숫자 금지(V4 P2-1).
    #   독립 증명이 있는 B만 숫자다. 내부 wealth·기준선은 정상 유지된다.
    assert out["acct"] is None, out["acct"]
    assert out["a"] is None, out["a"]
    assert abs(out["b"] - 1.0) < 1e-6, out["b"]
    assert st["day"]["US"]["pending_keys"] == ["A", "account"]
    assert st["day"]["US"]["nav_prev"]["A"]["value"] == 20.0    # A 기준선 유지
    assert abs(st["day"]["US"]["nav_prev"]["B"]["value"] - 181.8) < 1e-9
    print("[PASS] 이상 판정은 계좌/A/B 독립 — 정상 키만 숫자·hold는 검증 중")


def test_close_keeps_per_key_values_and_quality_row():
    """Codex TWR-V2 P1-2·P1-3: A만 미확정이어도 계좌·B의 일간 성과는 기록되고,
    미확정 하루도 품질 행으로 역사에 남아야 한다."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    nav0 = {"account": {"value": 200.0, "flow": 0.0},
            "A": {"value": 20.0, "flow": 0.0},
            "B": {"value": 180.0, "flow": 0.0}}
    with mock.patch.object(alpha.notify, "send") as sent, \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-04", nav=nav0)
        for i in range(alpha._ANOMALY_HOLD_TICKS + 1):   # A만 지속 이상 → 격리
            nav = {"account": {"value": 198.8, "flow": 0.0},
                   "A": {"value": 17.0, "flow": 0.0},
                   "B": {"value": 181.8, "flow": 0.0}}
            alpha.session_update(st, "US", agg, idx, f"22:{36 + i}",
                                 "2026-08-04", nav=nav)
        # account는 A+B 종속값 — A 격리는 account도 미확정으로 전파(V3 P1-1).
        assert st["day"]["US"]["unresolved"] == ["A", "account"]
        alpha._close_alert(st, "US", st["day"]["US"])
    # 하루가 사라지지 않는다 — 품질 행으로 남고, 확정 가능한 키(B)는 숫자.
    assert len(st["days"]) == 1
    row = st["days"][0]
    assert row["quality"] == "unresolved"
    assert row["unresolved_keys"] == ["A", "account"]
    assert row["a"] is None and row["acct"] is None      # 의심 가중 값 확정 금지
    assert row["b"] is not None                          # 독립 증명 있는 B만 확정
    # carry — account 미확정이므로 다음 세션은 계좌·지수를 같은 첫 표본 0%로.
    assert set(st["carry"]["US"]["unresolved"]) == {"A", "account"}
    st2 = {"carry": {"US": st["carry"]["US"]}}
    out = alpha.session_update(
        st2, "US", agg, idx, "22:31", "2026-08-05",
        nav={"account": {"value": 198.8, "flow": 0.0},
             "A": {"value": 17.0, "flow": 0.0},
             "B": {"value": 181.8, "flow": 0.0}},
        idx_previous_close={"나스닥": 20000.0})
    assert st2["day"]["US"]["basis"] == "first_sample"
    assert abs(out["acct"]) < 1e-9                           # 이월 손익 혼입 없음
    assert abs(out["idx"]["나스닥"]) < 1e-9                  # 지수도 같은 기준
    print("[PASS] 슬리브 미확정은 account로 전파 · B만 확정 · 품질 행 보존")


def test_capture_stats_reports_dropped_quality_rows():
    days = ([{"mkt": "US", "acct": 1.0, "idx": 0.5}] * 5
            + [{"mkt": "US", "acct": None, "idx": 0.4,
                "quality": "unresolved"}])
    text = alpha.capture_stats(days, "US")
    assert "미확정 제외 1일" in text, text
    print("[PASS] 캡처 통계가 미확정 제외 일수를 명시(편향 가시화)")


def test_none_consumers_do_not_crash_or_show_zero():
    """Codex TWR-V2 P1-1: 알림·텔레그램이 None에서 예외·0% 표시하던 문제."""
    assert alpha._fmt(None) == "미확정"
    line = alpha._vs_line(None, {"나스닥": 1.6})
    assert "미확정" in line and "0.0" not in line and "🔴" not in line
    sent = []
    with mock.patch.object(alpha.notify, "send",
                           side_effect=lambda *a, **k: sent.append(a[0])), \
            mock.patch.object(alpha.notify, "send_photo", return_value=False):
        alpha._mid_alert({}, "US", {
            "acct": None, "a": None, "b": 1.0,
            "idx": {"나스닥": 1.6}, "series": [["22:31", None, 1.6]]})
    assert sent and "미확정" in sent[0]
    from bot import kis_telegram
    with mock.patch.object(alpha, "_load", return_value={
            "day": {"US": {"date": "2026-08-04",
                           "series": [["23:00", None, 1.6]]}},
            "days": []}), \
            mock.patch.object(kis_telegram, "notify", create=True), \
            mock.patch("bot.notify.send_photo", return_value=True):
        text = kis_telegram._perf_text()
    assert "미확정" in text and "판정 보류" in text
    print("[PASS] 장중 알림·/성과가 None을 미확정으로 표시(예외·0% 없음)")


def test_sleeve_quarantine_propagates_to_account():
    """Codex V3 P1-1 재현: A는 미확정인데 A를 포함해 더한 account가 확정되던 것."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    nav0 = {"account": {"value": 200.0, "flow": 0.0},
            "A": {"value": 20.0, "flow": 0.0},
            "B": {"value": 180.0, "flow": 0.0}}
    nav1 = {"account": {"value": 198.8, "flow": 0.0},   # -0.6% (한계 안)
            "A": {"value": 17.0, "flow": 0.0},          # -15% (한계 초과)
            "B": {"value": 181.8, "flow": 0.0}}         # +1%
    with mock.patch.object(alpha.notify, "send"):
        alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-04", nav=nav0)
        out = None
        for i in range(alpha._ANOMALY_HOLD_TICKS + 1):
            out = alpha.session_update(st, "US", agg, idx, f"22:{36 + i}",
                                       "2026-08-04", nav=nav1)
    assert out["a"] is None                            # A 격리
    assert out["acct"] is None, out["acct"]            # 종속값 account도 미확정
    assert abs(out["b"] - 1.0) < 1e-6                  # 독립 확정 가능한 B만 숫자
    assert "account" in st["day"]["US"]["unresolved"]
    print("[PASS] 슬리브 격리가 종속값 account로 전파(의심 가중 값 확정 금지)")


def test_missing_primary_index_is_none_not_substituted():
    """Codex V3 P1-3: 주 지수 결측일이 0%/다른 지수로 둔갑하던 문제."""
    st, agg = {}, _flat_agg()
    with mock.patch.object(alpha.notify, "send"):
        alpha.session_update(st, "US", agg, {"나스닥": 20000.0, "S&P500": 5000.0},
                             "22:31", "2026-08-04", nav=_nav(100.0, 0.0))
        # 나스닥만 결측 — S&P500이 주 지수 자리에 대체되면 안 된다.
        out = alpha.session_update(st, "US", agg, {"S&P500": 5100.0},
                                   "22:36", "2026-08-04", nav=_nav(101.0, 0.0))
    last = st["day"]["US"]["series"][-1]
    assert last[2] is None, last                       # 주 지수 자리 None
    assert abs(out["idx"]["S&P500"] - 2.0) < 1e-9      # S&P500은 자기 이름으로
    with mock.patch.object(alpha.notify, "send"), \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        alpha._close_alert(st, "US", st["day"]["US"])
    assert st["days"][0]["idx"] is None                # 일별 행도 결측 유지
    text = alpha.capture_stats(st["days"], "US")
    assert "미확정 제외 1일" in text, text             # 통계 표본에서 제외 표기
    print("[PASS] 주 지수 결측은 None 유지 — 대체·0% 둔갑 없음")


def test_capture_stats_shows_dropped_even_below_five_samples():
    """Codex V3 P2-1: 표본 5일 미만이면 제외 일수까지 숨던 문제."""
    days = ([{"mkt": "US", "acct": 1.0, "idx": 0.5}] * 4
            + [{"mkt": "US", "acct": None, "idx": 0.4}] * 2)
    text = alpha.capture_stats(days, "US")
    assert "누적 4일" in text and "미확정 제외 2일" in text, text
    assert alpha.capture_stats([], "US") == ""
    print("[PASS] 표본 초기에도 미확정 제외 일수 표시")


def test_legacy_series_with_none_does_not_crash_snapshot():
    """Codex V3 P2-2: 구형 series에 None이 있으면 스냅샷 API 전체가 죽던 문제."""
    st = {"day": {"US": {"date": "2026-08-04", "basis": "first_sample",
                         "series": [["10:00", None, 0.2],
                                    ["10:05", 1.0, None],
                                    ["bad"],
                                    ["10:10", "corrupt", 0.3]],
                         "series_v2": []}},
          "days": []}
    snap = alpha.dashboard_snapshot(st)
    series = snap["markets"]["US"]["series"]
    assert len(series) == 2                            # 손상 행만 제외
    assert series[0]["account"] is None                # None 보존(0 강등 금지)
    assert series[1]["indices"]["나스닥"] is None
    print("[PASS] 구형 series None·손상 행에서 스냅샷 API 생존")


def test_dashboard_snapshot_preserves_none_and_quality():
    st = {"day": {}, "days": [
        {"d": "2026-08-04", "mkt": "US", "acct": None, "idx": 1.2,
         "quality": "unresolved", "unresolved_keys": ["account"],
         "a": None, "b": 0.5},
        {"d": "2026-08-05", "mkt": "US", "acct": 1.0, "idx": 0.3},
    ]}
    snap = alpha.dashboard_snapshot(st)
    rows = snap["days"]
    assert rows[0]["account"] is None                   # 0으로 낮추지 않음
    assert rows[0]["quality"] == "unresolved"
    assert rows[0]["unresolved_keys"] == ["account"]
    assert rows[1]["account"] == 1.0 and rows[1]["quality"] == "ok"
    print("[PASS] 대시보드 스냅샷이 None·품질 등급을 그대로 전파")


def test_unresolved_day_does_not_leak_into_next_session_basis():
    """Codex P1-3: 보류한 전날 손익이 다음 날 계좌 수익으로 넘어가던 경로."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    with mock.patch.object(alpha.notify, "send"), \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-03",
                             nav=_nav(100.0, 0.0))
        alpha.session_update(st, "US", agg, idx, "22:36", "2026-08-03",
                             nav=_nav(92.0, 0.0), accounting_settled=False)
        alpha._close_alert(st, "US", st["day"]["US"])
        # 전 키 의심(미정산)이므로 carry unresolved는 전체 키 집합이다.
        assert set(st["carry"]["US"]["unresolved"]) == {"A", "B", "account"}
        assert all(r.get("acct") is None for r in st.get("days") or [])
        # 다음 날: 오래된 기준선을 이어받지 않고 계좌·지수가 함께 첫 표본 0%.
        out = alpha.session_update(
            st, "US", agg, idx, "22:31", "2026-08-04",
            nav=_nav(92.0, -6.0), idx_previous_close={"나스닥": 19800.0})
    assert st["day"]["US"]["basis"] == "first_sample"
    assert abs(out["acct"]) < 1e-9, out["acct"]        # 전날 손익이 안 넘어옴
    assert abs(out["idx"]["나스닥"]) < 1e-9            # 지수도 같은 순간 0%
    print("[PASS] 미확정 마감일의 기준선이 다음 세션 지수 비교로 새지 않음")


def test_reanchored_partial_session_is_excluded_from_cumulative():
    """Codex P2-1: 재기준 이후의 부분 세션이 하루처럼 누적에 들어가던 문제."""
    st, agg, idx = {"reanchored": {"US": {"date": "2026-08-04"}}}, _flat_agg(), \
        {"나스닥": 20000.0}
    with mock.patch.object(alpha.notify, "send"), \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        alpha.session_update(st, "US", agg, idx, "23:10", "2026-08-04",
                             nav=_nav(100.0, 0.0))
        alpha.session_update(st, "US", agg, idx, "23:15", "2026-08-04",
                             nav=_nav(101.0, 0.0))
        assert st["day"]["US"]["partial_session"] is True
        alpha._close_alert(st, "US", st["day"]["US"])
    # 품질 행(quality=partial, 값 None)으로 남고 숫자로는 집계되지 않는다.
    assert len(st["days"]) == 1 and st["days"][0]["quality"] == "partial"
    assert st["days"][0]["acct"] is None
    print("[PASS] 재기준 부분 세션은 숫자 집계 제외 · 품질 행으로 보존")


def test_zero_and_nonfinite_values_cannot_wipe_the_account():
    """브로커가 빈 잔고/0을 주면 종전 가드(interval > -1)는 -99%를 통과시켰다."""
    for bad in (0.5, 0.0):
        st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
        with mock.patch.object(alpha.notify, "send"):
            alpha.session_update(
                st, "US", agg, idx, "22:31", "2026-08-04", nav=_nav(100.0, 0.0))
            out = alpha.session_update(
                st, "US", agg, idx, "22:36", "2026-08-04", nav=_nav(bad, 0.0))
        # 누적 금지 + hold 동안 확정 숫자 금지(검증 중 None).
        assert out["acct"] is None, f"value={bad} 가 계좌를 지움: {out}"
        assert st["day"]["US"]["wealth"]["account"] == 1.0, bad
    print("[PASS] 잔고 0/붕괴 응답이 계좌 수익률을 지우지 못함")


def test_session_first_gap_allows_overnight_but_not_absurd():
    """세션 첫 구간만 전일 갭을 허용하되 무한정은 아니다."""
    carry = {"US": {"date": "2026-08-03", "nav_last": {
        "account": {"value": 100.0, "flow": 0.0},
        "A": {"value": 100.0, "flow": 0.0},
        "B": {"value": 0.0, "flow": 0.0}}}}
    st = {"carry": dict(carry)}
    out = alpha.session_update(
        st, "US", _flat_agg(), {"나스닥": 20000.0}, "22:31", "2026-08-04",
        nav=_nav(95.0, 0.0), idx_previous_close={"나스닥": 19900.0})
    assert st["day"]["US"]["basis"] == "previous_close"
    assert abs(out["acct"] - (-5.0)) < 1e-9, out["acct"]   # 5% 갭은 정상 반영
    st2 = {"carry": dict(carry)}
    with mock.patch.object(alpha.notify, "send"):
        out2 = alpha.session_update(
            st2, "US", _flat_agg(), {"나스닥": 20000.0}, "22:31", "2026-08-04",
            nav=_nav(70.0, 0.0), idx_previous_close={"나스닥": 19900.0})
    # 30% 갭은 보류 — hold 동안은 확정 숫자 대신 검증 중(None, V4 P2-1).
    assert out2["acct"] is None, out2["acct"]
    assert st2["day"]["US"]["wealth"]["account"] == 1.0    # 누적 없음
    print("[PASS] 첫 구간 갭은 허용범위까지만 · 과대 갭은 보류")


def test_holdings_equal_weight_uses_starting_positions_only():
    import pandas as pd
    rows = [
        {**_row("AAA", "US", 100, 10), "cur": 110},
        {**_row("BBB", "US", 100, -10), "cur": 90},
        {**_row("NEW", "US", 100, 20), "cur": 120},
        {**_row("MANUAL_NEW", "US", 100, 30), "cur": 130},
    ]
    frames = {
        code: pd.DataFrame(
            {"Close": [100.0]},
            index=pd.to_datetime(["2026-07-23"]))
        for code in ("AAA", "BBB", "NEW", "MANUAL_NEW")
    }
    recs = {
        "AAA": {"opened": "2026-07-22", "sleeve": "A"},
        "BBB": {"opened": "2026-07-22", "sleeve": "B"},
        "NEW": {"opened": "2026-07-24", "sleeve": "A"},
    }
    with mock.patch("scanner.cache.load", side_effect=lambda code: frames.get(code)):
        out = alpha.holdings_equal_weight(
            rows, "US", recs, set(), "2026-07-24",
            # 첫 틱 스냅샷에 수동매수가 섞여도 opened 추적일이 없으면 제외.
            start_codes={"AAA", "BBB", "MANUAL_NEW"})
    assert abs(out["account"]) < 1e-9              # +10%와 -10% 동일가중=0%
    assert abs(out["A"] - 10.0) < 1e-9 and abs(out["B"] + 10.0) < 1e-9
    assert out["covered"] == 2 and out["eligible"] == 2
    print("[PASS] 장시작 보유만 전일종가 대비 동일가중(봇·수동 신규 제외)")


def test_capture_stats():
    days = [{"d": f"d{i}", "mkt": "US", "acct": a, "idx": ix}
            for i, (a, ix) in enumerate(
                [(0.5, 1.0), (1.2, 1.0), (-0.3, -1.0), (-0.5, -1.0), (0.2, 0.5)])]
    s = alpha.capture_stats(days, "US")
    assert "상승일 캡처" in s and "하락일 캡처" in s and "지수 이긴 날" in s
    # 표본<5 → 통계 대신 진행 상황 표시(제외 일수 가시화 계약 — V3 P2-1)
    assert "누적 3일" in alpha.capture_stats(days[:3], "US")
    print("[PASS] 캡처 통계(상승/하락/승률) + 표본 최소 5일")


def test_state_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        alpha.STATE_PATH = os.path.join(tmp, "alpha_state.json")
        alpha._save({"x": 1})
        loaded = alpha._load()
        assert loaded["x"] == 1 and loaded.get("updated_at")
    print("[PASS] 상태 저장/복원")


def test_accounting_migration_rebase_is_atomic_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        alpha.STATE_PATH = os.path.join(tmp, "alpha_state.json")
        alpha._save({
            "day": {"US": {"date": "2026-07-28", "series": [
                ["23:30", -16.4, -.2]], "series_v2": [{
                    "t": "23:30", "account": -16.4,
                    "indices": {"나스닥": -.2},
                }]}},
            "days": [{"d": "2026-07-28", "mkt": "US",
                      "acct": -16.4, "idx": -.2}],
            "carry": {"US": {"nav_last": {"account": {
                "value": 100, "flow": 200}}}},
        })
        with mock.patch.object(alpha, "publish_dash") as publish:
            result = alpha.rebase_after_accounting_migration(
                "plan-sha", started_at=1785250800, archived=True)
        publish.assert_called_once()
        assert result["rebased"] is True
        state = alpha._load()
        assert state["day"] == {} and state["days"] == [] and state["carry"] == {}
        assert state["performance_epoch"]["id"] == "plan-sha"
        assert state["performance_epoch"]["archived_previous_state"] is True

        # 같은 이관 plan 재실행은 새로 쌓인 성과를 다시 지우지 않는다.
        state["days"].append({"d": "new-valid-day"})
        alpha._save(state)
        with mock.patch.object(alpha, "publish_dash") as republish:
            again = alpha.rebase_after_accounting_migration(
                "plan-sha", started_at=1785250900, archived=True)
        republish.assert_not_called()
        assert again["already_applied"] is True
        assert alpha._load()["days"] == [{"d": "new-valid-day"}]

        # 첫 새 표본은 계좌와 지수를 같은 시각의 0%로 시작한다.
        fresh = alpha._load()
        agg = {
            "US": {"A": {"cost": 100.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}},
            "KR": {"A": {"cost": 0.0, "pl": 0.0},
                   "B": {"cost": 0.0, "pl": 0.0}},
        }
        out = alpha.session_update(
            fresh, "US", agg, {"나스닥": 20000.0},
            "22:30", "2026-07-29",
            nav={
                "account": {"value": 100.0, "flow": 100.0},
                "A": {"value": 100.0, "flow": 100.0},
                "B": {"value": 0.0, "flow": 0.0},
            },
            idx_previous_close={"나스닥": 19900.0})
        assert out["acct"] == 0.0 and out["idx"]["나스닥"] == 0.0
        assert fresh["day"]["US"]["basis"] == "first_sample"
        first_point = fresh["day"]["US"]["series_v2"][0]
        assert first_point["daily_indices"]["나스닥"] == 0.0
    print("[PASS] 레거시 이관 후 계좌·지수 동시 0% 리베이스 + 재실행 멱등")


def test_dashboard_snapshot_is_percentage_only():
    st = {
        "updated_at": "2026-07-24T00:00:00+00:00",
        "day": {"US": {
            "date": "2026-07-24",
            "series_v2": [{
                "t": "23:00", "account": 1.2, "A": 1.5, "B": -.2,
                "indices": {"나스닥": .8, "S&P500": .6},
            }],
        }},
        "days": [{
            "d": "2026-07-23", "mkt": "KR", "acct": .5, "idx": .2,
            "a": .4, "b": .7, "indices": {"코스피": .2, "코스닥": .3},
        }],
    }
    payload = alpha.dashboard_snapshot(st)
    assert payload["markets"]["US"]["series"][0]["A"] == 1.5
    assert payload["markets"]["US"]["indices"] == ["나스닥", "S&P500"]
    assert payload["markets"]["KR"]["indices"] == ["코스피", "코스닥"]
    assert payload["days"][0]["indices"]["코스닥"] == .3
    assert payload["version"] == alpha.SNAPSHOT_VERSION
    assert "positions" not in payload and "amount" not in payload
    print("[PASS] 개인 웹 성과 스냅샷 — A/B·4대 지수·퍼센트 전용")


def test_missing_previous_close_never_becomes_zero_percent():
    """Codex V4 P1-2: carry 세션에서 전일종가 없는 지수가 현재값 기준 0%로
    quality=ok 확정되던 경로. 그 지수는 명시적 None(미확정)이어야 한다."""
    carry = {"US": {"date": "2026-08-03", "nav_last": {
        "account": {"value": 100.0, "flow": 0.0},
        "A": {"value": 100.0, "flow": 0.0},
        "B": {"value": 0.0, "flow": 0.0}}}}
    st = {"carry": dict(carry)}
    with mock.patch.object(alpha.notify, "send"), \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        out = alpha.session_update(
            st, "US", _flat_agg(), {"나스닥": 20000.0, "S&P500": 5100.0},
            "22:31", "2026-08-04", nav=_nav(101.0, 0.0),
            idx_previous_close={"S&P500": 5000.0})    # 나스닥 전일종가 결측
    day = st["day"]["US"]
    assert day["basis"] == "previous_close"
    assert out["idx"]["나스닥"] is None, out["idx"]        # 0% 확정 금지
    assert abs(out["idx"]["S&P500"] - 2.0) < 1e-9          # 있는 지수만 숫자
    assert day["series"][-1][2] is None                    # 주 지수 자리 None
    point = day["series_v2"][-1]
    assert point["indices"]["나스닥"] is None
    assert "나스닥" in point["daily_indices"]              # 키 자체는 존재 —
    assert point["daily_indices"]["나스닥"] is None        # **명시적** None
    assert point["daily_indices"]["S&P500"] is not None
    # 다음 틱에 전일종가가 복구되면 — 현재값이 아니라 **전일종가**가 기준이 된다.
    with mock.patch.object(alpha.notify, "send"):
        out2 = alpha.session_update(
            st, "US", _flat_agg(), {"나스닥": 20200.0, "S&P500": 5100.0},
            "22:36", "2026-08-04", nav=_nav(101.0, 0.0),
            idx_previous_close={"나스닥": 20000.0, "S&P500": 5000.0})
    assert abs(out2["idx"]["나스닥"] - 1.0) < 1e-9, out2["idx"]  # 20000 기준 +1%
    # 마감 행에도 결측이 숫자로 둔갑하지 않는다(스냅샷 직렬화 포함).
    with mock.patch.object(alpha.notify, "send"), \
            mock.patch.object(alpha.notify, "send_photo", return_value=True):
        alpha._close_alert(st, "US", day)
    row = st["days"][-1]
    snap = alpha.dashboard_snapshot({"day": {}, "days": [
        {**row, "indices": {"나스닥": None, "S&P500": 2.0},
         "daily_indices": {"나스닥": None, "S&P500": 2.0}}]})
    srow = snap["days"][0]
    assert srow["indices"]["나스닥"] is None               # float(None) 금지
    assert srow["daily_indices"]["나스닥"] is None
    print("[PASS] 전일종가 결측 지수는 명시적 None — 현재값 0% 세탁 없음")


def test_snapshot_omits_daily_indices_for_legacy_rows_only():
    """Codex V4 P1-2 소비자 계약: 새 스키마 행은 daily_indices 키를 항상 갖고
    (None 포함), 키가 없는 구버전 행만 프런트가 세션 값으로 폴백한다."""
    snap = alpha.dashboard_snapshot({"day": {}, "days": [
        {"d": "2026-08-01", "mkt": "US", "acct": 1.0, "idx": 0.5,
         "indices": {"나스닥": 0.5}},                      # 구버전: daily 없음
        {"d": "2026-08-02", "mkt": "US", "acct": 1.0, "idx": 0.5,
         "indices": {"나스닥": 0.5}, "daily_indices": {"나스닥": None}},
    ]})
    legacy, fresh = snap["days"]
    assert "daily_indices" not in legacy                   # 구버전 표식 보존
    assert fresh["daily_indices"] == {"나스닥": None}      # 명시적 결측 보존
    print("[PASS] 스냅샷이 구버전/명시적-결측 행을 구분해 전파")


def test_close_alert_retry_is_time_based_not_count_based():
    """Codex V4 P3-1: 12회 카운트는 호출 주기에 따라 12분이 될 수 있다.
    5분 간격 재시도 + 최초 실패 후 1시간 경과 시에만 포기해야 한다."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    with mock.patch.object(alpha.notify, "send", return_value=False), \
            mock.patch.object(alpha.notify, "send_photo", return_value=False):
        alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-04",
                             nav=_nav(100.0, 0.0))
        day = st["day"]["US"]
        with mock.patch.object(alpha, "_save"), \
                mock.patch.object(alpha, "_quality_append") as qa:
            t0 = 1_800_000_000.0
            with mock.patch.object(alpha.time, "time", return_value=t0):
                alpha._close_alert(st, "US", day)          # 1차 실패
            assert day["close_alert_pending"] is True
            assert day["close_alert_tries"] == 1
            assert day["close_alert_first_fail_at"] == t0
            # 1분 뒤 호출(빠른 buyloop 주기) — 재시도 간격 미도달, 소모 없음.
            with mock.patch.object(alpha.time, "time", return_value=t0 + 60):
                alpha._deliver_close_alert(st, "US", day)
            assert day["close_alert_tries"] == 1
            # 6분 뒤 — 실제 재시도 1회.
            with mock.patch.object(alpha.time, "time", return_value=t0 + 360):
                alpha._deliver_close_alert(st, "US", day)
            assert day["close_alert_tries"] == 2
            # 1시간 경과 — 포기하되 경보·품질 원장 기록, 본문은 보존.
            with mock.patch.object(alpha.time, "time", return_value=t0 + 3601):
                alpha._deliver_close_alert(st, "US", day)
            assert day["close_alert_pending"] is False
            assert day.get("close_alert_body")             # forensic 보존
            assert any(c.args[0].get("ev") == "close_alert_giveup"
                       for c in qa.call_args_list)
    print("[PASS] 마감 알림 재시도는 시간 기준(5분 간격·1시간 포기)")


def test_close_appends_to_long_term_days_ledger():
    """Codex V4 P2-2: state days[]는 400일 창 — 창 밖으로 밀려도 원본이 남게
    마감마다 append-only 장기 원장에 먼저 영속한다."""
    st, agg, idx = {}, _flat_agg(), {"나스닥": 20000.0}
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = os.path.join(tmp, "days.jsonl")
        with mock.patch.object(alpha, "DAYS_LEDGER_PATH", ledger_path), \
                mock.patch.object(alpha.notify, "send"), \
                mock.patch.object(alpha.notify, "send_photo", return_value=True):
            alpha.session_update(st, "US", agg, idx, "22:31", "2026-08-04",
                                 nav=_nav(100.0, 0.0))
            alpha.session_update(st, "US", agg, idx, "22:36", "2026-08-04",
                                 nav=_nav(101.0, 0.0))
            alpha._close_alert(st, "US", st["day"]["US"])
        import json as _json
        lines = [_json.loads(l) for l in open(ledger_path, encoding="utf-8")]
    assert len(lines) == 1
    assert lines[0]["d"] == "2026-08-04" and lines[0]["mkt"] == "US"
    assert abs(lines[0]["acct"] - 1.0) < 1e-6
    print("[PASS] 마감 행이 append-only 장기 원장에 영속(400일 창 밖 보존)")


def test_vs_line_holds_judgement_when_index_unknown():
    """Codex V4 P1-1 백엔드 대칭: 주 지수 None이면 초과수익을 빼지 않는다."""
    line = alpha._vs_line(1.25, {"나스닥": None})
    assert "판정 보류" in line and "+1.25%p" not in line, line
    assert "🔴" not in line and "🟢" not in line
    print("[PASS] 지수 미확정이면 지수 대비 판정 보류(0% 뺄셈 없음)")


def main():
    test_aggregate()
    test_session_and_flow_neutral()
    test_nav_twr_uses_previous_close_and_removes_trade_flows()
    test_unaccounted_sell_does_not_carve_phantom_cliff()
    test_unaccounted_buy_does_not_fabricate_gain()
    test_pending_close_is_not_written_into_cumulative_days()
    test_observed_cliffs_are_rejected_regardless_of_cause()
    test_normal_moves_still_accumulate()
    test_persistent_anomaly_is_quarantined_not_confirmed_as_zero()
    test_one_sleeve_anomaly_does_not_erase_other_keys()
    test_close_keeps_per_key_values_and_quality_row()
    test_capture_stats_reports_dropped_quality_rows()
    test_none_consumers_do_not_crash_or_show_zero()
    test_dashboard_snapshot_preserves_none_and_quality()
    test_sleeve_quarantine_propagates_to_account()
    test_missing_primary_index_is_none_not_substituted()
    test_capture_stats_shows_dropped_even_below_five_samples()
    test_legacy_series_with_none_does_not_crash_snapshot()
    test_unresolved_day_does_not_leak_into_next_session_basis()
    test_reanchored_partial_session_is_excluded_from_cumulative()
    test_zero_and_nonfinite_values_cannot_wipe_the_account()
    test_session_first_gap_allows_overnight_but_not_absurd()
    test_holdings_equal_weight_uses_starting_positions_only()
    test_capture_stats()
    test_state_roundtrip()
    test_accounting_migration_rebase_is_atomic_and_idempotent()
    test_dashboard_snapshot_is_percentage_only()
    test_missing_previous_close_never_becomes_zero_percent()
    test_snapshot_omits_daily_indices_for_legacy_rows_only()
    test_close_alert_retry_is_time_based_not_count_based()
    test_close_appends_to_long_term_days_ledger()
    test_vs_line_holds_judgement_when_index_unknown()
    print("\n알파 추적 검증 통과 — 집계·세션기준·캡처통계.")


if __name__ == "__main__":
    main()
