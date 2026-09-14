"""0체결 행에 잔량이 남은 당일 주문은 살아 있다 — 닫으면 유령·이중매도가 된다.

실측 2026-09-09(KST 22:31~23:01, 미 동부 09:31~10:01):

  OBDC  타임스탑 SELL 3주 @11.18 (odno 42186). 미체결 조회에는 없고 체결내역
        행은 `ft_ccld_qty=0 · nccs_qty=3`. 잔고 3주 불변으로 20분 재확인까지
        통과 → `rejected`. 09-14 조회: 같은 ODNO가 **3주 @11.18 체결**, 잔고 0.
        결과: 원장 3주 · 브로커 0주 유령.
  NFG   +1R 절반 SELL 8주 @84.8 (odno 43289). 같은 형태로 `rejected`.
        09-14 조회: 행이 여전히 `ft_ccld_qty=0 · nccs_qty=8`, 잔고 17 그대로.

두 건 다 mock이 **살아 있는 주문을 미체결 목록에서 빼놓은** 것이다. 잔량
필드가 그 증거다. 당일 주문이면 열어 두고, 지난 거래일 주문은 더 체결될 수
없으니 종전대로 닫는다.
"""
import datetime
import json
import tempfile
import time
from zoneinfo import ZoneInfo

import pytest

from bot import kis_reconcile as R
from bot import ledger as L

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _ledger(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", f"{tmp}/led.jsonl")
    monkeypatch.setenv("USER_BASELINE_PATH", f"{tmp}/baseline.json")
    monkeypatch.setenv("SYMBOL_FREEZE_PATH", f"{tmp}/freeze.json")
    with open(f"{tmp}/baseline.json", "w", encoding="utf-8") as fp:
        json.dump({"symbols": []}, fp)
    with open(f"{tmp}/freeze.json", "w", encoding="utf-8") as fp:
        json.dump({}, fp)
    yield


def _et(y, m, d, hh, mm) -> float:
    return datetime.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def _kst(y, m, d, hh, mm) -> float:
    return datetime.datetime(y, m, d, hh, mm, tzinfo=KST).timestamp()


def _order(*, submitted: float, key="xe:OBDC:time:2026-08-19#1", symbol="OBDC",
           qty=3, before=3, odno="0000042186", market="US", marker_at=None):
    L._append({"ev": "submit", "key": key, "symbol": symbol, "intended": qty,
               "filled": 0, "state": "submitted", "reason": "time stop",
               "ts": submitted,
               "meta": {"side": "SELL", "market": market, "excg": "NYSE",
                        "hldg_before": before, "price": 11.18}})
    L.bind_broker_order(key, odno, ord_tmd="093114")
    L.on_result(key, "ack", 0)
    if marker_at is not None:
        L.record_reconcile_meta(key, reason="absence-observed",
                                meta={"absence_first_at": marker_at,
                                      "absence_first_hldg": before})
    return key, symbol, before


def _proof(key, symbol, before, *, ccnl):
    return {key: {"nccs_rows": [], "ccnl_rows": ccnl,
                  "holdings": {symbol: before}}}


def _row(remaining, *, odno="42186", symbol="OBDC", ordered=3):
    row = {"odno": odno, "pdno": symbol, "sll_buy_dvsn_cd": "01",
           "ft_ord_qty": str(ordered), "ft_ccld_qty": "0",
           "ft_ord_unpr3": "11.18000000"}
    if remaining is not None:
        row["nccs_qty"] = str(remaining)
    return row


# ── OBDC 재현 ─────────────────────────────────────────────────────────────

def test_obdc_live_zero_fill_row_keeps_order_open():
    """제출 30분 뒤, 잔량 3 표시 — 재확인 창을 채웠어도 닫지 않는다."""
    submitted = _et(2026, 9, 9, 9, 31)
    now = submitted + 30 * 60
    key, sym, before = _order(submitted=submitted, marker_at=submitted + 10 * 60)
    resolved, contradictions = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[_row(3)]), now_ts=now)
    assert resolved == [] and contradictions == []
    assert L.state_of(key)["state"] == "ack"


