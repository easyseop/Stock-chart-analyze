"""매수 루프(Loop B) — autopaper 'now' 신호 KIS 미러 매수 검증(모킹).

브로커-진실: 이미 KIS 보유(3거래소 병합)·잔고 불명·가격 괴리·장외·어닝 D-3·
당일 매도 쿨다운은 execute_entry 호출 전 skip. 게이트 통과분만 execute_entry로,
브로커-진실 캡 입력(open_positions·open_cost_krw)이 사이클 내에서 누적된다.

실행: python -m tests.test_kis_buyloop
"""
from __future__ import annotations

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
         fold=None, pd=None):
    """holdings: {code: qty}|None(None=잔고 조회실패). pd: positions_detail 대체."""
    if exec_ret is None:
        exec_ret = kis_buy.BuyDecision(True, "sent", "ack ODNO=1", qty=3)

    def fake_pd(market="US", excg="NASD"):
        if holdings is None:
            return None
        return _rows_of(holdings, market, excg)

    tf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tf.close()
    lf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); lf.close()
    with mock.patch.object(BL.kis, "positions_detail", side_effect=pd or fake_pd), \
         mock.patch.object(BL.kis, "last_price", return_value=last), \
         mock.patch.object(BL.settings, "market_open", return_value=mkt_open), \
         mock.patch.object(BL.kis_positions, "PATH", tf.name), \
         mock.patch("bot.ledger.LEDGER_PATH", lf.name), \
         mock.patch("bot.ownership.baseline", return_value=set()), \
         mock.patch("bot.ledger._fold", return_value=fold or {}), \
         mock.patch.object(BL.kis_buy, "execute_entry",
                           return_value=exec_ret) as ex:
        res = BL.run_once(signals)
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


def test_inflight_buy_counts_toward_cap():
    import time as _t
    fold = {"kb:y#1": {"symbol": "TSLA", "side": "BUY", "state": "ack",
                       "submitted_at": _t.time(), "intended": 2, "filled": 0}}
    res, ex, rec, _ = _run([_sig()], holdings={}, fold=fold)
    assert ex.call_args.kwargs.get("open_positions") == 1   # in-flight BUY 가산
    print("[PASS] in-flight BUY(접수 후 잔고 미반영)도 포지션 수에 가산")


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


def main():
    test_happy_path_executes()
    test_already_held_skips()
    test_nyse_holding_merged()
    test_holdings_unknown_skips()
    test_price_deviation_skips()
    test_market_closed_skips()
    test_earnings_window_skips()
    test_cooldown_after_same_day_sell()
    test_inflight_buy_counts_toward_cap()
    test_intra_cycle_accumulation()
    test_non_now_filtered()
    test_us_signal_routes_and_fx()
    test_tactic_half_creates_persistent_second_order()
    test_tactic_pullback_uses_limit_without_chasing()
    print("\n매수 루프 검증 통과 — 브로커-진실 미러(중복·괴리·장외·어닝·쿨다운·캡 누적).")


if __name__ == "__main__":
    main()
