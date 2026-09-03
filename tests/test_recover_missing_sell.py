"""체결됐으나 거절로 오종결된 매도의 회계 복구 — PAAS 사고 재현.

실측 2026-08-26: PAAS 매도 5주(odno 41030)가 같은 세션 @52.99 전량 체결됐는데
부재 증명이 `rejected·filled=0`으로 닫았다. 원장은 5주를 계속 보유 중이라 믿고,
costbook은 33만원을 묶어두고, 보호원장은 없는 수량에 손절선을 걸고 있었다.
"""
import datetime
import json
import os
import sys
import tempfile
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import kis_recover_missing_sell as RS   # noqa: E402

from bot import costbook, kis_positions, ledger as L  # noqa: E402


ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm) -> float:
    return datetime.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def _row(odno="0000041030", qty="5", price="52.99", side="01", pdno="PAAS"):
    return {"odno": odno, "pdno": pdno, "sll_buy_dvsn_cd": side,
            "ft_ccld_qty": qty, "ft_ccld_unpr3": price}


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(L, "LEDGER_PATH", f"{tmp}/orders.jsonl")
    monkeypatch.setenv("KIS_POSITIONS_PATH", f"{tmp}/positions.jsonl")
    monkeypatch.setenv("COSTBOOK_PATH", f"{tmp}/costbook.jsonl")
    monkeypatch.setattr(kis_positions, "PATH", f"{tmp}/positions.jsonl")
    key = "xe:PAAS:time:2026-08-06#1"
    L._append({"ev": "submit", "key": key, "symbol": "PAAS", "intended": 5,
               "filled": 0, "state": "submitted", "reason": "B 타임스탑 21일",
               "ts": _et(2026, 8, 26, 11, 1),
               "meta": {"side": "SELL", "market": "US", "excg": "NYSE",
                        "hldg_before": 5, "price": 52.99, "fx": 1380.0,
                        "pos_key": "sb:PAAS:PAAS-2026-08-06-shelf",
                        "sleeve": "B", "ccy": "USD"}})
    L.bind_broker_order(key, "0000041030", ord_tmd="110100")
    L.on_result(key, "rejected", 0, open_order=False)
    kis_positions.record("PAAS", stop=41.4834, ccy="USD", entry=48.112,
                         qty=5, sleeve="B",
                         pos_key="sb:PAAS:PAAS-2026-08-06-shelf")
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None:
                        {"rt_cd": "0", "output": [_row()] if excg == "NYSE" else []})
    monkeypatch.setattr(RS.kis, "holdings",
                        lambda market, excg=None: {})     # 이미 팔려서 0주
    return key


def test_plan_matches_the_broker_fill(env):
    plan = RS.collect(env, trade_date="20260826")
    assert plan["fill_qty"] == 5 and plan["fill_price"] == 52.99
    assert plan["odno"] == "41030"      # order_no_key가 선행 0을 정규화
    assert plan["broker_qty_now"] == 0
    # 11:01 ET = KST 익일 00:01 → 08-27에 귀속
    assert plan["realized_day_kst"] == "2026-08-27"
    assert plan["recorded_filled"] == 0 and plan["missing_qty"] == 5


def test_plan_writes_nothing(env):
    before = open(L.LEDGER_PATH, encoding="utf-8").read()
    RS.collect(env, trade_date="20260826")
    assert open(L.LEDGER_PATH, encoding="utf-8").read() == before


def test_query_failure_is_not_absence(env, monkeypatch):
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None: None)
    with pytest.raises(RS.Refused, match="조회 실패"):
        RS.collect(env, trade_date="20260826")


def test_balance_failure_is_not_zero(env, monkeypatch):
    monkeypatch.setattr(RS.kis, "holdings", lambda market, excg=None: None)
    with pytest.raises(RS.Refused, match="잔고 조회 실패"):
        RS.collect(env, trade_date="20260826")


def test_refuses_when_broker_still_holds(env, monkeypatch):
    """아직 들고 있으면 우리 거절 판정이 맞았을 수 있다 — 닫으면 안 된다."""
    monkeypatch.setattr(RS.kis, "holdings",
                        lambda market, excg=None: {"PAAS": 5} if excg == "NYSE" else {})
    with pytest.raises(RS.Refused, match="아직 원장"):
        RS.collect(env, trade_date="20260826")


def test_refuses_buy_row(env, monkeypatch):
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None:
                        {"rt_cd": "0",
                         "output": [_row(side="02")] if excg == "NYSE" else []})
    with pytest.raises(RS.Refused, match="매도 행이 아니다"):
        RS.collect(env, trade_date="20260826")


def test_refuses_ambiguous_odno(env, monkeypatch):
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None:
                        {"rt_cd": "0", "output": [_row(), _row()]}
                        if excg == "NYSE" else {"rt_cd": "0", "output": []})
    with pytest.raises(RS.Refused, match="유일해야"):
        RS.collect(env, trade_date="20260826")


def test_refuses_fill_over_intended(env, monkeypatch):
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None:
                        {"rt_cd": "0", "output": [_row(qty="9")]}
                        if excg == "NYSE" else {"rt_cd": "0", "output": []})
    with pytest.raises(RS.Refused, match="초과"):
        RS.collect(env, trade_date="20260826")


def test_apply_requires_ack(env):
    with pytest.raises(RS.Refused, match="ack"):
        RS.apply(env, trade_date="20260826", ack="  ")


def test_apply_closes_position_and_costbook(env):
    out = RS.apply(env, trade_date="20260826", ack="운영자: ccnl 대조 완료")
    assert out["accounting"]["ok"] and out["accounting"]["delta"] == 5
    assert out["position_qty_after"] == 0
    assert out["costbook_open_after"] == 0
    state = L.state_of(env)
    assert state["state"] == "filled" and state["filled"] == 5
    assert int(state.get("accounted") or 0) == 5


