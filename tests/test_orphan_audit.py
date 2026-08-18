"""브로커↔원장 대조 검증 — 고아 적발과 '조회 실패 ≠ 부재' 계약.

실측(2026-08-18): CVNA 74주가 브로커에만 있고 원장에 없어 무보호로 방치됐다.

  1) 브로커에 있고 원장에 없음 → 고아로 적발
  2) 조회 실패가 섞이면 '유령'(원장에만 있음) 판정을 **생략**한다
  3) 전 조회 성공이면 유령도 보고
  4) stop<=0 · 수량 불일치 적발
  5) 이상 없으면 종료코드 0

실행: python -m tests.test_orphan_audit
"""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import kis_orphan_audit as A   # noqa: E402


def _broker(rows, failed=()):
    return (rows, list(failed))


def _patch(rows, failed=(), ledger=None):
    return (mock.patch.object(A, "collect_broker",
                              lambda: _broker(rows, failed)),
            mock.patch("bot.kis_positions.load", lambda: ledger or {}),
            mock.patch("bot.ownership.baseline", lambda: set()),
            mock.patch("bot.ownership.frozen_state", lambda: {}))


def _run(rows, failed=(), ledger=None):
    ps = _patch(rows, failed, ledger)
    for p in ps: p.start()
    try:
        return A.audit()
    finally:
        for p in ps: p.stop()


_CVNA = {"CVNA": {"qty": 74, "avg": 65.03, "price": 66.15, "market": "NASD"}}


def test_orphan_detected():
    rep = _run(_CVNA, ledger={})
    assert [e["code"] for e in rep["orphans"]] == ["CVNA"]
    assert rep["orphans"][0]["broker_qty"] == 74
    print("[PASS] 브로커에만 있는 74주 → 고아 적발")


def test_query_failure_suppresses_ghost_report():
    """조회 실패를 부재로 오독하면 멀쩡한 포지션을 지우게 된다."""
    ledger = {"AAPL": {"qty": 10, "stop": 100.0}}
    rep = _run({}, failed=("NASD",), ledger=ledger)
    assert rep["ghosts"] == [] and rep["ghosts_suppressed"] is True
    assert rep["query_failed"] == ["NASD"]
    print("[PASS] 조회 실패 → 유령 판정 생략(실패 ≠ 부재)")


def test_ghost_reported_when_all_queries_succeed():
    ledger = {"AAPL": {"qty": 10, "stop": 100.0}}
    rep = _run({}, ledger=ledger)
    assert [e["code"] for e in rep["ghosts"]] == ["AAPL"]
    assert rep["ghosts_suppressed"] is False
    print("[PASS] 전 조회 성공 → 유령 보고")


def test_missing_stop_and_qty_mismatch():
    ledger = {"CVNA": {"qty": 50, "stop": 0.0}}
    rep = _run(_CVNA, ledger=ledger)
    assert [e["code"] for e in rep["unprotected"]] == ["CVNA"]
    assert rep["mismatched"][0]["ledger_qty"] == 50
    assert rep["orphans"] == []          # 원장에 있으니 고아는 아님
    print("[PASS] stop<=0 · 수량 불일치 적발")


def test_clean_state_exit_zero():
    ledger = {"CVNA": {"qty": 74, "stop": 60.48}}
    rep = _run(_CVNA, ledger=ledger)
    assert not (rep["orphans"] or rep["unprotected"]
                or rep["mismatched"] or rep["ghosts"])
    assert A.render(rep) == 0
    print("[PASS] 정합 상태 → 종료코드 0")


def main():
    test_orphan_detected()
    test_query_failure_suppresses_ghost_report()
    test_ghost_reported_when_all_queries_succeed()
    test_missing_stop_and_qty_mismatch()
    test_clean_state_exit_zero()
    print("\n고아 포지션 점검 검증 통과 — 적발·부재증명 계약·정합 판정.")


if __name__ == "__main__":
    main()
