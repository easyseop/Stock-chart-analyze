"""공개 승률 요약 검증 — 금액·수량·종목 비공개, 원장 불신이면 침묵.

  1) 매도 확정 행 → 승/패·승률·수익률 통계(금액 필드 0)
  2) 원장 불신(available=False) → None(지어내지 않음)
  3) 전략별·월별 분해가 실제 행과 일치
  4) 발행 payload에 금액·수량·종목·시크릿이 없다

실행: python -m tests.test_trade_stats
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import trade_stats as T   # noqa: E402


def _snap(rows, available=True, partial=False):
    return {"available": available, "partial": partial,
            "generated_at": "2026-08-15T00:00:00+00:00", "trades": rows}


_ROWS = [
    {"side": "sell", "sleeve": "A", "ts": "2026-08-10T12:00:00+00:00",
     "realized_pnl_krw": 12000.0, "return_pct": 3.0,
     "code": "AAPL", "name": "Apple", "qty": 10, "price": 220.5},
    {"side": "sell", "sleeve": "A", "ts": "2026-08-11T12:00:00+00:00",
     "realized_pnl_krw": -5000.0, "return_pct": -1.0,
     "code": "TAP", "name": "Molson", "qty": 80, "price": 41.2},
    {"side": "sell", "sleeve": "B", "ts": "2026-08-11T13:00:00+00:00",
     "realized_pnl_krw": 3000.0, "return_pct": 1.0,
     "code": "PAAS", "name": "Pan Am", "qty": 5, "price": 30.0},
    {"side": "buy", "sleeve": "A", "ts": "2026-08-11T14:00:00+00:00",
     "realized_pnl_krw": None, "return_pct": None, "code": "BX"},
]


def test_summary_counts_and_rates():
    with mock.patch("bot.trade_history.snapshot", lambda limit=500: _snap(_ROWS)):
        out = T.summary()
    total = out["total"]
    assert total["closed"] == 3 and total["decided"] == 3      # 매수 행 제외
    assert total["wins"] == 2 and total["losses"] == 1
    assert total["win_rate"] == 66.67, total
    assert total["avg_return_pct"] == 1.0                      # (3-1+1)/3
    assert total["avg_win_pct"] == 2.0 and total["avg_loss_pct"] == -1.0
    print("[PASS] 승/패·승률·평균 수익률 집계(매수 행 제외)")


def test_sleeve_and_month_breakdown():
    with mock.patch("bot.trade_history.snapshot", lambda limit=500: _snap(_ROWS)):
        out = T.summary()
    assert out["by_sleeve"]["A"]["wins"] == 1
    assert out["by_sleeve"]["A"]["losses"] == 1
    assert out["by_sleeve"]["B"]["wins"] == 1
    assert set(out["by_month"]) == {"2026-08"}
    assert out["by_month"]["2026-08"]["closed"] == 3
    print("[PASS] 전략(A/B)·월별 분해")


def test_untrusted_ledger_returns_none():
    with mock.patch("bot.trade_history.snapshot",
                    lambda limit=500: _snap([], available=False)):
        assert T.summary() is None
    def boom(limit=500):
        raise RuntimeError("ledger corrupt")
    with mock.patch("bot.trade_history.snapshot", boom):
        assert T.summary() is None
    print("[PASS] 원장 불신·예외 → None(수치 발명 금지)")


def test_payload_has_no_money_or_symbols():
    with mock.patch("bot.trade_history.snapshot", lambda limit=500: _snap(_ROWS)):
        out = T.summary()
    raw = json.dumps(out, ensure_ascii=False)
    for banned in ("AAPL", "TAP", "PAAS", "Apple", "Molson",
                   "realized_pnl", "krw", "qty", "price", "220.5"):
        assert banned not in raw, banned
    # 발행 경로도 같은 payload를 그대로 보낸다(네트워크는 모의).
    sent = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        sent["body"] = req.data.decode(); sent["url"] = req.full_url
        sent["title"] = req.headers.get("Title")
        return _Resp()
    with mock.patch("bot.trade_history.snapshot", lambda limit=500: _snap(_ROWS)), \
            mock.patch.object(T.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.dict(os.environ, {"NTFY_TRADE_STATS_TOPIC": "t-test",
                                         "KIS_MOCK_APPKEY": "SECRET777"}):
        assert T.publish() is True
    assert sent["title"] == "trade-stats" and "t-test" in sent["url"]
    assert "SECRET777" not in sent["body"] and "AAPL" not in sent["body"]
    print("[PASS] 금액·수량·종목·시크릿 0 (요약·발행 양쪽)")


def main():
    test_summary_counts_and_rates()
    test_sleeve_and_month_breakdown()
    test_untrusted_ledger_returns_none()
    test_payload_has_no_money_or_symbols()
    print("\n공개 승률 요약 검증 통과 — 비율·건수만, 원장 불신이면 침묵.")


if __name__ == "__main__":
    main()