def test_apply_is_idempotent(env):
    RS.apply(env, trade_date="20260826", ack="1회차")
    with pytest.raises(RS.Refused, match="복구할 것이 없다"):
        RS.apply(env, trade_date="20260826", ack="2회차")


def test_apply_records_operator_audit(env):
    RS.apply(env, trade_date="20260826", ack="운영자: 근거 X")
    action = L.state_of(env)["last_operator_action"]
    assert action["action"] == "recover-missing-sell"
    assert action["ack"] == "운영자: 근거 X"
    assert action["evidence"]["filled"] == 5


def test_cli_plan_is_json_and_sends_no_orders(env, capsys):
    rc = RS.main(["--key", env, "--trade-date", "20260826", "--plan"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["orders_sent"] == 0
    assert out["fill_qty"] == 5


def test_cli_refusal_exits_two(env, capsys):
    rc = RS.main(["--key", "nope", "--trade-date", "20260826", "--plan"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["refused"] and out["orders_sent"] == 0


# ── 실현일 귀속 — 월별 결산이 여기에 달려 있다 ──────────────────
def test_morning_et_fill_books_to_the_same_kst_day(env):
    """09:35 ET = KST 같은 날 22:35. 익일로 밀면 거래가 달을 넘어간다."""
    key = "xe:PAAS:morning"
    L._append({"ev": "submit", "key": key, "symbol": "PAAS", "intended": 5,
               "filled": 0, "state": "submitted", "reason": "t",
               "ts": _et(2026, 8, 31, 9, 35),
               "meta": {"side": "SELL", "market": "US", "excg": "NYSE",
                        "hldg_before": 5, "price": 52.99, "fx": 1380.0,
                        "pos_key": "sb:PAAS:PAAS-2026-08-06-shelf",
                        "sleeve": "B", "ccy": "USD"}})
    L.bind_broker_order(key, "0000041030", ord_tmd="093500")
    L.on_result(key, "rejected", 0, open_order=False)
    plan = RS.collect(key, trade_date="20260831")
    assert plan["realized_day_kst"] == "2026-08-31", "하루 밀렸다"


def test_cross_session_fill_is_refused(env):
    """제출 세션과 체결 세션이 다르면 시각을 유추할 수 없다 — 추측 금지."""
    with pytest.raises(RS.Refused, match="실현일 귀속을 자동 판단"):
        RS.collect(env, trade_date="20260831")     # 픽스처는 08-26 제출


# ── TRUP: 닫힌 부분체결도 대상 ──────────────────────────────────
def _trup_partial(monkeypatch, *, filled=1, intended=7, broker_qty="7"):
    key = "xe:TRUP:time:2026-08-12#2"
    L._append({"ev": "submit", "key": key, "symbol": "TRUP",
               "intended": intended, "filled": 0, "state": "submitted",
               "reason": "B 타임스탑 21일", "ts": _et(2026, 9, 2, 9, 32),
               "meta": {"side": "SELL", "market": "US", "excg": "NASD",
                        "hldg_before": intended, "price": 28.08, "fx": 1380.0,
                        "pos_key": "sb:TRUP:TRUP-2026-08-12-shelf",
                        "sleeve": "B", "ccy": "USD"}})
    L.bind_broker_order(key, "0000041895", ord_tmd="093200")
    L.on_result(key, "partial", filled, open_order=False)   # 닫힌 부분체결
    L._append({"ev": "accounted", "key": key, "accounted": filled})
    kis_positions.record("TRUP", stop=25.0, ccy="USD", entry=30.0,
                         qty=intended - filled, sleeve="B",
                         pos_key="sb:TRUP:TRUP-2026-08-12-shelf")
    monkeypatch.setattr(RS.kis, "fills",
                        lambda excg=None, start=None, end=None:
                        {"rt_cd": "0",
                         "output": [_row(odno="0000041895", qty=broker_qty,
                                         price="28.08128571", pdno="TRUP")]
                         if excg == "NASD" else []})
    monkeypatch.setattr(RS.kis, "holdings", lambda market, excg=None: {})
    return key


def test_closed_partial_is_a_target(env, monkeypatch):
    key = _trup_partial(monkeypatch)
    plan = RS.collect(key, trade_date="20260902")
    assert plan["fill_qty"] == 7 and plan["recorded_filled"] == 1
    assert plan["missing_qty"] == 6
    assert plan["realized_day_kst"] == "2026-09-02"    # 09:32 ET → 같은 날


def test_open_order_is_refused(env, monkeypatch):
    """열린 주문은 자동 대사의 몫 — 끼어들면 이중 회계."""
    key = _trup_partial(monkeypatch)
    L.on_result(key, "partial", 1, open_order=True)
    with pytest.raises(RS.Refused, match="아직 열린 주문"):
        RS.collect(key, trade_date="20260902")


def test_nothing_to_recover_is_refused(env, monkeypatch):
    """브로커 체결이 기록을 넘지 않으면 할 일이 없다."""
    key = _trup_partial(monkeypatch, filled=7, broker_qty="7")
    with pytest.raises(RS.Refused, match="복구할 것이 없다"):
        RS.collect(key, trade_date="20260902")


def test_closed_partial_apply_accounts_the_remainder(env, monkeypatch):
    key = _trup_partial(monkeypatch)
    out = RS.apply(key, trade_date="20260902", ack="운영자: odno 41895 대조")
    assert out["accounting"]["ok"] and out["accounting"]["delta"] == 6
    assert L.state_of(key)["filled"] == 7
    assert out["position_qty_after"] == 0
