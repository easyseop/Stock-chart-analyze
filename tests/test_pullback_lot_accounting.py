"""눌림 2차 lot 회계 — 한 종목의 매도는 그 종목의 모든 lot에서 나간다.

실측 2026-09-17 WLDN: 1차 19주(kb:WLDN:…-now)와 눌림 2차 20주(…-now:pb)가
다른 costbook 키에 쌓였다. 39주 손절 매도가 지명 lot(19주)만 닫아 19주 원가로
39주 대금을 실현했고(+96%), 2차 20주 원가 2,252,174원은 열린 채 남았다.
CHKP(+127%)·OMCL(+95%)도 같은 얼굴. 9월 결산이 이 셋만으로 약 300만원 부풀었다.

두 겹으로 막는다: ① 2차 체결의 회계를 부모 pos_key로 귀속(kis_accounting),
② 그래도 키가 갈라져 있으면 매도가 같은 종목의 다른 lot으로 이어서 닫는다
(costbook.close_plan). 거래이력도 같은 규칙으로 한 행에 합친다.
"""
import json
import os
import tempfile
from unittest import mock

import pytest

from bot import costbook as C
from bot import kis_accounting as A
from bot import kis_positions as P
from bot import ledger as L
from bot import trade_history as H

FX = 1380.0
PARENT = "kb:WLDN:WLDN-2026-09-10-now"
CHILD = PARENT + ":pb"


