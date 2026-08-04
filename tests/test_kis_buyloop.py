"""매수 루프(Loop B) — autopaper 'now' 신호 KIS 미러 매수 검증(모킹).

브로커-진실: 이미 KIS 보유(3거래소 병합)·잔고 불명·가격 괴리·장외·어닝 D-3·
당일 매도 쿨다운은 execute_entry 호출 전 skip. 게이트 통과분만 execute_entry로,
브로커-진실 입력(open_positions·open_cost_krw)이 사이클 내에서 누적된다.
mirror의 동시 보유 수는 제한하지 않고 open_cost 기반 예산 게이트를 사용한다.

실행: python -m tests.test_kis_buyloop
"""
from __future__ import annotations

import datetime
import os
import json
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_buyloop as BL, kis_buy  # noqa: E402


def _sig(code="005930", ccy="KRW", entry=100.0, stop=95.0,
         group="now", fresh=True, **kw):
    return {"group": group, "id": f"s-{code}", "code": code, "name": "t",
            "ccy": ccy, "entry": entry, "stop": stop, "fresh": fresh,
            "stage": 3, "norm": 50, **kw}


def _rows_of(holdings, market, excg):
    """{code: qty} → positions_detail 정규화 행(기본: US 보유는 NASD에만)."""
    rows = []
    for c, q in (holdings or {}).items():
        mk = "KR" if (len(c) == 6 and c[:5].isdigit()) else "US"
        if mk != market or (market == "US" and excg != "NASD"):
            continue
        rows.append({"code": c, "name": c, "qty": q, "avg": 100.0, "cur": 100.0,
                     "eval_amt": 100.0 * q, "buy_amt": 100.0 * q, "pl_amt": 0.0,
                     "pl_rt": 0.0, "ccy": "KRW" if mk == "KR" else "USD",
                     "market": mk})
    return rows


def _run(signals, holdings=None, last=100.0, exec_ret=None, mkt_open=True,
         fold=None, pd=None, run_kwargs=None, mirrored="all"):
    """holdings: {code: qty}|None(None=잔고 조회실패). pd: positions_detail 대체.

    mirrored: autopaper 실제 진입 집합. "all"=신호 전체(기존 테스트 의미 유지),
    None=피드 조회 실패, set=그 종목만 미러 대상.
    """
    if exec_ret is None:
        exec_ret = kis_buy.BuyDecision(True, "sent", "ack ODNO=1", qty=3)
    if mirrored == "all":
        mirrored = {str(s.get("code", "")).upper() for s in signals}

    def fake_pd(market="US", excg="NASD"):
        if holdings is None:
            return None
        return _rows_of(holdings, market, excg)

    tf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tf.close()
    lf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); lf.close()
    with mock.patch.object(BL, "autopaper_entries", return_value=mirrored), \
         mock.patch.object(BL.kis, "positions_detail", side_effect=pd or fake_pd), \
         mock.patch.object(BL.kis, "last_price", return_value=last), \
         mock.patch.object(BL.settings, "market_open", return_value=mkt_open), \
         mock.patch.object(BL.kis_positions, "PATH", tf.name), \
         mock.patch("bot.ledger.LEDGER_PATH", lf.name), \
         mock.patch("bot.ownership.baseline", return_value=set()), \
         mock.patch("bot.ledger._fold", return_value=fold or {}), \
         mock.patch.object(BL.kis_buy, "execute_entry",
                           return_value=exec_ret) as ex:
        res = BL.run_once(signals, **(run_kwargs or {}))
        recorded = BL.kis_positions.load()
        with open(lf.name, encoding="utf-8") as f:
            ledger_events = [json.loads(x) for x in f if x.strip()]
    os.unlink(tf.name); os.unlink(lf.name)
    return res, ex, recorded, ledger_events


def _g(res, code):
    return next((r for r in res if r["code"] == code), None)


