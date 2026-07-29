"""수익 매도 사후추적 계산·품질 격리·원자 발행 검증."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock

from bot import post_exit


def _sale(**changes) -> dict:
    row = {
        "side": "sell",
        "executed_at": "2026-07-29T08:38:32+09:00",
        "code": "AAPL",
        "name": "Apple",
        "market": "US",
        "ccy": "USD",
        "sleeve": "A",
        "reason": "익절 +1R 절반",
        "reason_kind": "take_profit",
        "qty": 5,
        "entry_price": 100,
        "exit_price": 110,
        "return_pct": 10,
        "partial_exit": True,
        "fill_price_source": "broker",
        "verified": True,
    }
    row.update(changes)
    return row


def _bars():
    # 체결 KST 07-29 08:38 = 뉴욕 07-28 19:38. 07-29부터 사후 1일.
    return [
        {"date": "2026-07-28", "high": 115, "low": 95, "close": 108},
        {"date": "2026-07-29", "high": 112, "low": 108, "close": 111},
        {"date": "2026-07-30", "high": 120, "low": 109, "close": 117},
        {"date": "2026-07-31", "high": 118, "low": 112, "close": 115},
        {"date": "2026-08-03", "high": 125, "low": 114, "close": 122},
        {"date": "2026-08-04", "high": 123, "low": 116, "close": 121},
    ]


def test_horizons_use_exchange_session_and_show_three_denominators():
    payload = post_exit.build_snapshot(
        [_sale()], {"AAPL": _bars()}, generated_at="2026-08-04T22:00:00+00:00")
    event = payload["events"][0]
    one = event["observations"]["1"]
    five = event["observations"]["5"]

    assert event["session_day"] == "2026-07-28"
    assert one["through_date"] == "2026-07-29"
    assert one["peak_vs_entry_pct"] == 12.0
    assert one["additional_entry_points_after_exit"] == 2.0
    assert round(one["missed_upside_vs_exit_pct"], 4) == 1.8182
    assert five["peak_price"] == 125.0
    assert five["peak_vs_entry_pct"] == 25.0
    assert five["additional_entry_points_after_exit"] == 15.0
    assert round(five["missed_upside_vs_exit_pct"], 4) == 13.6364
    assert five["close_vs_exit_pct"] == 10.0
    assert five["max_drawdown_vs_exit_pct"] == -1.8182
    assert five["complete"] is True
    assert event["observations"]["10"]["observed_sessions"] == 5
    assert event["observations"]["10"]["complete"] is False
    print("[PASS] 거래소 날짜·1/3/5/10/20일·평단/추가%p/매도가 분모 정확")


def test_estimated_prices_are_visible_but_excluded_from_common_traits():
    trades = [
        _sale(code="AAPL"),
        _sale(code="MSFT", name="Microsoft"),
        _sale(code="NVDA", name="NVIDIA"),
        _sale(
            code="OLD", name="Legacy", verified=False,
            fill_price_source="submitted-fallback"),
    ]
    bars = {row["code"]: _bars() for row in trades}
    payload = post_exit.build_snapshot(trades, bars)

    assert payload["summary"]["profitable_exits"] == 4
    assert payload["summary"]["verified_exits"] == 3
    assert payload["summary"]["estimated_exits"] == 1
    assert payload["summary"]["complete_5d_verified"] == 3
    sleeve = next(row for row in payload["traits"]["5"]
                  if row["key"] == "sleeve:A")
    assert sleeve["sample"] == 3 and sleeve["conclusion_ready"] is True
    assert all(row["sample"] == 3 for row in payload["traits"]["5"])
    estimated = next(row for row in payload["events"] if row["code"] == "OLD")
    assert estimated["quality"] == "estimated"
    print("[PASS] 구버전 추정가는 개별 참고만·확정가 공통점 통계와 격리")


def test_losses_invalid_rows_and_exit_session_high_are_not_counted():
    trades = [
        _sale(code="LOSS", exit_price=90, return_pct=-10),
        _sale(code="BAD", executed_at="not-a-time"),
        _sale(code="GOOD"),
    ]
    payload = post_exit.build_snapshot(trades, {"GOOD": _bars()})
    assert [row["code"] for row in payload["events"]] == ["GOOD"]
    assert payload["events"][0]["observations"]["1"]["peak_price"] == 112
    assert payload["events"][0]["observations"]["1"]["peak_price"] != 115
    print("[PASS] 손실·무효체결 제외, 익절 당일 고가는 사후상승으로 오인하지 않음")


def test_same_symbol_multiple_profit_exits_stay_separate():
    trades = [
        _sale(executed_at="2026-07-29T08:38:32+09:00", qty=5),
        _sale(executed_at="2026-07-30T08:38:32+09:00", qty=3, exit_price=112),
    ]
    payload = post_exit.build_snapshot(trades, {"AAPL": _bars()})
    assert len(payload["events"]) == 2
    assert len({row["id"] for row in payload["events"]}) == 2
    assert [row["qty"] for row in payload["events"]] == [3, 5]
    print("[PASS] 같은 종목의 서로 다른 익절은 덮어쓰지 않고 별도 추적")


def test_snapshot_loader_is_cache_only_and_published_file_fails_closed():
    history = {
        "available": True, "partial": False, "trades": [_sale()],
    }
    loader = mock.Mock(return_value=_bars())
    payload = post_exit.snapshot(history=history, loader=loader)
    loader.assert_called_once_with("AAPL")
    assert payload["read_only"] is True and payload["events"]

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "snapshot.json"
        assert post_exit.read_published(str(path))["available"] is False
        path.write_text('{"version":1,"read_only":true,"events":"bad"}',
                        encoding="utf-8")
        assert post_exit.read_published(str(path))["available"] is False
        post_exit._atomic_write(payload, str(path))
        loaded = post_exit.read_published(str(path))
        assert loaded["events"][0]["code"] == "AAPL"
        assert path.stat().st_mode & 0o777 == 0o640
    print("[PASS] HTTP 계산은 캐시만·미발행/손상 fail-closed·원자 0640 발행")


def test_order_plane_and_secrets_are_absent():
    source = Path(post_exit.__file__).read_text(encoding="utf-8")
    unit = (Path(__file__).parents[1] / "infra" / "server" /
            "post-exit-refresh.service").read_text(encoding="utf-8")
    for forbidden in (
        "bot.kis_orders", "bot.kis_buy", "bot.kill", "place_buy",
        "place_sell", "EnvironmentFile", "APPKEY", "APPSECRET", "CANO",
    ):
        assert forbidden not in source
        assert forbidden not in unit
    assert "Nice=10" in unit and "MemoryMax=380M" in unit
    assert "SCANNER_CACHE_DIR=/var/cache/stock-post-exit" in unit
    print("[PASS] KIS·주문·kill·시크릿 경계 분리 + 저우선순위 자원격리")


def main():
    test_horizons_use_exchange_session_and_show_three_denominators()
    test_estimated_prices_are_visible_but_excluded_from_common_traits()
    test_losses_invalid_rows_and_exit_session_high_are_not_counted()
    test_same_symbol_multiple_profit_exits_stay_separate()
    test_snapshot_loader_is_cache_only_and_published_file_fails_closed()
    test_order_plane_and_secrets_are_absent()
    print("\n익절 사후추적 계산·품질·격리 검증 통과.")


if __name__ == "__main__":
    main()
