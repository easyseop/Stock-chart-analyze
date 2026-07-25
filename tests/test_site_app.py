"""공개 정적 웹앱·로컬 보유자산 API의 안전 경계 검증."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock

from bot import portfolio_web
from scanner import siteapp


def test_static_publish_is_additive():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "api").mkdir()
        api = root / "api" / "signals.json"
        index = root / "index.html"
        api.write_text('{"contract":"keep"}', encoding="utf-8")
        index.write_text("legacy-site", encoding="utf-8")

        target = Path(siteapp.publish(td))

        assert target == root / "app"
        assert (target / "index.html").is_file()
        assert (target / "app.css").is_file()
        assert (target / "portfolio_math.js").is_file()
        assert (target / "app.js").is_file()
        assert (target / "og-v2.png").is_file()
        assert api.read_text(encoding="utf-8") == '{"contract":"keep"}'
        assert index.read_text(encoding="utf-8") == "legacy-site"


def test_public_app_uses_only_allowed_read_sources():
    js = (siteapp.SOURCE_DIR / "app.js").read_text(encoding="utf-8")
    assert "../api/signals.json" in js
    assert "../api/paper_auto.json" in js
    assert "../api/track.json" in js
    assert "../api/portfolio.json" in js
    assert "../api/quotes.json" in js
    assert "../api/performance.json" in js
    for forbidden in ("KIS_LIVE_APPKEY", "APPSECRET", "/order", "place_buy",
                      "place_sell", "client_secret"):
        assert forbidden not in js


def test_decision_brief_uses_existing_read_only_data():
    html = (siteapp.SOURCE_DIR / "index.html").read_text(encoding="utf-8")
    js = (siteapp.SOURCE_DIR / "app.js").read_text(encoding="utf-8")
    math_js = (siteapp.SOURCE_DIR / "portfolio_math.js").read_text(encoding="utf-8")
    css = (siteapp.SOURCE_DIR / "app.css").read_text(encoding="utf-8")

    assert 'data-view="briefing"' in html
    assert "오늘 브리핑" in html
    assert '<script src="portfolio_math.js" defer></script>' in html
    for feature in ("positionAttention", "positionPlanMarkup",
                    "performanceInsights", "renderBriefing"):
        assert f"function {feature}" in js
    for feature in ("positionDistances", "strategyStats",
                    "concentrationRows", "maximumDrawdown"):
        assert f"function {feature}" in math_js
    assert "손절선 또는 목표선 3% 안" in js
    assert "보호선 정보 없음" in js
    assert "남은 손익비" in js
    assert "초 동안 갱신되지 않았습니다" in js
    assert "통화가 다른 자산은 억지로 합산하지 않습니다." in js
    assert ".attention-card" in css and ".insight-grid" in css
    assert "/api/risk" not in js


def test_portfolio_snapshot_deduplicates_exchange_queries():
    kr = [{
        "code": "005930", "name": "삼성전자", "market": "KR", "ccy": "KRW",
        "qty": 2, "avg": 70000, "cur": 72000, "eval_amt": 144000,
        "buy_amt": 140000, "pl_amt": 4000, "pl_rt": 2.85,
    }]
    us = [{
        "code": "AAPL", "name": "Apple", "market": "US", "ccy": "USD",
        "qty": 1, "avg": 190, "cur": 200, "eval_amt": 200,
        "buy_amt": 190, "pl_amt": 10, "pl_rt": 5.26,
    }]

    def positions(market, excg="NASD"):
        if market == "KR":
            return kr
        return us if excg in ("NASD", "NYSE") else []

    with tempfile.TemporaryDirectory() as td, \
            mock.patch.dict("os.environ", {
                "KIS_MARKET_CACHE_PATH": str(Path(td) / "market.json")}), \
            mock.patch("bot.kis.positions_detail", side_effect=positions), \
            mock.patch.object(portfolio_web, "_position_meta",
                              return_value={"entry": 0.0, "stop": 0.0, "sleeve": "A"}):
        payload = portfolio_web.portfolio_snapshot()

    assert payload["read_only"] is True
    assert 5 <= payload["refresh_seconds"] <= 300
    assert {row["code"] for row in payload["positions"]} == {"005930", "AAPL"}
    assert len(payload["positions"]) == 2
    encoded = json.dumps(payload)
    assert "APPSECRET" not in encoded and "CANO" not in encoded


def test_chart_is_display_only_existing_ohlcv_series():
    import pandas as pd
    frame = pd.DataFrame(
        {"Open": [9, 10], "High": [11, 12], "Low": [8, 9],
         "Close": [10.0, 11.5], "Volume": [100, 120]},
        index=pd.to_datetime(["2026-07-23", "2026-07-24"]))
    with mock.patch("scanner.cache.frames", return_value={"D": frame}):
        payload = portfolio_web.chart_snapshot("AAPL", 180)
    assert payload["source"] == "existing-scanner-cache"
    assert payload["points"] == [
        {"date": "2026-07-23", "open": 9.0, "high": 11.0, "low": 8.0,
         "close": 10.0, "volume": 100, "ma20": None, "ma60": None,
         "ma120": None},
        {"date": "2026-07-24", "open": 10.0, "high": 12.0, "low": 9.0,
         "close": 11.5, "volume": 120, "ma20": None, "ma60": None,
         "ma120": None},
    ]
    assert payload["interval"] == "1d" and payload["read_only"] is True


def test_oracle_service_is_loopback_read_only_dashboard():
    unit = (Path(__file__).parents[1] / "infra" / "server" /
            "portfolio-web.service").read_text(encoding="utf-8")
    server = Path(portfolio_web.__file__).read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/stock/kis.env" in unit
    assert "KIS_TOKEN_CACHE=/opt/stock/kis_token.json" in unit
    assert "python3 -m bot.portfolio_web --port 8888" in unit
    assert "PORTFOLIO_REFRESH_SECONDS=60" in unit
    assert "NoNewPrivileges=true" in unit
    assert 'ThreadingHTTPServer(("127.0.0.1"' in server
    assert '"portfolio_math.js"' in server
    for forbidden in ("bot.kis_orders", "bot.kis_buyloop", "bot.sentinel"):
        assert forbidden not in server


def test_portfolio_snapshot_cache_avoids_kis_poll_bursts():
    with mock.patch("bot.market_cache.portfolio", return_value=None), \
            mock.patch.object(portfolio_web, "portfolio_snapshot") as snapshot:
        payload = portfolio_web.cached_portfolio_snapshot()
    assert payload["source"] == "shared_cache_unavailable"
    assert payload["positions"] == []
    snapshot.assert_not_called()


def test_shared_cache_serves_fast_without_kis_balance_calls():
    shared = {
        "generated_at": "2026-07-24T00:00:00+00:00",
        "positions": [], "partial": False, "failed_markets": [],
        "read_only": True, "source": "sentinel_shared_cache",
        "price_age_seconds": 3.2,
    }
    with mock.patch("bot.market_cache.portfolio", return_value=shared), \
            mock.patch.object(portfolio_web, "portfolio_snapshot") as direct:
        payload = portfolio_web.cached_portfolio_snapshot()
    assert payload["source"] == "sentinel_shared_cache"
    assert payload["refresh_seconds"] == portfolio_web.PORTFOLIO_BROWSER_REFRESH_SECONDS
    direct.assert_not_called()


def test_performance_snapshot_contains_percentages_only():
    fixture = {
        "version": 2, "generated_at": "2026-07-24T00:00:00+00:00",
        "sample_seconds": 300, "markets": {}, "days": [],
        "basis": "KIS 봇 보유 평가손익 기준",
    }
    with mock.patch("bot.alpha.dashboard_snapshot", return_value=fixture):
        payload = portfolio_web.performance_snapshot()
    encoded = json.dumps(payload)
    assert payload["read_only"] is True
    for forbidden in ("CANO", "APPSECRET", "qty", "eval_amt", "buy_amt"):
        assert forbidden not in encoded


if __name__ == "__main__":
    tests = [
        test_static_publish_is_additive,
        test_public_app_uses_only_allowed_read_sources,
        test_decision_brief_uses_existing_read_only_data,
        test_portfolio_snapshot_deduplicates_exchange_queries,
        test_chart_is_display_only_existing_ohlcv_series,
        test_oracle_service_is_loopback_read_only_dashboard,
        test_portfolio_snapshot_cache_avoids_kis_poll_bursts,
        test_shared_cache_serves_fast_without_kis_balance_calls,
        test_performance_snapshot_contains_percentages_only,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("\n공개/개인 대시보드 안전 경계 검증 통과.")
