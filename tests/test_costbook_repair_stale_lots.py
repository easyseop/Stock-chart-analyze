"""잔재 lot 보정 — WLDN·CHKP 실측 모양에서 손익·원가·거래이력이 한 번에 맞는가."""
import json
import os
import tempfile

import pytest

from bot import costbook as C
from bot import kis_positions as P
from bot import ledger as L
from bot import trade_history as H
from scripts import costbook_repair_stale_lots as REPAIR

FX = 1380.0


@pytest.fixture
def books(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", os.path.join(tmp, "orders.jsonl"))
    monkeypatch.setattr(P, "PATH", os.path.join(tmp, "positions.jsonl"))
    monkeypatch.setenv("COSTBOOK_PATH", os.path.join(tmp, "cost.jsonl"))
    return tmp


def _buggy_wldn():
    """버그 당시 이벤트 순서 그대로: 두 키에 매수, 부모 키만 닫힌 매도."""
    parent, child = "kb:WLDN:WLDN-2026-09-10-now", "kb:WLDN:WLDN-2026-09-10-now:pb"
    for key, qty, px, eid in ((parent, 19, 83.53, "fill:p:BUY:19"),
                              (child, 20, 81.6005, "fill:c:BUY:20")):
        C.add_lot(key, "WLDN", qty, px, fx=FX, sleeve="A", event_id=eid)
        P.apply_buy_fill("WLDN", qty=qty, price=px, stop=77.36, ccy="USD",
                         pos_key=key, sleeve="A", event_id=eid)
    sell_eid = "fill:xe:WLDN:stop#1:SELL:39"
    L.record_submit("xe:WLDN:stop#1", "WLDN", 39, "미국주 손절 chase",
                    meta={"side": "SELL", "pos_key": parent, "sleeve": "A",
                          "fx": FX, "ccy": "USD", "market": "US"})
    L.on_result("xe:WLDN:stop#1", "filled", 39, fill_price=79.9,
                fill_price_source="broker", open_order=False)
    # 옛 close_lot 동작을 그대로 재현 — 지명 lot(19)만 닫고 대금은 39주 전액.
    proceeds = 39 * 79.9 * FX
    cost19 = 19 * 83.53 * FX
    C._append({"ev": "close", "key": parent, "qty": 39, "proceeds_krw": proceeds,
               "cost_closed_krw": cost19, "realized_pnl_krw": proceeds - cost19,
               "sleeve": "A", "day_kst": "2026-09-17", "event_id": sell_eid})
    P.apply_sell_fill("WLDN", qty=39, price=79.9, pos_key=parent, event_id=sell_eid)
    return parent, child, sell_eid, proceeds


def test_plan_finds_stale_child_lot_and_reports_corrected_pnl(books):
    parent, child, sell_eid, proceeds = _buggy_wldn()
    plan = REPAIR.collect()
    assert [i["key"] for i in plan["items"]] == [child]
    it = plan["items"][0]
    assert it["qty"] == 20 and it["attach_to"] == sell_eid and it["day_kst"] == "2026-09-17"
    assert it["recorded_pnl_krw"] > 0 and it["corrected_pnl_krw"] < 0
    assert plan["pnl_delta_by_month"] == {"2026-09": pytest.approx(-20 * 81.6005 * FX, rel=1e-6)}
    assert plan["skipped"] == []


def test_apply_fixes_costbook_daily_and_history_row(books):
    parent, child, sell_eid, proceeds = _buggy_wldn()
    before_rows = [r for r in H.snapshot(limit=50)["trades"] if r["side"] == "sell"]
    assert before_rows[0]["return_pct"] > 50, "버그 재현이 안 됐다"
    out = REPAIR.apply("test ack")
    assert out["applied"][0]["state"] == "closed"
    assert C.open_qty("WLDN") == 0 and out["open_cost_A_after"] == pytest.approx(0.0)
    f = C._fold()
    true_pnl = proceeds - (19 * 83.53 + 20 * 81.6005) * FX
    assert f["daily_realized"]["2026-09-17"] == pytest.approx(true_pnl)
    assert f["totals"]["sell_proceeds"] == pytest.approx(proceeds)
    rows = [r for r in H.snapshot(limit=50)["trades"] if r["side"] == "sell"]
    assert len(rows) == 1
    assert rows[0]["realized_pnl_krw"] == pytest.approx(true_pnl, rel=1e-6)
    assert -5 < rows[0]["return_pct"] < 0


def test_apply_is_idempotent(books):
    _buggy_wldn()
    REPAIR.apply("a")
    out = REPAIR.apply("a")
    assert out["items"] == [] and out["applied"] == []
    assert sum(1 for l in open(os.environ["COSTBOOK_PATH"], encoding="utf-8")
               if '"stale-lot-repair"' in l) == 1


def test_open_position_split_lot_is_left_alone(books):
    parent, child = "kb:G:G-2026-09-09-now", "kb:G:G-2026-09-09-now:pb"
    for key, qty, px, eid in ((parent, 8, 35.0, "e1"), (child, 8, 34.0, "e2")):
        C.add_lot(key, "G", qty, px, fx=FX, sleeve="A", event_id=eid)
        P.apply_buy_fill("G", qty=qty, price=px, stop=32.0, ccy="USD",
                         pos_key=key, sleeve="A", event_id=eid)
    plan = REPAIR.collect()
    assert plan["items"] == [] and plan["skipped"] == []


def test_stale_lot_without_any_sell_is_skipped_not_closed(books):
    C.add_lot("kb:X:x", "X", 3, 10.0, fx=FX, sleeve="A", event_id="e1")
    plan = REPAIR.collect()
    assert plan["items"] == [] and plan["skipped"][0]["key"] == "kb:X:x"
    REPAIR.apply("a")
    assert C.open_qty("X") == 3


def test_apply_requires_ack(books):
    with pytest.raises(REPAIR.Refused):
        REPAIR.apply("")
