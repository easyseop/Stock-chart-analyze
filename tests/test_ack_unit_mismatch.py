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
                 "total": {"INGR": 11}, "sellable": {"INGR": 6}}), \
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
                 "total": None, "sellable": {"INGR": 6}}), \
             mock.patch("bot.kis_orders.place_sell",
                        return_value={"act": "ack"}) as place2:
            assert br.place_sell("INGR", 5, "손절", "sell:ingr#2")["state"] == "ack"
        assert place2.call_args.kwargs["hldg_before"] is None

        with mock.patch.object(S, "LIVE", True), \
             mock.patch("bot.kis.market_of_symbol", return_value="US"), \
             mock.patch("bot.kis.us_excg_of", return_value="NYSE"), \
             mock.patch("bot.kis.holding_quantities", return_value={
                 "total": {"INGR": 11}, "sellable": None}), \
             mock.patch("bot.kis_orders.place_sell") as blocked:
            assert br.place_sell("INGR", 5, "손절", "sell:ingr#3")["state"] \
                == "rejected"
        assert not blocked.called
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


def main():
    test_c1_sell_uses_total_for_baseline_and_sellable_for_clamp()
    test_c1_balance_table_no_fill_then_full_fill()
    test_c2_frozen_allows_only_exact_direct_evidence()
    test_c2_baseline_and_multiple_orders_still_block_direct()
    test_c2_unknown_frozen_exact_bound_odno_only()
    test_c3_response_last_status_and_secret_redaction()
    test_operator_cli_query_failure_no_write_and_exact_apply()
    print("\nACK 단위 불일치/동결 자기잠금/거절 사유 회귀 통과.")


if __name__ == "__main__":
    main()