def test_happy_path_executes():
    res, ex, rec, _ = _run([_sig()], holdings={})
    r = _g(res, "005930")
    assert r["ok"] and r["gate"] == "sent" and r["qty"] == 3
    assert ex.called
    kw = ex.call_args.kwargs
    assert kw.get("market") == "KR"
    # 브로커-진실 캡 입력 전달: 빈 계좌 → 0/0. ack 대사 기준 hldg_before=0.
    assert kw.get("open_positions") == 0 and kw.get("open_cost_krw") == 0
    assert kw.get("hldg_before") == 0
    # ack는 체결이 아니므로 보호 포지션을 미리 만들지 않는다.
    assert rec == {}
    print("[PASS] now·미보유·가격근접 → 주문 접수, ack 단계 포지션 미기록")


def test_already_held_skips():
    res, ex, rec, _ = _run([_sig()], holdings={"005930": 5})
    assert _g(res, "005930")["gate"] == "already" and not ex.called
    print("[PASS] 이미 KIS 보유 → skip(중복매수 금지)")


def test_nyse_holding_merged():
    """NYSE에만 있는 보유가 병합 조회로 잡혀 중복매수를 막는지(검토 구멍 수정)."""
    def pd(market="US", excg="NASD"):
        if market == "US" and excg == "NYSE":
            return [{"code": "KKR", "name": "KKR", "qty": 2, "avg": 90.0,
                     "cur": 90.0, "eval_amt": 180.0, "buy_amt": 180.0,
                     "pl_amt": 0.0, "pl_rt": 0.0, "ccy": "USD", "market": "US"}]
        return []
    res, ex, rec, _ = _run([_sig(code="KKR", ccy="USD", entry=90.0, stop=85.0)],
                        holdings={}, pd=pd, last=90.0)
    assert _g(res, "KKR")["gate"] == "already" and not ex.called
    print("[PASS] NYSE 보유 병합 → 중복매수 차단(NASD-only 구멍 수정)")


def test_holdings_unknown_skips():
    res, ex, rec, _ = _run([_sig()], holdings=None)          # 잔고 조회 실패
    assert _g(res, "005930")["gate"] == "holdings" and not ex.called
    print("[PASS] 잔고 조회실패 → 보수적 전면 skip")


def test_price_deviation_skips():
    res, ex, rec, _ = _run([_sig(entry=100.0)], holdings={}, last=110.0)  # +10% 이탈
    assert _g(res, "005930")["gate"] == "tolerance" and not ex.called
    print("[PASS] 가격 괴리(진입가 ±1.5% 밖) → skip")


def test_market_closed_skips():
    res, ex, rec, _ = _run([_sig()], holdings={}, mkt_open=False)
    assert _g(res, "005930")["gate"] == "session" and not ex.called
    print("[PASS] 장외 → skip")


def test_earnings_window_skips():
    res, ex, rec, _ = _run([_sig(earnings_d=2)], holdings={})
    assert _g(res, "005930")["gate"] == "earnings" and not ex.called
    res2, ex2, _, _ = _run([_sig(earnings_d=5)], holdings={})
    assert _g(res2, "005930")["ok"]                       # D-5는 통과
    print("[PASS] 어닝 D-3 이내 → skip (autopaper 패리티)")


def test_cooldown_after_same_day_sell():
    import time as _t
    fold = {"x#1": {"symbol": "005930", "side": "SELL", "state": "filled",
                    "submitted_at": _t.time(), "intended": 3, "filled": 3}}
    res, ex, rec, _ = _run([_sig()], holdings={}, fold=fold)
    assert _g(res, "005930")["gate"] == "cooldown" and not ex.called
    print("[PASS] 당일 매도 종목 재진입 금지(쿨다운 — autopaper 패리티)")


def test_inflight_buy_counts_toward_position_accounting():
    import time as _t
    fold = {"kb:y#1": {"symbol": "TSLA", "side": "BUY", "state": "ack",
                       "submitted_at": _t.time(), "intended": 2, "filled": 0}}
    res, ex, rec, _ = _run([_sig()], holdings={}, fold=fold)
    assert ex.call_args.kwargs.get("open_positions") == 1   # in-flight BUY 가산
    print("[PASS] in-flight BUY도 포지션 현황에 가산(하위 Stage·진단용)")