@pytest.fixture
def books(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl"))
    monkeypatch.setattr(P, "PATH", os.path.join(tmp, "positions.jsonl"))
    monkeypatch.setenv("COSTBOOK_PATH", os.path.join(tmp, "cost.jsonl"))
    monkeypatch.setenv("USER_BASELINE_PATH", os.path.join(tmp, "baseline.json"))
    monkeypatch.setenv("SYMBOL_FREEZE_PATH", os.path.join(tmp, "freeze.json"))
    with open(os.environ["USER_BASELINE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({"symbols": []}, fp)
    with open(os.environ["SYMBOL_FREEZE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({}, fp)
    return tmp


def _wldn_split_lots():
    """버그 당시 모양 그대로 — 두 키에 따로 쌓인 lot."""
    C.add_lot(PARENT, "WLDN", 19, 83.53, fx=FX, sleeve="A", event_id="fill:p:BUY:19")
    C.add_lot(CHILD, "WLDN", 20, 81.6005, fx=FX, sleeve="A", event_id="fill:c:BUY:20")


# ── costbook ─────────────────────────────────────────────────────────────

def test_wldn_sell_closes_both_lots_and_realizes_true_pnl(books):
    _wldn_split_lots()
    proceeds = 39 * 79.9 * FX
    pnl = C.close_lot(PARENT, 39, proceeds, sleeve="A", day_kst="2026-09-17",
                      event_id="fill:s:SELL:39")
    expected = proceeds - (19 * 83.53 + 20 * 81.6005) * FX
    assert pnl == pytest.approx(expected)
    assert pnl < 0, "39주 손절이 이익으로 잡혔다"
    assert C.open_qty("WLDN") == 0
    assert C.open_cost_total("A") == pytest.approx(0.0)
    f = C._fold()
    assert f["totals"]["sell_proceeds"] == pytest.approx(proceeds)
    assert f["daily_realized"]["2026-09-17"] == pytest.approx(expected)


def test_close_is_idempotent_across_spilled_lots(books):
    _wldn_split_lots()
    proceeds = 39 * 79.9 * FX
    first = C.close_lot(PARENT, 39, proceeds, sleeve="A", event_id="fill:s:SELL:39")
    again = C.close_lot(PARENT, 39, proceeds, sleeve="A", event_id="fill:s:SELL:39")
    assert again == pytest.approx(first)
    assert C._fold()["totals"]["sell_proceeds"] == pytest.approx(proceeds)


def test_single_lot_behaviour_unchanged(books):
    C.add_lot(PARENT, "WLDN", 19, 83.53, fx=FX, sleeve="A", event_id="e1")
    proceeds = 19 * 90.0 * FX
    pnl = C.close_lot(PARENT, 19, proceeds, sleeve="A", event_id="s1")
    assert pnl == pytest.approx(proceeds - 19 * 83.53 * FX)
    events = [json.loads(l) for l in open(os.environ["COSTBOOK_PATH"], encoding="utf-8")]
    assert sum(1 for e in events if e["ev"] == "close") == 1


def test_no_lot_at_all_keeps_legacy_full_proceeds(books):
    pnl = C.close_lot("legacy:X", 5, 1000.0, sleeve="A", event_id="s1")
    assert pnl == pytest.approx(1000.0)


def test_other_sleeve_lot_is_not_consumed(books):
    C.add_lot(PARENT, "WLDN", 19, 83.53, fx=FX, sleeve="A", event_id="e1")
    C.add_lot("sb:WLDN:x", "WLDN", 20, 80.0, fx=FX, sleeve="B", event_id="e2")
    C.close_lot(PARENT, 39, 39 * 79.9 * FX, sleeve="A", event_id="s1")
    assert C.open_qty("WLDN", "B") == 20
    assert C.open_qty("WLDN", "A") == 0


def test_other_symbol_lot_is_not_consumed(books):
    C.add_lot(PARENT, "WLDN", 19, 83.53, fx=FX, sleeve="A", event_id="e1")
    C.add_lot("kb:ALG:x", "ALG", 22, 170.3, fx=FX, sleeve="A", event_id="e2")
    C.close_lot(PARENT, 39, 39 * 79.9 * FX, sleeve="A", event_id="s1")
    assert C.open_qty("ALG") == 22


def test_close_plan_orders_named_lot_first_then_others():
    lots = {CHILD: {"symbol": "WLDN", "qty": 20, "sleeve": "A"},
            PARENT: {"symbol": "WLDN", "qty": 19, "sleeve": "A"}}
    assert C.close_plan(lots, PARENT, 39) == [(PARENT, 19), (CHILD, 20)]
    assert C.close_plan(lots, PARENT, 25) == [(PARENT, 19), (CHILD, 6)]
    assert C.close_plan(lots, PARENT, 10) == [(PARENT, 10)]


# ── kis_accounting: 2차 체결은 부모 키로 ─────────────────────────────────

def _meta(pos_key, parent=None):
    m = {"side": "BUY", "market": "US", "price": 81.61, "pos_key": pos_key,
         "sleeve": "A", "fx": FX, "ccy": "USD", "stop": 77.36, "target": 96.5,
         "name": "Willdan", "opened": "2026-09-10"}
    if parent:
        m["parent_key"] = parent
        m["pending"] = True
    return m


def test_child_fill_is_booked_under_parent_position(books):
    L.record_submit(PARENT, "WLDN", 19, meta=_meta(PARENT))
    L.on_result(PARENT, "filled", 19, fill_price=83.53, open_order=False)
    A.sync_fill(PARENT, filled_qty=19, fill_price=83.53, fill_price_source="ccnl")
    L.record_submit(CHILD, "WLDN", 20, meta=_meta(CHILD, parent=PARENT))
    L.on_result(CHILD, "filled", 20, fill_price=81.6005, open_order=False)
    acct = A.sync_fill(CHILD, filled_qty=20, fill_price=81.6005, fill_price_source="ccnl")
    assert acct["ok"] and acct["delta"] == 20
    lots = C._fold()["lots"]
    assert CHILD not in lots, "2차 lot이 여전히 자기 키에 쌓였다"
    assert lots[PARENT]["qty"] == 39
    assert P.load()["WLDN"]["qty"] == 39


def test_order_without_parent_keeps_own_key(books):
    L.record_submit(PARENT, "WLDN", 19, meta=_meta(PARENT))
    L.on_result(PARENT, "filled", 19, fill_price=83.53, open_order=False)
    A.sync_fill(PARENT, filled_qty=19, fill_price=83.53, fill_price_source="ccnl")
    assert PARENT in C._fold()["lots"]


# ── trade_history: 한 행에 합친다 ────────────────────────────────────────

def _sell_wldn():
    L.record_submit("xe:WLDN:stop#1", "WLDN", 39, "미국주 손절 chase",
                    meta={"side": "SELL", "pos_key": PARENT, "sleeve": "A",
                          "fx": FX, "ccy": "USD", "market": "US"})
    L.on_result("xe:WLDN:stop#1", "filled", 39, fill_price=79.9,
                fill_price_source="broker", open_order=False)
    eid = "fill:xe:WLDN:stop#1:SELL:39"
    C.close_lot(PARENT, 39, 39 * 79.9 * FX, sleeve="A", day_kst="2026-09-17",
                event_id=eid)
    P.apply_sell_fill("WLDN", qty=39, price=79.9, pos_key=PARENT, event_id=eid)


def test_history_row_shows_true_return_for_split_lots(books):
    for key, qty, px, eid in ((PARENT, 19, 83.53, "fill:p:BUY:19"),
                              (CHILD, 20, 81.6005, "fill:c:BUY:20")):
        C.add_lot(key, "WLDN", qty, px, fx=FX, sleeve="A", event_id=eid)
        P.apply_buy_fill("WLDN", qty=qty, price=px, stop=77.36, ccy="USD",
                         pos_key=key, sleeve="A", event_id=eid)
    _sell_wldn()
    rows = [r for r in H.snapshot(limit=50)["trades"] if r["side"] == "sell"]
    assert len(rows) == 1
    row = rows[0]
    assert row["qty"] == 39 and row["position_qty_after"] == 0
    assert row["entry_price"] == pytest.approx((19 * 83.53 + 20 * 81.6005) / 39)
    assert -5 < row["return_pct"] < 0, row["return_pct"]
    assert row["realized_pnl_krw"] == pytest.approx(
        39 * 79.9 * FX - (19 * 83.53 + 20 * 81.6005) * FX, rel=1e-6)
    assert row["cost_closed_krw"] == pytest.approx((19 * 83.53 + 20 * 81.6005) * FX, rel=1e-6)
