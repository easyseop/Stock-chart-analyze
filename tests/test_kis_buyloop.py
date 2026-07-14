"""매수 루프(Loop B) — autopaper 'now' 신호 KIS 미러 매수 검증(모킹).

브로커-진실: 이미 KIS 보유·잔고 불명·가격 괴리·장외는 execute_entry 호출 전 skip.
게이트 통과분만 execute_entry로 넘어간다.

실행: python -m tests.test_kis_buyloop
"""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_buyloop as BL, kis_buy  # noqa: E402


def _sig(code="005930", ccy="KRW", entry=100.0, stop=95.0,
         group="now", fresh=True, **kw):
    return {"group": group, "id": f"s-{code}", "code": code, "name": "t",
            "ccy": ccy, "entry": entry, "stop": stop, "fresh": fresh,
            "stage": 3, "norm": 50, **kw}


import tempfile


def _run(signals, holdings=None, last=100.0, exec_ret=None, mkt_open=True):
    if exec_ret is None:
        exec_ret = kis_buy.BuyDecision(True, "sent", "ack ODNO=1", qty=3)
    tf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tf.close()
    with mock.patch.object(BL.kis, "holdings", return_value=holdings), \
         mock.patch.object(BL.kis, "last_price", return_value=last), \
         mock.patch.object(BL.settings, "market_open", return_value=mkt_open), \
         mock.patch.object(BL.kis_positions, "PATH", tf.name), \
         mock.patch.object(BL.kis_buy, "execute_entry",
                           return_value=exec_ret) as ex:
        res = BL.run_once(signals)
        recorded = BL.kis_positions.load()
    os.unlink(tf.name)
    return res, ex, recorded


def _g(res, code):
    return next((r for r in res if r["code"] == code), None)


def test_happy_path_executes():
    res, ex, rec = _run([_sig()], holdings={})
    r = _g(res, "005930")
    assert r["ok"] and r["gate"] == "sent" and r["qty"] == 3
    assert ex.called
    # KR 신호 → market=KR로 execute_entry 호출
    assert ex.call_args.kwargs.get("market") == "KR"
    # 성공 진입 → 파수꾼 보호용 손절선 기록됨(브로커-진실 fallback)
    assert rec.get("005930", {}).get("stop") == 95.0
    print("[PASS] now·미보유·가격근접 → execute_entry 전송 + 손절선 기록")


def test_already_held_skips():
    res, ex, rec = _run([_sig()], holdings={"005930": 5})
    assert _g(res, "005930")["gate"] == "already" and not ex.called
    print("[PASS] 이미 KIS 보유 → skip(중복매수 금지)")


def test_holdings_unknown_skips():
    res, ex, rec = _run([_sig()], holdings=None)          # 잔고 조회 실패
    assert _g(res, "005930")["gate"] == "holdings" and not ex.called
    print("[PASS] 잔고 조회실패 → 보수적 skip")


def test_price_deviation_skips():
    res, ex, rec = _run([_sig(entry=100.0)], holdings={}, last=110.0)  # +10% 이탈
    assert _g(res, "005930")["gate"] == "tolerance" and not ex.called
    print("[PASS] 가격 괴리(진입가 ±1.5% 밖) → skip")


def test_market_closed_skips():
    res, ex, rec = _run([_sig()], holdings={}, mkt_open=False)
    assert _g(res, "005930")["gate"] == "session" and not ex.called
    print("[PASS] 장외 → skip")


def test_non_now_filtered():
    res, _, rec = _run([_sig(group="watch"), _sig(code="000660", fresh=False)],
                  holdings={})
    assert res == []                                 # 후보 아님
    print("[PASS] now 아님·미신선 → 후보 제외")


def test_us_signal_routes_and_fx():
    ex_ret = kis_buy.BuyDecision(True, "sent", "ack", qty=1)
    res, ex, rec = _run([_sig(code="AAPL", ccy="USD", entry=190.0, stop=185.0)],
                   holdings={}, last=190.5, exec_ret=ex_ret)
    assert _g(res, "AAPL")["ok"]
    assert ex.call_args.kwargs.get("market") == "US"
    assert ex.call_args.kwargs.get("krw_per_usd") > 0   # fx 전달
    print("[PASS] 미국 신호 → market=US·fx 전달")


def main():
    test_happy_path_executes()
    test_already_held_skips()
    test_holdings_unknown_skips()
    test_price_deviation_skips()
    test_market_closed_skips()
    test_non_now_filtered()
    test_us_signal_routes_and_fx()
    print("\n매수 루프 검증 통과 — 브로커-진실 미러 매수(중복·괴리·장외 방어).")


if __name__ == "__main__":
    main()