def test_b_sleeve_has_no_fixed_position_count_gate():
    """B 예약이 25종목이어도 buyloop가 고정 개수로 막지 않고 예산 게이트에 위임."""
    import time as _t
    fold = {
        f"sb:planned:{i}": {
            "symbol": f"{i:06d}", "side": "BUY", "state": "planned",
            "created_at": _t.time() + i, "intended": 1, "filled": 0,
            "price": 10.0, "market": "KR", "sleeve": "B",
            "pos_key": f"sb:planned:{i}",
        }
        for i in range(25)
    }
    sig = _sig(code="999999", group="shelf", entry=100.0, stop=95.0)
    res, ex, _, _ = _run(
        [sig], holdings={}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    assert ex.called and _g(res, "999999")["ok"]
    assert ex.call_args.kwargs["open_positions"] == 25
    assert ex.call_args.kwargs["open_cost_krw"] == 250.0
    print("[PASS] B 25종목 예약 상태도 고정 개수 차단 없이 예산 게이트로 전달")


def test_mirror_only_buys_what_autopaper_actually_entered():
    """모듈 선언대로 '미러'가 되는지 — 신호에 있어도 autopaper 미진입이면 안 산다.

    Codex 적대검토 P1: 종전에는 신호 피드의 fresh now 후보를 그대로 매수
    경로에 넘겨, autopaper가 한도(동시 12·하루 3건)로 사지 않은 종목도 KIS가
    샀다. 두 장부의 성과 비교가 성립하지 않는 정책 결함.
    """
    entered, skipped = _sig(code="005930"), _sig(code="000660")
    res, ex, _, _ = _run([entered, skipped], holdings={},
                         mirrored={"005930"})
    assert _g(res, "005930")["gate"] == "sent"
    blocked = _g(res, "000660")
    assert blocked["gate"] == "mirror" and "미진입" in blocked["why"]
    assert ex.call_count == 1                      # 미진입 종목은 주문 0건
    print("[PASS] 미러는 autopaper 실제 진입 종목만 매수")


def test_autopaper_feed_failure_holds_mirror_closed():
    res, ex, _, _ = _run([_sig()], holdings={}, mirrored=None)
    r = _g(res, "005930")
    assert r["gate"] == "mirror" and "무효" in r["why"]
    assert not ex.called                           # 미러 대상 불명 → 매수 0건
    print("[PASS] autopaper 피드 실패는 fail-closed(미러 보류)")


def _feed(positions, generated_at=None, **extra):
    now = datetime.datetime.now(BL._KST)
    return {"generated_at": (generated_at
                             if generated_at is not None else now.isoformat()),
            "positions": positions, **extra}


def test_old_holdings_are_not_backfilled_in_one_day():
    """Codex 미러 P1-1 재현: L1·장애로 쉬는 동안 쌓인 보유를 재개 첫날 몰아 사기.

    autopaper 보유 12종목 중 오늘 진입은 3건뿐인데, 현재 보유 코드 교집합만
    보면 12건이 한꺼번에 나간다. 진입시점·평단·손절계획이 모두 달라진다.
    """
    now = datetime.datetime(2026, 8, 4, 23, 0, tzinfo=BL._KST)
    today, old = "2026-08-04", "2026-07-20"
    rows = ([{"code": f"NEW{i}", "opened": today} for i in range(3)]
            + [{"code": f"OLD{i}", "opened": old} for i in range(9)])
    with mock.patch.object(BL, "_parse_paper_feed",
                           return_value={"age_min": 1.0, "positions": {
                               r["code"]: {"opened": r["opened"]} for r in rows}}), \
         mock.patch.object(BL.urllib.request, "urlopen"), \
         mock.patch.object(BL.json, "load", return_value={}):
        entries = BL.autopaper_entries(now)
    assert entries == {"NEW0", "NEW1", "NEW2"}, entries
    signals = [_sig(code=r["code"]) for r in rows]
    res, ex, _, _ = _run(signals, holdings={}, mirrored=entries)
    bought = {r["code"] for r in res if r.get("gate") == "sent"}
    assert bought == {"NEW0", "NEW1", "NEW2"}, bought
    assert ex.call_count == 3                      # autopaper 하루 3건과 일치
    print("[PASS] 옛 보유 일괄 백필 차단 — 오늘 진입만 미러(하루 상한 일치)")


def test_us_session_across_kst_midnight_keeps_same_session_entries():
    """미 정규장 한 세션은 KST 자정을 넘는다 — 그 세션 진입이 끊기면 안 된다."""
    with mock.patch.object(BL.settings, "market_open", return_value=True):
        past_midnight = datetime.datetime(2026, 8, 5, 3, 0, tzinfo=BL._KST)
        assert BL._mirror_window(past_midnight) == {"2026-08-05", "2026-08-04"}
    with mock.patch.object(BL.settings, "market_open", return_value=False):
        daytime = datetime.datetime(2026, 8, 5, 15, 0, tzinfo=BL._KST)
        assert BL._mirror_window(daytime) == {"2026-08-05"}
    print("[PASS] 자정 넘는 미장 세션은 전날 진입 인정 · 장 밖에서는 오늘만")


def test_stale_or_malformed_feed_is_rejected_not_trusted():
    """Codex 미러 P1-2·P2-2: 성공했지만 낡은/계약 위반 피드가 fail-open이었다."""
    now = datetime.datetime(2026, 8, 4, 23, 0, tzinfo=BL._KST)
    ok_rows = [{"code": "AAPL", "opened": "2026-08-04"}]
    bad_payloads = [
        _feed(ok_rows, generated_at="2025-01-01T00:00:00+09:00"),   # 낡음
        _feed(ok_rows, generated_at="2026-08-04"),                  # tz 없음
        _feed(ok_rows, generated_at=None),                          # 시각 없음
        {"positions": ok_rows},                                     # 시각 필드 부재
        _feed(["AAPL"]),                                            # scalar 행
        _feed([{"opened": "2026-08-04"}]),                          # code 없음
        _feed("AAPL"),                                              # positions 비list
        ["not", "a", "dict"],                                       # 루트 비dict
        _feed(ok_rows, generated_at="2026-08-05T23:00:00+09:00"),   # 미래 시각
    ]
    for payload in bad_payloads:
        assert BL._parse_paper_feed(payload, now=now) is None, payload
    good = BL._parse_paper_feed(_feed(ok_rows), now=datetime.datetime.now(BL._KST))
    assert good and set(good["positions"]) == {"AAPL"}
    print("[PASS] 낡음·tz없음·scalar행·루트오류·미래시각 전부 소스 거부")


def test_mirror_feed_outage_alerts_instead_of_silent_block():
    """새 게이트가 '조용히 5일 막힘' 사고를 재생산하지 않는지."""
    BL._mirror_feed_fail_streak = 0
    BL._mirror_feed_alerted = False
    sent = []
    with mock.patch.object(BL, "_notify_safe",
                           side_effect=lambda t: sent.append(t)):
        for _ in range(BL._MIRROR_FEED_ALERT_AFTER):
            BL._note_mirror_feed(ok=False)
        assert any("연속 무효" in t for t in sent), sent
        before = len(sent)
        BL._note_mirror_feed(ok=False)
        assert len(sent) == before                 # 폭주하지 않음
        BL._note_mirror_feed(ok=True)
        assert any("복구" in t for t in sent), sent
    BL._mirror_feed_fail_streak = 0
    BL._mirror_feed_alerted = False
    print("[PASS] 미러 피드 연속 무효 경보 1회 + 복구 경보")


def test_shelf_sleeve_is_not_gated_by_autopaper():
    """B(매물대)는 autopaper가 다루지 않는 별도 예산 전략 — 이 게이트 미적용."""
    sig = _sig(code="005930")
    sig["group"] = "shelf"
    sig["shelf"] = {"rr": 2.0}
    res, ex, _, _ = _run([sig], holdings={}, mirrored=set(),
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert _g(res, "005930")["gate"] != "mirror"
    assert ex.called                               # autopaper 미보유여도 B는 진행
    print("[PASS] 전략 B는 autopaper 미러 게이트에 막히지 않음")


def test_intra_cycle_accumulation():
    """한 사이클 두 매수 — 두 번째 호출은 첫 매수를 반영한 캡 입력을 받아야."""
    res, ex, rec, _ = _run([_sig(code="005930"), _sig(code="000660")], holdings={})
    assert len(ex.call_args_list) == 2
    k1 = ex.call_args_list[0].kwargs
    k2 = ex.call_args_list[1].kwargs
    assert k1["open_positions"] == 0 and k1["open_cost_krw"] == 0
    assert k2["open_positions"] == 1                       # 첫 매수 즉시 반영
    assert k2["open_cost_krw"] == 3 * 100.0                # qty3 × last100 (KR fx=1)
    print("[PASS] 사이클 내 누적 — 같은 스냅샷 연속매수로 SEED 초과하는 구멍 차단")


def test_non_now_filtered():
    res, _, rec, _ = _run([_sig(group="watch"), _sig(code="000660", fresh=False)],
                       holdings={})
    assert res == []                                 # 후보 아님
    print("[PASS] now 아님·미신선 → 후보 제외")


def test_us_signal_routes_and_fx():
    ex_ret = kis_buy.BuyDecision(True, "sent", "ack", qty=1)
    res, ex, rec, _ = _run([_sig(code="AAPL", ccy="USD", entry=190.0, stop=185.0)],
                        holdings={}, last=190.5, exec_ret=ex_ret)
    assert _g(res, "AAPL")["ok"]
    assert ex.call_args.kwargs.get("market") == "US"
    assert ex.call_args.kwargs.get("krw_per_usd") > 0   # fx 전달
    print("[PASS] 미국 신호 → market=US·fx 전달")


def test_tactic_half_creates_persistent_second_order():
    d = kis_buy.BuyDecision(True, "sent", "ack", qty=5, planned_qty=10)
    sig = _sig(entry=100.0, stop=93.0,
               tactic={"mode": "half", "pb_price": 97.0})
    res, ex, rec, events = _run([sig], holdings={}, exec_ret=d)
    assert ex.call_args.kwargs["qty_fraction"] == 0.5
    plans = [e for e in events if e.get("ev") == "plan"]
    assert len(plans) == 1 and plans[0]["intended"] == 5
    assert plans[0]["meta"]["limit"] == 97.0 and plans[0]["meta"]["pending"]
    assert rec == {}
    print("[PASS] tactic=half: 1차 절반 + 원장에 2차 눌림 5주 영속")


def test_tactic_pullback_uses_limit_without_chasing():
    d = kis_buy.BuyDecision(True, "sent", "ack", qty=7, planned_qty=7)
    sig = _sig(entry=100.0, stop=85.0,
               tactic={"mode": "pullback", "pb_price": 90.0})
    res, ex, _, _ = _run([sig], holdings={}, last=120.0, exec_ret=d)
    kw = ex.call_args.kwargs
    assert kw["price_usd"] == 90.0 and kw["limit_price"] == 90.0
    assert kw["order_meta"]["pending"] is True
    assert _g(res, "005930")["tactic"] == "pullback"
    print("[PASS] tactic=pullback: 현재가 추격 없이 눌림가 전량 지정가")


def test_b_sleeve_survives_balance_before_position_reconcile():
    """B ACK가 잔고에 먼저 보이면 kis_positions 전에도 B 원가/개수로 남아야."""
    import time as _t
    fold = {"sb:alk": {
        "symbol": "005930", "side": "BUY", "state": "ack",
        "submitted_at": _t.time(), "intended": 8, "filled": 0,
        "hldg_before": 0, "price": 100.0, "market": "KR",
        "sleeve": "B", "pos_key": "sb:alk"}}
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 8}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1
    assert kw["open_cost_krw"] == 800.0
    assert kw["total_open_cost_krw"] == 800.0
    print("[PASS] B ACK→잔고 선반영→kpos 지연에도 B 귀속·예산 유지")


def test_unfilled_b_plan_does_not_retag_existing_a_holding():
    """미제출 B 계획만으로 기존 A 보유가 B 원가·포지션으로 이동하지 않는다."""
    import time as _t
    fold = {"sb:planned": {
        "symbol": "005930", "side": "BUY", "state": "planned",
        "created_at": _t.time(), "intended": 2, "filled": 0,
        "price": 90.0, "market": "KR", "sleeve": "B",
        "pos_key": "sb:planned"}}
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 5}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1   # B 계획 자체는 B의 열린 예약 포지션
    assert kw["open_cost_krw"] == 2 * 90
    assert kw["total_open_cost_krw"] == 5 * 100 + 2 * 90
    print("[PASS] 미체결 B 계획은 기존 A 보유를 재태깅하지 않고 예약만 B에 반영")


