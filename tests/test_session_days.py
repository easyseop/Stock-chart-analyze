"""세션일 짝짓기 — RZLV 09-01 오독(진입 다음 세션의 종가로 200일선 판정) 재발 방지."""
import datetime
import tempfile
import time
from zoneinfo import ZoneInfo

import pytest

from bot import ledger as L
from scripts import session_days as S
from scripts import sleeve_b_trend_test as T

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _ledger(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", f"{tmp}/led.jsonl")
    yield


def _kst(y, m, d, hh, mm, ss=0) -> float:
    return datetime.datetime(y, m, d, hh, mm, ss, tzinfo=KST).timestamp()


def _submit(key, symbol, side, qty, ts, *, filled=None, price=1.0):
    L._append({"ev": "submit", "key": key, "symbol": symbol, "intended": qty,
               "filled": 0, "state": "submitted", "ts": ts,
               "meta": {"side": side, "market": "US", "price": price}})
    if filled:
        L.on_result(key, "filled", filled)


# ── 세션일 ────────────────────────────────────────────────────────────────

def test_rzlv_after_midnight_kst_entry_is_previous_us_session():
    assert S.session_day(_kst(2026, 9, 1, 3, 41, 12), "RZLV") == "2026-08-31"


def test_evening_kst_entry_is_same_date_session():
    assert S.session_day(_kst(2026, 8, 19, 22, 44, 39), "OBDC") == "2026-08-19"


def test_domestic_code_uses_kst_calendar():
    assert S.session_day(_kst(2026, 9, 10, 9, 5), "005930") == "2026-09-10"
    assert S.session_day(_kst(2026, 9, 10, 9, 5), "ABC", market="KR") == "2026-09-10"


def test_us_session_boundary_at_kst_midnight():
    assert S.session_day(_kst(2026, 9, 2, 0, 0, 1), "RZLV") == "2026-09-01"
    assert S.session_day(_kst(2026, 9, 1, 23, 59, 59), "RZLV") == "2026-09-01"


# ── 매도 행 → (진입 세션, 청산 세션) ───────────────────────────────────────

def _rzlv_ledger():
    _submit("sb:RZLV:RZLV-2026-09-01-shelf", "RZLV", "BUY", 195,
            _kst(2026, 9, 1, 3, 41, 12), filled=195, price=2.92)
    _submit("kis:default:RZLV:2026-09-01:sell#c1", "RZLV", "SELL", 195,
            _kst(2026, 9, 1, 22, 32, 8), filled=195, price=2.53)


def test_sessions_for_rzlv_sell_row():
    _rzlv_ledger()
    row = {"code": "RZLV", "market": "US",
           "executed_at": "2026-09-01T22:35:00+09:00"}
    d0, d1, entry = S.sessions_for_sell(row)
    assert (d0, d1) == ("2026-08-31", "2026-09-01")
    assert entry["key"] == "sb:RZLV:RZLV-2026-09-01-shelf"


def test_latest_filled_buy_before_sell_is_the_entry():
    _submit("sb:RZLV:RZLV-2026-08-12-shelf", "RZLV", "BUY", 148,
            _kst(2026, 8, 12, 22, 30, 31), filled=148)
    _submit("kis:default:RZLV:2026-08-12:sell#c1", "RZLV", "SELL", 148,
            _kst(2026, 8, 18, 22, 32, 55), filled=148)
    _rzlv_ledger()
    first = {"code": "RZLV", "market": "US",
             "executed_at": "2026-08-18T22:40:00+09:00"}
    d0, d1, entry = S.sessions_for_sell(first)
    assert (d0, d1) == ("2026-08-12", "2026-08-18")
    assert entry["key"] == "sb:RZLV:RZLV-2026-08-12-shelf"
    second = {"code": "RZLV", "market": "US",
              "executed_at": "2026-09-01T22:35:00+09:00"}
    assert S.sessions_for_sell(second)[0] == "2026-08-31"


def test_unfilled_buy_is_not_an_entry():
    _submit("sb:RZLV:x#1", "RZLV", "BUY", 10, _kst(2026, 9, 1, 3, 0), filled=None)
    row = {"code": "RZLV", "market": "US",
           "executed_at": "2026-09-01T22:35:00+09:00"}
    d0, d1, entry = S.sessions_for_sell(row)
    assert d0 is None and entry is None and d1 == "2026-09-01"


def test_journal_lag_exit_still_maps_to_submit_session():
    """실현 시각이 저널 지연으로 다음날 새벽이라도 청산 세션은 제출 세션이다."""
    _rzlv_ledger()
    row = {"code": "RZLV", "market": "US",
           "executed_at": "2026-09-02T14:30:00+09:00"}   # 09-02 ET 01:30 — 다음날
    assert S.sessions_for_sell(row)[1] == "2026-09-01"


def test_bad_executed_at_returns_nothing():
    assert S.sessions_for_sell({"code": "RZLV", "executed_at": "?"}) == (None, None, None)


# ── 200일선 판정: 진입 세션 봉을 포함하지 않는다(미래 참조 금지) ─────────────

def _bars(n=205, close=2.70):
    day0 = datetime.date(2025, 10, 1)
    out = []
    for i in range(n):
        out.append({"d": (day0 + datetime.timedelta(days=i)).isoformat(),
                    "c": close, "hi": close, "lo": close})
    return out


def test_trend_at_uses_entry_price_and_prior_bars_only():
    bars = _bars()
    session = bars[-1]["d"]
    bars[-1]["c"] = 2.39                    # 진입 세션 종가(손절 후) — 써선 안 됨
    price, ma = T.trend_at(bars, session, entry_price=2.92)
    assert price == 2.92
    assert ma == pytest.approx(2.70)
    assert price >= ma                      # RZLV 08-31 ET: 위


def test_trend_at_kst_day_would_have_used_next_session():
    """옛 방식 재현: 세션일을 하루 늦게 주면 손절 후 봉이 평균에 섞인다."""
    bars = _bars()
    bars[-1]["c"] = 0.5
    _, ma_wrong = T.trend_at(bars, "2099-01-01", entry_price=2.92)
    _, ma_right = T.trend_at(bars, bars[-1]["d"], entry_price=2.92)
    assert ma_wrong < ma_right


def test_trend_at_short_history_withholds_judgement():
    bars = _bars(n=150)
    price, ma = T.trend_at(bars, bars[-1]["d"], entry_price=2.92)
    assert price == 2.92 and ma is None


def test_trend_at_no_prior_bars():
    bars = _bars(n=5)
    assert T.trend_at(bars, bars[0]["d"], entry_price=1.0) == (None, None)
