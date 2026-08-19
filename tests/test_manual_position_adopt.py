"""미래 수동매수 baseline adopt의 순서·멱등·자동매도 제외 검증."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack
from unittest import mock

from bot import costbook as C
from bot import kis_positions as P
from bot import ownership as O
from bot import sentinel as S
from scripts import kis_arm
from scripts import kis_orphan_audit


def _paths(stack: ExitStack, tmp: str) -> None:
    stack.enter_context(mock.patch.object(P, "PATH", os.path.join(tmp, "positions.jsonl")))
    stack.enter_context(mock.patch.dict(os.environ, {
        "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
        "USER_BASELINE_PATH": os.path.join(tmp, "baseline.json"),
        "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
    }))


def test_adopt_adds_baseline_then_closes_position_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        assert O.capture_baseline([])
        P.record("MANU", qty=5, entry=100, stop=90, ccy="USD", pos_key="bot:wrong")
        cost_before = C._fold()
        assert kis_arm.main(["--adopt", "MANU", "사용자 수동매수 확인"]) == 0
        assert "MANU" in O.baseline() and "MANU" not in P.load()
        assert O.buy_denied("MANU")[0] is True
        base_bytes = open(O.baseline_path(), "rb").read()
        pos_bytes = open(P.PATH, "rb").read()
        assert kis_arm.main(["--adopt", "MANU", "재확인"]) == 0
        assert open(O.baseline_path(), "rb").read() == base_bytes
        assert open(P.PATH, "rb").read() == pos_bytes
        assert C._fold() == cost_before
    print("[PASS] baseline 성공→보호원장 close 순서·2회 byte-idempotent·costbook 0")


def test_adopt_baseline_failure_never_removes_protection():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        with open(O.baseline_path(), "w", encoding="utf-8") as fp:
            fp.write("{broken")
        P.record("MANU", qty=5, entry=100, stop=90, ccy="USD")
        assert kis_arm.main(["--adopt", "MANU", "사람 확인"]) == 2
        assert "MANU" in P.load()
    print("[PASS] baseline 손상/저장 실패 시 보호원장 제거 0")


def test_baseline_position_is_not_orphan_or_sentinel_sell_target():
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        _paths(stack, tmp)
        assert O.capture_baseline([{"ovrs_pdno": "MANU"}])
        with mock.patch.object(kis_orphan_audit, "collect_broker", return_value=({
            "MANU": {"qty": 5, "avg": 100, "price": 80, "market": "NYSE"}}, [])), \
                mock.patch.object(P, "load", return_value={}):
            report = kis_orphan_audit.audit()
        assert report["orphans"] == [] and report["unprotected"] == []

        class Broker:
            name = "kis"
            sells = []
            def holdings(self): return {"MANU": 5}
            def quote(self, code, ccy): return 80
            def place_sell(self, code, qty, reason, key):
                self.sells.append((code, qty)); return True

        broker = Broker()
        old_sent = S.SENT_PATH
        S.SENT_PATH = os.path.join(tmp, "sent.json")
        try:
            with mock.patch.object(S, "_fetch_positions", return_value=([{
                    "code": "MANU", "name": "manual", "ccy": "USD",
                    "q": 5, "stop": 90, "opened": "2026-08-19"}], 0)), \
                    mock.patch.object(S, "_market_open", return_value=True), \
                    mock.patch.object(S, "_notify"), \
                    mock.patch("bot.kis_positions.load", return_value={}), \
                    mock.patch("bot.kis_exits.manage"):
                S.check_once(broker, {})
        finally:
            S.SENT_PATH = old_sent
        assert broker.sells == []
    print("[PASS] baseline 보유는 고아 아님 · 파수꾼 감시/매도 제외")


def main():
    test_adopt_adds_baseline_then_closes_position_and_is_idempotent()
    test_adopt_baseline_failure_never_removes_protection()
    test_baseline_position_is_not_orphan_or_sentinel_sell_target()
    print("\n모든 수동 포지션 adopt 테스트 통과.")


if __name__ == "__main__":
    main()
