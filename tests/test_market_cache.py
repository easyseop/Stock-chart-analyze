"""파수꾼 공유 시세 캐시 — KIS 추가 호출 없는 준실시간 웹 배선 검증."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from unittest import mock

from bot import market_cache


def _row(code: str, market: str, cur: float, sleeve: str = "A") -> dict:
    qty, avg = 2, cur - 10
    return {
        "code": code, "name": code, "market": market,
        "ccy": "KRW" if market == "KR" else "USD",
        "qty": qty, "avg": avg, "cur": cur,
        "eval_amt": cur * qty, "buy_amt": avg * qty,
        "pl_amt": (cur - avg) * qty, "pl_rt": (cur / avg - 1) * 100,
        "entry": avg, "stop": avg - 5, "target": cur + 20,
        "sleeve": sleeve, "opened": "2026-07-21",
    }


def test_complete_cache_and_quote_refresh():
    with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"KIS_MARKET_CACHE_PATH": os.path.join(td, "market.json")}):
        market_cache.update_market("KR", [_row("005930", "KR", 70000)], now=1000)
        assert market_cache.portfolio(now=1001) is None   # US 완전 스냅샷 전
        market_cache.update_market("US", [_row("AAPL", "US", 200, "B")], now=1002)
        snap = market_cache.portfolio(
            now=1003, market_open=lambda market: market == "US")
        assert snap is not None and len(snap["positions"]) == 2
        assert snap["source"] == "sentinel_shared_cache"

        assert market_cache.update_quote("AAPL", 205, now=1004)
        snap = market_cache.portfolio(
            now=1005, market_open=lambda market: market == "US")
        apple = next(row for row in snap["positions"] if row["code"] == "AAPL")
        assert apple["cur"] == 205
        assert apple["opened"] == "2026-07-21"
        assert apple["eval_amt"] == 410
        assert apple["pl_amt"] == 30
        history = market_cache.quote_history("AAPL")
        assert history["points"][-1] == {"ts": 1004.0, "price": 205.0}
        mode = stat.S_IMODE(os.stat(market_cache.path()).st_mode)
        assert mode == 0o600
        encoded = json.dumps(market_cache.read())
        assert "APPSECRET" not in encoded and "CANO" not in encoded


def test_open_market_stale_cache_is_rejected():
    with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"KIS_MARKET_CACHE_PATH": os.path.join(td, "market.json")}):
        market_cache.update_market("KR", [], now=1000)
        market_cache.update_market("US", [_row("NVDA", "US", 170)], now=1000)
        assert market_cache.portfolio(
            now=1100, open_max_age=90,
            market_open=lambda market: market == "US") is None
        assert market_cache.positions_for_market("US", now=1100, max_age=90) is None


def test_sentinel_reuses_its_balance_response_for_web():
    from bot import sentinel
    broker = object.__new__(sentinel._KisBroker)
    us = _row("AAPL", "US", 200, "B")
    calls = []

    def positions(market, excg="NASD"):
        if market == "KR":
            return []
        return [us] if excg == "NASD" else []

    with mock.patch("bot.settings.market_open", return_value=True), \
            mock.patch("bot.kis.positions_detail", side_effect=positions) as query, \
            mock.patch("bot.kis_positions.load",
                       return_value={"AAPL": {"sleeve": "B", "stop": 180,
                                               "opened": "2026-07-21"}}), \
            mock.patch("bot.market_cache.update_market",
                       side_effect=lambda market, rows: calls.append((market, rows))), \
            mock.patch("bot.market_cache.update_quote") as update_quote, \
            mock.patch("bot.kis.last_price") as last_price:
        held = broker.holdings()
        quote = broker.quote("AAPL", "USD")
    assert held == {"AAPL": 2}
    assert query.call_count == 4                 # 기존 KR+미 3거래소와 같은 호출 수
    assert quote == 200 and not last_price.called  # 잔고 현재가 재사용 — 종목별 호출 0
    update_quote.assert_called_once_with("AAPL", 200.0)
    assert {market for market, _rows in calls} == {"KR", "US"}
    apple = next(row for market, rows in calls if market == "US"
                 for row in rows if row["code"] == "AAPL")
    assert apple["sleeve"] == "B" and apple["stop"] == 180
    assert apple["opened"] == "2026-07-21"

    broker._balance_quotes = {"STALE": 999.0}
    with mock.patch("bot.settings.market_open", return_value=True), \
            mock.patch("bot.kis.positions_detail", return_value=None):
        assert broker.holdings() is None
    assert broker._balance_quotes == {}          # 실패 사이클은 직전 가격 재사용 금지
    with mock.patch("bot.kis.last_price", return_value=123.0) as fallback, \
            mock.patch("bot.market_cache.update_quote"):
        assert broker.quote("STALE", "USD") == 123.0
    fallback.assert_called_once()


def main():
    test_complete_cache_and_quote_refresh()
    print("[PASS] 완전 캐시·현재가/손익 갱신·0600")
    test_open_market_stale_cache_is_rejected()
    print("[PASS] 열린 시장 낡은 캐시 거부")
    test_sentinel_reuses_its_balance_response_for_web()
    print("[PASS] 파수꾼 잔고 응답 공유 — KIS 추가 호출 0")
    print("\n파수꾼 공유 시세 캐시 검증 통과.")


if __name__ == "__main__":
    main()