def test_obdc_late_fill_is_then_captured_by_row_resolution():
    """열어 뒀으니 뒤늦은 체결행이 1순위 경로에서 정상 확정된다."""
    submitted = _et(2026, 9, 9, 9, 31)
    key, sym, before = _order(submitted=submitted, marker_at=submitted + 10 * 60)
    R.resolve_acks_by_absence(_proof(key, sym, before, ccnl=[_row(3)]),
                              now_ts=submitted + 30 * 60)
    rows = R.normalize_rows(None, {"rt_cd": "0", "output": [
        {**_row(0), "ft_ccld_qty": "3", "ft_ccld_unpr3": "11.18000000"}]})
    rs = R.resolve_acks_from_rows(rows)
    assert len(rs) == 1 and rs[0]["state"] == "filled"
    assert L.state_of(key)["filled"] == 3


def test_remaining_zero_row_closes_after_window():
    """잔량 0 + 0체결 + 잔고 불변 + 재확인 창 — 종전과 같이 거절 종결."""
    submitted = _et(2026, 9, 9, 9, 31)
    key, sym, before = _order(submitted=submitted, marker_at=submitted + 10 * 60)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[_row(0)]), now_ts=submitted + 30 * 60)
    assert len(resolved) == 1 and resolved[0]["state"] == "rejected"
    assert resolved[0]["via"] == "zero-fill-balance-proof"


def test_row_without_remaining_field_keeps_previous_rule():
    """잔량 필드가 없으면 판단하지 않는다 — 종전 규칙 그대로 종결."""
    submitted = _et(2026, 9, 9, 9, 31)
    key, sym, before = _order(submitted=submitted, marker_at=submitted + 10 * 60)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[_row(None)]), now_ts=submitted + 30 * 60)
    assert len(resolved) == 1 and resolved[0]["state"] == "rejected"


# ── NFG 형태: 지난 거래일 주문에 잔량 표시가 남아 있다 ────────────────────

def test_previous_session_order_with_stale_remaining_closes():
    submitted = _et(2026, 9, 9, 9, 48)
    key, sym, before = _order(submitted=submitted, key="xe:NFG:half:2026-08-25#1",
                              symbol="NFG", qty=8, before=17, odno="0000043289",
                              marker_at=submitted + 10 * 60)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[_row(8, odno="43289", symbol="NFG",
                                            ordered=8)]),
        now_ts=_et(2026, 9, 10, 9, 35))
    assert len(resolved) == 1 and resolved[0]["state"] == "rejected"


def test_session_boundary_is_us_eastern_not_kst():
    """KST 자정을 넘겨도 미 동부 같은 세션이면 살아 있다(22:30~05:00 KST)."""
    submitted = _kst(2026, 9, 10, 1, 10)       # 09-09 12:10 ET
    key, sym, before = _order(submitted=submitted, marker_at=submitted + 10 * 60)
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[_row(3)]),
        now_ts=_kst(2026, 9, 10, 3, 0))        # 09-09 14:00 ET — 같은 세션
    assert resolved == [] and L.state_of(key)["state"] == "ack"


def test_domestic_session_is_kst():
    submitted = _kst(2026, 9, 10, 9, 5)
    key, sym, before = _order(submitted=submitted, key="xe:005930:time#1",
                              symbol="005930", market="KR", odno="0000000777",
                              marker_at=submitted + 10 * 60)
    live = _row(3, odno="777", symbol="005930")
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[live]), now_ts=_kst(2026, 9, 10, 14, 0))
    assert resolved == []
    resolved, _ = R.resolve_acks_by_absence(
        _proof(key, sym, before, ccnl=[live]), now_ts=_kst(2026, 9, 11, 9, 30))
    assert len(resolved) == 1 and resolved[0]["state"] == "rejected"


# ── 단위 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("3", 3.0), ("0", 0.0), ("", None), ("abc", None), ("-1", None),
    ("nan", None), (None, None),
])
def test_row_remaining_qty_parses_conservatively(value, expected):
    row = {"ft_ccld_qty": "0"}
    if value is not None:
        row["nccs_qty"] = value
    assert R._row_remaining_qty(row) == expected


def test_rmn_qty_alias_counts_as_remaining():
    assert R._row_remaining_qty({"rmn_qty": "5"}) == 5.0


def test_unknown_submit_time_is_treated_as_live():
    row = {"ft_ccld_qty": "0", "nccs_qty": "8"}
    assert R.zero_fill_row_still_live(row, {"symbol": "NFG"}, time.time()) is True
    assert R.zero_fill_row_still_live(
        row, {"symbol": "NFG", "submitted_at": "bad"}, time.time()) is True


def test_zero_remaining_is_not_live():
    row = {"ft_ccld_qty": "0", "nccs_qty": "0"}
    assert R.zero_fill_row_still_live(
        row, {"symbol": "NFG", "submitted_at": time.time()}, time.time()) is False