def test_partial_and_multiple_same_symbol_reservations_are_summed():
    """보유 6 + 1차잔량4 + 눌림계획5를 같은 B 포지션 1개·원가 전부로 계산."""
    import time as _t
    fold = {
        "sb:alk": {
            "symbol": "005930", "side": "BUY", "state": "partial",
            "submitted_at": _t.time(), "intended": 10, "filled": 0,
            "hldg_before": 0, "price": 100.0, "market": "KR",
            "sleeve": "B", "pos_key": "sb:alk", "open": True},
        "sb:alk:pb": {
            "symbol": "005930", "side": "BUY", "state": "planned",
            "created_at": _t.time() + 1, "intended": 5, "filled": 0,
            "price": 90.0, "market": "KR", "sleeve": "B",
            "pos_key": "sb:alk"},
    }
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 6}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1
    assert kw["open_cost_krw"] == 600 + 4 * 100 + 5 * 90
    assert kw["total_open_cost_krw"] == 1450
    print("[PASS] 동종목 부분잔량·계획 예약 누적, dict 덮어쓰기 제거")


def main():
    test_happy_path_executes()
    test_already_held_skips()
    test_nyse_holding_merged()
    test_holdings_unknown_skips()
    test_price_deviation_skips()
    test_market_closed_skips()
    test_earnings_window_skips()
    test_cooldown_after_same_day_sell()
    test_inflight_buy_counts_toward_position_accounting()
    test_b_sleeve_has_no_fixed_position_count_gate()
    test_mirror_only_buys_what_autopaper_actually_entered()
    test_autopaper_feed_failure_holds_mirror_closed()
    test_old_holdings_are_not_backfilled_in_one_day()
    test_us_session_across_kst_midnight_keeps_same_session_entries()
    test_stale_or_malformed_feed_is_rejected_not_trusted()
    test_mirror_feed_outage_alerts_instead_of_silent_block()
    test_shelf_sleeve_is_not_gated_by_autopaper()
    test_intra_cycle_accumulation()
    test_non_now_filtered()
    test_us_signal_routes_and_fx()
    test_tactic_half_creates_persistent_second_order()
    test_tactic_pullback_uses_limit_without_chasing()
    test_b_sleeve_survives_balance_before_position_reconcile()
    test_unfilled_b_plan_does_not_retag_existing_a_holding()
    test_partial_and_multiple_same_symbol_reservations_are_summed()
    print("\n매수 루프 검증 통과 — 브로커-진실 미러(고정 종목수 무제한·예산 누적).")


if __name__ == "__main__":
    main()
