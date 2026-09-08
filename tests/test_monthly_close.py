"""월 귀속 회귀 — by_month가 존재하지 않는 키를 읽어 항상 비어 있었다."""
import io, contextlib, os, sys
import pytest
from bot import trade_stats, trade_history
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import monthly_close as MC  # noqa: E402

FAKE = {"available": True, "partial": False, "trades": [
    {"side": "sell", "sleeve": "B", "code": "PAAS", "day": "2026-08-27",
     "executed_at": "2026-08-27T00:12:00+09:00", "return_pct": 10.14,
     "realized_pnl_krw": 33658.2, "reason_kind": "time_stop"},
    {"side": "sell", "sleeve": "B", "code": "TRUP", "day": "2026-09-02",
     "executed_at": "2026-09-02T22:36:00+09:00", "return_pct": -6.4,
     "realized_pnl_krw": -19571.55, "reason_kind": "time_stop"},
    {"side": "sell", "sleeve": "A", "code": "CRVL", "day": "2026-08-20",
     "executed_at": "2026-08-20T23:00:00+09:00", "return_pct": 17.16,
     "realized_pnl_krw": 90000.0, "reason_kind": "take_profit"},
    {"side": "buy", "sleeve": "A", "code": "X", "day": "2026-08-20",
     "executed_at": "2026-08-20T22:00:00+09:00", "reason_kind": "entry"},
]}


@pytest.fixture(autouse=True)
def _snap(monkeypatch):
    monkeypatch.setattr(trade_history, "snapshot", lambda limit=500: FAKE)
    monkeypatch.setattr(MC.alpha, "_load", lambda: {"days": []})


def test_by_month_is_no_longer_empty():
    """실측 2026-08-25 발행분에서 by_month가 {}였다 — 월별 버킷의 존재 자체를 고정."""
    out = trade_stats.summary()
    assert set(out["by_month"]) == {"2026-08", "2026-09"}, out["by_month"]
    assert out["by_month"]["2026-08"]["closed"] == 2
    assert out["by_month"]["2026-09"]["closed"] == 1


def test_month_rows_use_kst_day_and_sells_only():
    rows = MC.month_rows(FAKE["trades"], "2026-08")
    assert sorted(r["code"] for r in rows) == ["CRVL", "PAAS"]      # 매수·9월 제외


def test_report_splits_sleeves_and_sums_pnl(capsys):
    assert MC.report("2026-08") == 0
    out = capsys.readouterr().out
    assert "확정 매도 2건" in out and "슬리브 A" in out and "슬리브 B" in out
    assert "+33,658" in out and "+90,000" in out        # 슬리브별 실현손익
    assert "PAAS" in out and "TRUP" not in out           # 9월 건 제외


def test_empty_month_exits_one(capsys):
    assert MC.report("2026-07") == 1