# ── ALG 재현: ccnl에만 보이는 부분체결 행의 잔량은 '열린 주문'이다 ──────────
#   09-10 00:38 KST 매수 22주(odno 45807). 저널이 4주 체결·잔량 18을 먼저
#   보였고, 코드는 ccnl 행을 무조건 닫힌 것으로 읽어 `partial 4/22 · open=False`
#   로 종결했다. 브로커는 22주 전량 체결 — 18주가 미회계로 남았다.

def _buy(key="kb:ALG:ALG-2026-09-10-now", symbol="ALG", qty=22, odno="0000045807",
         submitted=None):
    submitted = _et(2026, 9, 9, 11, 38) if submitted is None else submitted
    L._append({"ev": "submit", "key": key, "symbol": symbol, "intended": qty,
               "filled": 0, "state": "submitted", "reason": "미러진입",
               "ts": submitted,
               "meta": {"side": "BUY", "market": "US", "excg": "NYSE",
                        "hldg_before": 0, "price": 170.81, "stop": 160.14,
                        "pos_key": key, "sleeve": "A", "fx": 1380.0}})
    L.bind_broker_order(key, odno, ord_tmd="113841")
    L.on_result(key, "ack", 0)
    return key


def _alg_row(filled, remaining, *, ord_dt="20260909"):
    return {"odno": "45807", "pdno": "ALG", "sll_buy_dvsn_cd": "02",
            "ord_dt": ord_dt, "ft_ord_qty": "22", "ft_ccld_qty": str(filled),
            "nccs_qty": str(remaining), "ft_ccld_unpr3": "170.30000000"}


def test_ccnl_partial_row_with_remaining_is_open():
    rows = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(4, 18)]},
                            today="20260909")
    assert rows[0]["filled"] == 4 and rows[0]["open"] is True


def test_ccnl_row_with_zero_remaining_is_closed():
    rows = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(4, 0)]},
                            today="20260909")
    assert rows[0]["open"] is False


def test_ccnl_row_from_previous_session_is_closed_despite_remaining():
    rows = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(4, 18)]},
                            today="20260910")
    assert rows[0]["open"] is False


def test_ccnl_row_without_order_date_is_treated_as_live():
    rows = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(4, 18, ord_dt="")]},
                            today="20260910")
    assert rows[0]["open"] is True


def test_nccs_row_open_flag_is_kept_when_ccnl_merges():
    nccs = {"rt_cd": "0", "output": [{"odno": "45807", "pdno": "ALG",
                                      "ft_ord_qty": "22", "nccs_qty": "18",
                                      "sll_buy_dvsn_cd": "02"}]}
    rows = R.normalize_rows(nccs, {"rt_cd": "0", "output": [_alg_row(4, 0)]},
                            today="20260909")
    assert len(rows) == 1 and rows[0]["filled"] == 4 and rows[0]["open"] is True


def test_alg_partial_stays_open_then_full_fill_is_accounted(monkeypatch):
    from bot import kis_accounting
    monkeypatch.setattr(kis_accounting, "sync_fill",
                        lambda *a, **k: {"ok": True, "delta": k.get("filled_qty")})
    key = _buy()
    first = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(4, 18)]},
                             today="20260909")
    rs = R.resolve_acks_from_rows(first)
    assert len(rs) == 1 and rs[0]["state"] == "partial" and rs[0]["open"] is True
    cur = L.state_of(key)
    assert cur["state"] == "partial" and L.fold_is_open(cur), "부분체결을 닫아 버렸다"
    later = R.normalize_rows(None, {"rt_cd": "0", "output": [_alg_row(22, 0)]},
                             today="20260909")
    rs = R.resolve_acks_from_rows(later)
    assert len(rs) == 1 and rs[0]["state"] == "filled"
    assert L.state_of(key)["filled"] == 22


def test_alg_legacy_behaviour_closed_partial(monkeypatch):
    """옛 규칙 재현(잔량 0 표시) — 부분체결이 닫히고 잔량은 사후 도구 몫."""
    from bot import kis_accounting
    monkeypatch.setattr(kis_accounting, "sync_fill", lambda *a, **k: {"ok": True})
    key = _buy()
    rs = R.resolve_acks_from_rows(R.normalize_rows(
        None, {"rt_cd": "0", "output": [_alg_row(4, 0)]}, today="20260909"))
    assert rs[0]["state"] == "partial" and rs[0]["open"] is False
    assert not L.fold_is_open(L.state_of(key))
