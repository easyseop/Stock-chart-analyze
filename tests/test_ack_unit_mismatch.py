"""INGR 재발 방지: SELL 단위 분리·동결 직접증거·사유/운영자 대사."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import time
from unittest import mock


def _setup(tmp: str):
    os.environ["USER_BASELINE_PATH"] = os.path.join(tmp, "baseline.json")
    os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
    os.environ["ORDER_LEDGER_PATH"] = os.path.join(tmp, "orders.jsonl")
    with open(os.environ["USER_BASELINE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({"symbols": []}, fp)
    with open(os.environ["SYMBOL_FREEZE_PATH"], "w", encoding="utf-8") as fp:
        json.dump({}, fp)
    from bot import ledger, ownership, kis_reconcile, sentinel, kis_orders
    for module in (ledger, ownership, kis_reconcile, sentinel, kis_orders):
        importlib.reload(module)
    ledger.LEDGER_PATH = os.environ["ORDER_LEDGER_PATH"]
    return ledger, ownership, kis_reconcile, sentinel, kis_orders


def _ack(ledger, key: str, symbol: str, *, qty: int = 5, before=11,
         odno: str = "0000012345", age_s: float = 601, side: str = "SELL"):
    ledger._append({
        "ev": "submit", "key": key, "symbol": symbol, "intended": qty,
        "filled": 0, "state": "submitted", "reason": "test",
        "meta": {"side": side, "market": "US", "hldg_before": before,
                 "price": 100.0}, "ts": time.time() - age_s,
    })
    ledger.bind_broker_order(key, odno, ord_tmd="101500")
    ledger.on_result(key, "ack", 0)
    return {"key": key, **ledger.state_of(key)}


def _row(symbol: str, *, odno: str = "12345", filled: int = 5,
         opened: bool = False, status: str = "") -> dict:
    return {"odno": odno, "pdno": symbol, "side": "SELL", "ord_qty": 5,
            "filled": filled, "price": 101.0, "src": "ccnl",
            "open": opened, "broker_status": status}


def test_c1_sell_uses_total_for_baseline_and_sellable_for_clamp():
    with tempfile.TemporaryDirectory() as tmp:
        _L, _O, _R, S, _KO = _setup(tmp)
        br = object.__new__(S._KisBroker)
        br.quote = lambda *_: 100.0
        with mock.patch.object(S, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.holding_quantities", return_value={
                 "total": {"INGR": 11}, "sellable": {"INGR": 6},
                 "symbol_total": 11}), \
             mock.patch("bot.kis_orders.place_sell",
                        return_value={"act": "ack"}) as place:
            out = br.place_sell("INGR", 8, "익절", "sell:ingr#1")
        assert out["qty"] == 6
        assert place.call_args.args[2] == 6                 # 전송 clamp는 매도가능
        assert place.call_args.kwargs["hldg_before"] == 11  # delta 기준은 총보유

        # 총보유만 불신이어도 보호 매도는 계속, before는 추측하지 않는다.
        with mock.patch.object(S, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.holding_quantities", return_value={
                 "total": None, "sellable": {"INGR": 6},
                 "symbol_total": None}), \
             mock.patch("bot.kis_orders.place_sell",
                        return_value={"act": "ack"}) as place2:
            assert br.place_sell("INGR", 5, "손절", "sell:ingr#2")["state"] == "ack"
        assert place2.call_args.kwargs["hldg_before"] is None

        with mock.patch.object(S, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.holding_quantities", return_value={
                 "total": {"INGR": 11}, "sellable": None,
                 "symbol_total": 11}), \
             mock.patch("bot.kis_orders.place_sell") as blocked:
            assert br.place_sell("INGR", 5, "손절", "sell:ingr#3")["state"] \
                == "rejected"
        assert not blocked.called

        # 다른 행 하나의 total이 손상돼 시장 map은 None이어도, 정상 INGR 행의
        # 단일심볼 total은 발주 hldg_before에 기록한다.
        with mock.patch.object(S, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.holding_quantities", return_value={
                 "total": None, "sellable": {"INGR": 6, "BROKEN": 2},
                 "symbol_total": 11}), \
             mock.patch("bot.kis_orders.place_sell",
                        return_value={"act": "ack"}) as isolated:
            assert br.place_sell("INGR", 5, "손절", "sell:ingr#4")["state"] == "ack"
        assert isolated.call_args.kwargs["hldg_before"] == 11
    print("[PASS] C1 총보유 11/매도가능 6: before=11·전송≤6·부분실패 단위 격리")


def test_c1_balance_table_no_fill_then_full_fill():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        _ack(L, "sell:ingr", "INGR", before=11)
        # 미체결: 11→11 delta=0. 예전 before=6이면 -5 오동결이었다.
        assert R.resolve_acks_by_balance(
            {"US": {"INGR": 11}}, complete_snapshot=True) == []
        assert not O.is_frozen("INGR") and L.state_of("sell:ingr")["state"] == "ack"
        with mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}):
            result = R.resolve_acks_by_balance(
                {"US": {"INGR": 6}}, complete_snapshot=True)
        assert len(result) == 1 and result[0]["state"] == "filled"
    print("[PASS] C1 영향표: 미체결 delta0 보류·full-fill delta5 정상 확정")


def test_c2_frozen_allows_only_exact_direct_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        order = _ack(L, "sell:direct", "INGR")
        O.freeze("INGR", "C1 old mismatch")
        with mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}) as sync:
            out = R.resolve_acks_from_rows([_row("INGR")])
        assert len(out) == 1 and out[0]["state"] == "filled" and sync.call_count == 1
        assert O.is_frozen("INGR")              # 자동확정은 동결 해제가 아님(operator만)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        evidence = {"market": "US", "nccs": [], "ccnl": [],
                    "holdings": {"INGR": 6}, "rows": [_row("INGR")]}
        with mock.patch.object(CLI, "_read_market", return_value=evidence):
            released = CLI.apply_plan("sell:direct", ack="체결행 재확인")
        assert released["unfrozen"] and not O.is_frozen("INGR")

        # 잔고 delta와 부재 추론은 동결 예외가 아니다.
        order2 = _ack(L, "sell:delta", "DELTA", before=5, odno="22222")
        O.freeze("DELTA", "external change")
        assert R.resolve_acks_by_balance(
            {"US": {}}, complete_snapshot=True) == []
        proof = {order2["key"]: {"nccs_rows": [], "ccnl_rows": [],
                                 "holdings": {"DELTA": 5}}}
        assert R.resolve_acks_by_absence(proof, orders=L.open_orders())[0] == []
    print("[PASS] C2 동결: exact ODNO 체결+회계만 허용·delta/부재 추론 차단")


def test_c2_baseline_and_multiple_orders_still_block_direct():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        O.capture_baseline([{"ovrs_pdno": "USER"}])
        _ack(L, "sell:user", "USER", odno="33333")
        O.freeze("USER", "operator")
        with mock.patch("bot.kis_accounting.sync_fill") as sync:
            assert R.resolve_acks_from_rows([_row("USER", odno="33333")]) == []
        assert not sync.called and L.state_of("sell:user")["state"] == "ack"

        # 깨끗한 심볼도 broker in-flight 두 건이면 exact ODNO 하나로 귀속하지 않는다.
        _ack(L, "sell:multi1", "MULTI", odno="44441")
        _ack(L, "sell:multi2", "MULTI", odno="44442")
        with mock.patch("bot.kis_accounting.sync_fill") as sync2:
            assert R.resolve_acks_from_rows(
                [_row("MULTI", odno="44441")]) == []
        assert not sync2.called
    print("[PASS] C2 사용자 baseline·동일심볼 다중 in-flight는 direct 증거도 보류")


def test_c2_unknown_frozen_exact_bound_odno_only():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        _ack(L, "unknown:exact", "EXACT", odno="77777")
        L.on_result("unknown:exact", "unknown", 0, open_order=True)
        O.freeze("EXACT", "manual review")
        raw = {"rt_cd": "0", "output": [{
            "odno": "77777", "pdno": "EXACT", "ft_ord_qty": "5",
            "ft_ccld_qty": "5", "ft_ccld_unpr3": "101",
            "sll_buy_dvsn_cd": "01", "ord_tmd": "101500"}]}
        with mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}):
            result = R.reconcile_unknowns({"rt_cd": "0", "output": []}, raw)
        assert result[0]["state"] == "filled" and result[0]["candidates"] == 1

        # ODNO 미결속 합성 후보는 유일해도 동결 상태에서 추론 확정하지 않는다.
        _ack(L, "unknown:synthetic", "SYNTH", odno="88888")
        # 결속을 제거할 수 없으므로 별도 원장키는 bind 이벤트 없는 UNKNOWN으로 생성.
        L._append({"ev": "submit", "key": "unknown:unbound", "symbol": "UNBOUND",
                   "intended": 5, "filled": 0, "state": "submitted",
                   "meta": {"side": "SELL", "market": "US", "hldg_before": 5},
                   "ts": time.time() - 601})
        L.on_result("unknown:unbound", "unknown", 0, open_order=True)
        O.freeze("UNBOUND", "manual review")
        raw2 = {"rt_cd": "0", "output": [{
            "odno": "99999", "pdno": "UNBOUND", "ft_ord_qty": "5",
            "ft_ccld_qty": "5", "ft_ccld_unpr3": "101",
            "sll_buy_dvsn_cd": "01"}]}
        with mock.patch("bot.kis_accounting.sync_fill") as sync:
            out2 = R.reconcile_unknowns({"rt_cd": "0", "output": []}, raw2)
        unbound = [row for row in out2 if row["key"] == "unknown:unbound"][0]
        assert unbound["state"] == "unknown" and unbound["candidates"] == 0
        assert not sync.called
    print("[PASS] C2 UNKNOWN 동결: 결속 exact ODNO만 허용·합성 유일후보 차단")


def test_c3_response_last_status_and_secret_redaction():
    with tempfile.TemporaryDirectory() as tmp:
        L, _O, R, _S, KO = _setup(tmp)
        os.environ["KIS_MOCK_APPSECRET"] = "TOPSECRET123"
        _ack(L, "sell:reason", "REASON", odno="55555")
        KO._record_broker_response(
            "sell:reason", {"msg_cd": "APBK0013",
                            "msg1": "접수 account=12345678 order 87654321 TOPSECRET123"},
            source="order-response")
        # 0체결 open 행은 종결하지 않고 마지막 상태만 보존한다.
        assert R.resolve_acks_from_rows([
            _row("REASON", odno="55555", filled=0, opened=True,
                 status="접수 대기")]) == []
        state = L.state_of("sell:reason")
        meta = state["reconcile_meta"]
        assert meta["submit_msg_cd"] == "APBK0013"
        assert "TOPSECRET123" not in meta["submit_msg1"]
        assert "12345678" not in meta["submit_msg1"]
        assert "87654321" not in meta["submit_msg1"]
        assert meta["last_status"] == "접수 대기"

        proof = {"sell:reason": {"nccs_rows": [], "ccnl_rows": [],
                                  "holdings": {"REASON": 11}}}
        resolved, _ = R.resolve_acks_by_absence(proof, orders=L.open_orders())
        assert resolved[0]["broker_reason"] == "사유 미상(마지막 관측: 접수 대기)"
    print("[PASS] C3 접수응답+마지막 nccs 상태 보존·비밀/긴 식별자 제거·종결 사유")


def test_operator_cli_query_failure_no_write_and_exact_apply():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, _R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        L.LEDGER_PATH = os.environ["ORDER_LEDGER_PATH"]
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "sell:cli", "CLI", odno="66666")
        O.freeze("CLI", "manual review")
        ledger_before = open(L.LEDGER_PATH, "rb").read()
        freeze_before = open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read()
        try:
            CLI.apply_plan("sell:cli", ack="")
            raise AssertionError("empty operator ack must fail")
        except PermissionError:
            pass
        assert open(L.LEDGER_PATH, "rb").read() == ledger_before
        assert open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read() == freeze_before
        with mock.patch.object(CLI, "_read_market",
                               side_effect=RuntimeError("query failed")):
            try:
                CLI.collect_plan("sell:cli")
                raise AssertionError("query failure must fail")
            except RuntimeError:
                pass
        assert open(L.LEDGER_PATH, "rb").read() == ledger_before
        assert open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read() == freeze_before

        evidence = {"market": "US", "nccs": [], "ccnl": [],
                    "holdings": {"CLI": 6}, "rows": [_row("CLI", odno="66666")]}
        with mock.patch.object(CLI, "_read_market", return_value=evidence), \
             mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}):
            out = CLI.apply_plan("sell:cli", ack="운영자 ODNO 확인")
        state = L.state_of("sell:cli")
        assert out["result"] == "filled" and out["unfrozen"]
        assert state["last_operator_action"]["action"] == "ack-resolve"
        assert not O.is_frozen("CLI")
    print("[PASS] C2 CLI 조회실패 쓰기0·fresh exact apply·감사 append·terminal 동결해제")


def test_r1_before_unknown_operator_absence_only():
    """before=None은 자동 3경로 정지, 운영자 fresh 부재 증명만 종결한다."""
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "sell:unknown-before", "AMD", before=None, odno="91001")
        O.freeze("AMD", "before unknown")

        # 자동 direct/balance/absence는 모두 계속 보류한다.
        assert R.resolve_acks_from_rows([]) == []
        assert R.resolve_acks_by_balance(
            {"US": {"AMD": 11}}, complete_snapshot=True) == []
        proof = {"sell:unknown-before": {
            "nccs_rows": [], "ccnl_rows": [], "holdings": {"AMD": 11}}}
        assert R.resolve_acks_by_absence(
            proof, orders=L.open_orders())[0] == []

        evidence = {"market": "US", "nccs": [], "ccnl": [],
                    "holdings": {"AMD": 11}, "rows": []}
        with mock.patch.object(CLI, "_read_market", return_value=evidence):
            plan = CLI.collect_plan("sell:unknown-before")
        assert plan["kind"] == "absence-reject" and plan["before_unknown"] is True
        safe = CLI.safe_plan(plan)
        assert "key" not in safe and "filled" not in safe

        with mock.patch.object(CLI, "_read_market", return_value=evidence), \
             mock.patch("bot.kis_accounting.sync_fill") as accounting:
            result = CLI.apply_plan(
                "sell:unknown-before", ack="완전 부재와 fresh 잔고 확인")
        assert result["result"] == "rejected" and result["unfrozen"]
        assert not accounting.called
        events = [json.loads(line) for line in open(L.LEDGER_PATH, encoding="utf-8")]
        audit = [event for event in events if event.get("ev") == "operator_action"]
        assert len(audit) == 2
        assert all(event["evidence"]["before_unknown"] is True for event in audit)
    print("[PASS] R1-a before=None: 자동3경로 0·운영자 부재종결·감사 true·회계0")


def test_r1_before_unknown_refuses_untrusted_or_present_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, _R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "sell:unknown-refuse", "AMD", before=None, odno="92001")
        O.freeze("AMD", "before unknown")
        ledger_before = open(L.LEDGER_PATH, "rb").read()
        freeze_before = open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read()
        with mock.patch.object(CLI, "_read_market",
                               side_effect=RuntimeError("balance untrusted")):
            try:
                CLI.apply_plan("sell:unknown-refuse", ack="조회 확인")
                raise AssertionError("untrusted evidence must fail")
            except RuntimeError:
                pass
        assert open(L.LEDGER_PATH, "rb").read() == ledger_before
        assert open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read() == freeze_before

        zero_row = _row("AMD", odno="92001", filled=0, opened=False)
        present = {"market": "US", "nccs": [],
                   "ccnl": [{"odno": "92001"}], "holdings": {"AMD": 11},
                   "rows": [zero_row]}
        with mock.patch.object(CLI, "_read_market", return_value=present):
            assert CLI.collect_plan("sell:unknown-refuse")["kind"] == "hold"

        _ack(L, "sell:partial-before", "PARTIAL0", qty=5, before=None,
             odno="92002")
        L.on_result("sell:partial-before", "partial", 1, open_order=True)
        partial_absent = {"market": "US", "nccs": [], "ccnl": [],
                          "holdings": {"PARTIAL0": 4}, "rows": []}
        with mock.patch.object(CLI, "_read_market", return_value=partial_absent):
            assert CLI.collect_plan("sell:partial-before")["kind"] == "hold"

        _ack(L, "sell:multi-before-1", "MULTI0", before=None, odno="92003")
        _ack(L, "sell:multi-before-2", "MULTI0", before=None, odno="92004")
        try:
            CLI.collect_plan("sell:multi-before-1")
            raise AssertionError("same-symbol multi order must fail")
        except RuntimeError:
            pass
    print("[PASS] R1-a before=None: 조회불신/ccnl/partial/다중주문은 종결 거부")


def test_r2_numeric_msg_code_visible_but_account_stays_redacted():
    with tempfile.TemporaryDirectory() as tmp:
        L, _O, R, _S, _KO = _setup(tmp)
        from bot import kis_telegram
        _ack(L, "buy:numeric-code", "OMCL", before=0, odno="93001", side="BUY")
        L.record_reconcile_meta(
            "buy:numeric-code", reason="broker-observation",
            meta={"last_msg_cd": "40570000",
                  "last_msg1": "주문가능금액 부족 account=12345678"})
        state = {"key": "buy:numeric-code", **L.state_of("buy:numeric-code")}
        line = kis_telegram._diag_order_details({"buy:numeric-code": state})[0]
        assert "40570000" in line and "12345678" not in line
        proof = {"buy:numeric-code": {
            "nccs_rows": [], "ccnl_rows": [], "holdings": {"OMCL": 0}}}
        resolved, _ = R.resolve_acks_by_absence(proof, orders=L.open_orders())
        assert "40570000" in resolved[0]["broker_reason"]
        assert "12345678" not in resolved[0]["broker_reason"]

        _ack(L, "buy:submit-code", "OMC2", before=0, odno="93002", side="BUY")
        L.record_reconcile_meta(
            "buy:submit-code", reason="order-response",
            meta={"submit_msg_cd": "40910000",
                  "submit_msg1": "접수 거절 account=87654321"})
        proof2 = {"buy:submit-code": {
            "nccs_rows": [], "ccnl_rows": [], "holdings": {"OMC2": 0}}}
        resolved2, _ = R.resolve_acks_by_absence(
            proof2, orders=L.open_orders("OMC2"))
        assert "40910000" in resolved2[0]["broker_reason"]
        assert "87654321" not in resolved2[0]["broker_reason"]
    print("[PASS] R2 숫자 msg_cd 진단/종결 보존·같은 줄 계좌번호 마스킹")


def test_p3_armed_terminal_and_observation_contracts():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        # baseline 미무장은 exact ODNO도 확정하지 않는다.
        os.unlink(os.environ["USER_BASELINE_PATH"])
        _ack(L, "sell:unarmed", "UNARMED", odno="94001")
        with mock.patch("bot.kis_accounting.sync_fill") as sync:
            assert R.resolve_acks_from_rows(
                [_row("UNARMED", odno="94001")]) == []
        assert not sync.called

    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "sell:partial", "PARTIAL", qty=5, odno="95001")
        O.freeze("PARTIAL", "manual review")
        partial_row = _row("PARTIAL", odno="95001", filled=2, opened=True)
        evidence = {"market": "US", "nccs": [], "ccnl": [],
                    "holdings": {"PARTIAL": 9}, "rows": [partial_row]}
        with mock.patch.object(CLI, "_read_market", return_value=evidence), \
             mock.patch("bot.kis_accounting.sync_fill", return_value={"ok": True}):
            result = CLI.apply_plan("sell:partial", ack="부분체결 확인")
        assert result["result"] == "partial" and not result["unfrozen"]
        assert O.is_frozen("PARTIAL")

        order = L.open_orders("PARTIAL")[0]
        row = _row("PARTIAL", odno="95001", filled=2, opened=True,
                   status="부분체결 대기")
        R._record_broker_observation(order, row)
        once = open(L.LEDGER_PATH, "rb").read()
        refreshed = L.open_orders("PARTIAL")[0]
        R._record_broker_observation(refreshed, row)
        assert open(L.LEDGER_PATH, "rb").read() == once

        try:
            L.record_reconcile_meta("", reason="bad", meta={"source": "bad"})
            raise AssertionError("empty key must fail")
        except ValueError:
            pass
    print("[PASS] P3 armed·terminal-only unfreeze·관측 dedup·빈 key 거부")


def _zero_fill_evidence(symbol: str, odno: str, *, current: int,
                        duplicates: int = 1, nccs: bool = False) -> dict:
    raw = {
        "odno": odno, "pdno": symbol, "ft_ord_qty": "5",
        "ft_ccld_qty": "0", "ft_ccld_unpr3": "0",
        "sll_buy_dvsn_cd_name": "매도", "prcs_stat_name": "",
    }
    return {
        "market": "US",
        "nccs": [dict(raw, nccs_qty="5")] if nccs else [],
        "ccnl": [dict(raw) for _ in range(duplicates)],
        "holdings": {symbol: current},
        "rows": [_row(symbol, odno=odno, filled=0, opened=False)],
    }


def test_f1_f2_operator_zero_fill_stale_before_ingr_fixture():
    """INGR 실물: before=6·현재=11·단일 0체결행을 운영자만 종결."""
    with tempfile.TemporaryDirectory() as tmp:
        L, O, R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        order = _ack(L, "xe:INGR:half:2026-08-11#2", "INGR",
                     before=6, odno="0000096001", age_s=601)
        O.freeze("INGR", "stale sellable hldg_before")
        evidence = _zero_fill_evidence(
            "INGR", "0000096001", current=11)

        # 완화는 운영자 경로뿐이다. 자동 direct/balance/absence는 모두 0건.
        assert R.resolve_acks_from_rows(evidence["rows"]) == []
        assert R.resolve_acks_by_balance(
            {"US": {"INGR": 11}}, complete_snapshot=True) == []
        proof = {order["key"]: {
            "nccs_rows": evidence["nccs"], "ccnl_rows": evidence["ccnl"],
            "holdings": evidence["holdings"]}}
        assert R.resolve_acks_by_absence(
            proof, orders=L.open_orders())[0] == []

        with mock.patch.object(CLI, "_read_market", return_value=evidence):
            plan = CLI.collect_plan(order["key"])
        assert plan["kind"] == "operator-zero-fill" and plan["resolvable"]
        assert plan["zero_fill_proof"] is True
        assert plan["hldg_before_recorded"] == 6
        assert plan["hldg_now_observed"] == 11
        safe = CLI.safe_plan(plan)
        assert "hldg_before_recorded" not in safe and "hldg_now_observed" not in safe

        with mock.patch.object(CLI, "_read_market", return_value=evidence), \
             mock.patch("bot.kis_accounting.sync_fill") as accounting:
            result = CLI.apply_plan(order["key"], ack="INGR 0체결행 운영자 확인")
        assert result["result"] == "rejected" and result["unfrozen"]
        assert not accounting.called and not O.is_frozen("INGR")
        events = [json.loads(line) for line in open(L.LEDGER_PATH, encoding="utf-8")]
        audit = [event for event in events if event.get("ev") == "operator_action"]
        assert len(audit) == 2
        for event in audit:
            ev = event["evidence"]
            assert ev["zero_fill_proof"] is True
            assert ev["hldg_before_recorded"] == 6
            assert ev["hldg_now_observed"] == 11
    print("[PASS] F1/F2 INGR stale-before: 운영자 zero-fill 종결·자동3경로0·회계0·감사")


def test_operator_zero_fill_refuses_ambiguous_or_unsafe_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, _R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH

        _ack(L, "sell:zero", "ZERO", before=6, odno="97001", age_s=601)
        O.freeze("ZERO", "review")
        good = _zero_fill_evidence("ZERO", "97001", current=11)

        # 양수 체결은 기존 direct-fill 경로이고 zero-fill 완화가 아니다.
        positive = {**good, "ccnl": [{
            **good["ccnl"][0], "ft_ccld_qty": "5",
            "ft_ccld_unpr3": "101"}],
            "rows": [_row("ZERO", odno="97001", filled=5)]}
        with mock.patch.object(CLI, "_read_market", return_value=positive):
            assert CLI.collect_plan("sell:zero")["kind"] == "direct-fill"

        # 같은 ODNO 0체결 2행은 모호, nccs에 살아 있어도 거부.
        for bad in (
            _zero_fill_evidence("ZERO", "97001", current=11, duplicates=2),
            _zero_fill_evidence("ZERO", "97001", current=11, nccs=True),
            {**_zero_fill_evidence("ZERO", "97001", current=11),
             "holdings": {"ZERO": "not-a-number"}},
        ):
            with mock.patch.object(CLI, "_read_market", return_value=bad):
                assert CLI.collect_plan("sell:zero")["kind"] == "hold"

        submitted = L.state_of("sell:zero")["submitted_at"]
        with mock.patch.object(CLI, "_read_market", return_value=good):
            assert CLI.collect_plan(
                "sell:zero", now_ts=submitted + 599)["kind"] == "hold"
            assert CLI.collect_plan(
                "sell:zero", now_ts=submitted + 601)["kind"] \
                == "operator-zero-fill"

        # 조회 실패는 원장/동결 바이트 무변경.
        ledger_before = open(L.LEDGER_PATH, "rb").read()
        freeze_before = open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read()
        with mock.patch.object(CLI, "_read_market",
                               side_effect=RuntimeError("untrusted")):
            try:
                CLI.apply_plan("sell:zero", ack="조회 확인")
                raise AssertionError("query failure must fail")
            except RuntimeError:
                pass
        assert open(L.LEDGER_PATH, "rb").read() == ledger_before
        assert open(os.environ["SYMBOL_FREEZE_PATH"], "rb").read() == freeze_before

        # BUY, partial, cancel_pending, 동일심볼 2건은 전부 거부.
        _ack(L, "buy:zero", "BUY0", before=0, odno="97002",
             age_s=601, side="BUY")
        buy_evidence = _zero_fill_evidence("BUY0", "97002", current=0)
        with mock.patch.object(CLI, "_read_market", return_value=buy_evidence):
            assert CLI.collect_plan("buy:zero")["kind"] == "hold"

        _ack(L, "sell:partial-zero", "PART0", before=6, odno="97003")
        L.on_result("sell:partial-zero", "partial", 1, open_order=True)
        part_evidence = _zero_fill_evidence("PART0", "97003", current=10)
        with mock.patch.object(CLI, "_read_market", return_value=part_evidence):
            assert CLI.collect_plan("sell:partial-zero")["kind"] == "hold"

        _ack(L, "sell:cancel-zero", "CXL0", before=6, odno="97004")
        L.on_result("sell:cancel-zero", "cancel_pending", 0, open_order=True)
        cancel_evidence = _zero_fill_evidence("CXL0", "97004", current=11)
        with mock.patch.object(CLI, "_read_market", return_value=cancel_evidence):
            assert CLI.collect_plan("sell:cancel-zero")["kind"] == "hold"

        _ack(L, "sell:multi-zero-1", "MZ", before=6, odno="97005")
        _ack(L, "sell:multi-zero-2", "MZ", before=6, odno="97006")
        with mock.patch.object(CLI, "_read_market", return_value=
                               _zero_fill_evidence("MZ", "97005", current=11)):
            try:
                CLI.collect_plan("sell:multi-zero-1")
                raise AssertionError("same-symbol multi must fail")
            except RuntimeError:
                pass
    print("[PASS] operator-zero-fill: 양수/중복/nccs/599s/BUY/partial/cancel/multi/조회실패 방어")


def test_operator_zero_fill_requires_armed_nonbaseline_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        L, O, _R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "sell:baseline-zero", "USER0", before=6, odno="98001")
        O.capture_baseline([{"ovrs_pdno": "USER0"}])
        evidence = _zero_fill_evidence("USER0", "98001", current=11)
        with mock.patch.object(CLI, "_read_market", return_value=evidence):
            try:
                CLI.collect_plan("sell:baseline-zero")
                raise AssertionError("baseline must fail")
            except RuntimeError:
                pass

        os.unlink(os.environ["USER_BASELINE_PATH"])
        _ack(L, "sell:unarmed-zero", "UNARM0", before=6, odno="98002")
        with mock.patch.object(CLI, "_read_market", return_value=
                               _zero_fill_evidence("UNARM0", "98002", current=11)):
            try:
                CLI.collect_plan("sell:unarmed-zero")
                raise AssertionError("unarmed must fail")
            except RuntimeError:
                pass
    print("[PASS] operator-zero-fill: 사용자 baseline·미무장 소유경계 거부")


def test_operator_zero_fill_branch_is_sell_only():
    """G2 mutation guard: 새 zero-fill 분기에서 SELL 제한 제거를 즉시 검출한다."""
    with tempfile.TemporaryDirectory() as tmp:
        L, _O, _R, _S, _KO = _setup(tmp)
        from scripts import kis_ack_resolve as CLI
        importlib.reload(CLI)
        CLI.ledger.LEDGER_PATH = L.LEDGER_PATH
        _ack(L, "buy:zero-fill-mutation", "BUY0", before=0,
             odno="99001", age_s=601, side="BUY")
        evidence = _zero_fill_evidence("BUY0", "99001", current=0)
        with mock.patch.object(CLI, "_read_market", return_value=evidence):
            plan = CLI.collect_plan("buy:zero-fill-mutation")
        assert plan["side"] == "BUY"
        assert plan["kind"] == "hold" and not plan["resolvable"]
        assert not plan["zero_fill_proof"]
    print("[PASS] G2 BUY+단일0체결행은 operator-zero-fill 불가")


def main():
    test_c1_sell_uses_total_for_baseline_and_sellable_for_clamp()
    test_c1_balance_table_no_fill_then_full_fill()
    test_c2_frozen_allows_only_exact_direct_evidence()
    test_c2_baseline_and_multiple_orders_still_block_direct()
    test_c2_unknown_frozen_exact_bound_odno_only()
    test_c3_response_last_status_and_secret_redaction()
    test_operator_cli_query_failure_no_write_and_exact_apply()
    test_r1_before_unknown_operator_absence_only()
    test_r1_before_unknown_refuses_untrusted_or_present_evidence()
    test_r2_numeric_msg_code_visible_but_account_stays_redacted()
    test_p3_armed_terminal_and_observation_contracts()
    test_f1_f2_operator_zero_fill_stale_before_ingr_fixture()
    test_operator_zero_fill_refuses_ambiguous_or_unsafe_evidence()
    test_operator_zero_fill_requires_armed_nonbaseline_ownership()
    test_operator_zero_fill_branch_is_sell_only()
    print("\nACK 단위 불일치/동결 자기잠금/거절 사유 회귀 통과.")


if __name__ == "__main__":
    main()
