"""KIS UNKNOWN 대사(nccs+ccnl) 검증 — 리뷰 '위험 1위' 실패 시나리오 재현.

  1) 정규화: nccs(미체결)·ccnl(체결) 병합, 부분체결 유추, ODNO 중복은 ccnl 우선
  2) HIGH: 타임아웃 주문이 ccnl에서 유일 후보로 발견 → 자동 확정·잠금 해제
     (완전체결은 nccs에 안 뜨는 케이스 — ccnl로만 회수)
  3) LOW: 같은 초·같은 종목·같은 수량 주문 2건 → 자동 해소 금지·잠금 유지
  4) 교차 오귀속 방지: 이미 ODNO 결속된 행은 다른 UNKNOWN의 후보에서 제외
  5) 시간 윈도우: ±120초 밖 행은 후보 제외

실행: python -m tests.test_kis_reconcile
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.ledger as L
from bot import kis_reconcile as R


def _fresh(tmp):
    L.LEDGER_PATH = os.path.join(tmp, "order_ledger.jsonl")


def _nccs(rows):
    return {"rt_cd": "0", "output": rows}


def _ccnl(rows):
    return {"rt_cd": "0", "output": rows}


def test_normalize_merge():
    rows = R.normalize_rows(
        _nccs([{"odno": "1001", "pdno": "AAPL", "ft_ord_qty": "10",
                "nccs_qty": "6", "sll_buy_dvsn_cd_name": "매도",
                "ord_tmd": "142200"}]),
        _ccnl([{"odno": "1001", "pdno": "AAPL", "ft_ord_qty": "10",
                "ft_ccld_qty": "4", "ft_ccld_unpr3": "99.50",
                "sll_buy_dvsn_cd_name": "매도", "ord_tmd": "142200"}]))
    assert len(rows) == 1
    r = rows[0]
    assert r["filled"] == 4 and r["src"] == "ccnl" and r["side"] == "SELL"
    assert r["ord_qty"] == 10 and r["price"] == 99.50
    print("[PASS] 정규화: nccs+ccnl 같은 ODNO 병합(ccnl 체결치 우선)·부분체결 유추")


def test_high_fully_filled_found_in_ccnl_only():
    """시나리오(리뷰 A6-2): SELL 타임아웃 → 실제 즉시 완전체결 → nccs엔 없음.
    ccnl에서 유일 후보로 찾아 HIGH 확정·잠금 해제돼야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("p1#1", "AAPL", 7, "손절",
                        meta={"side": "SELL"})
        L.on_result("p1#1", "unknown", 0)        # 응답 유실
        assert L.is_locked("AAPL")
        res = R.reconcile_unknowns(
            _nccs([]),                            # 완전체결이라 미체결에 없음
            _ccnl([{"odno": "2001", "pdno": "AAPL", "ft_ord_qty": "7",
                    "ft_ccld_qty": "7", "ft_ccld_unpr3": "101.2",
                    "sll_buy_dvsn_cd_name": "매도", "ord_tmd": "150001"}]))
        assert len(res) == 1
        r = res[0]
        assert r["confidence"] == L.CONF_HIGH and r["state"] == "filled" \
            and r["residual"] == 0
        assert not L.is_locked("AAPL")            # 해제
        assert L.odno_of("p1#1") == "2001"        # 늦은 ODNO 결속
    print("[PASS] HIGH: 완전체결(nccs 부재)을 ccnl 유일후보로 확정·해제·ODNO 결속")


def test_low_twin_orders_same_second():
    """시나리오(리뷰 A6-1·B5): 같은 종목·같은 수량·같은 시각 주문 2건 중 하나가
    UNKNOWN → 후보 2개 → LOW → 잠금 유지(자동 재주문 금지)."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("p2#1", "TSLA", 1, "손절", meta={"side": "SELL"})
        L.on_result("p2#1", "unknown", 0)
        res = R.reconcile_unknowns(
            _nccs([{"odno": "3001", "pdno": "TSLA", "ft_ord_qty": "1",
                    "nccs_qty": "1", "sll_buy_dvsn_cd_name": "매도",
                    "ord_tmd": "142233"}]),
            _ccnl([{"odno": "3002", "pdno": "TSLA", "ft_ord_qty": "1",
                    "ft_ccld_qty": "1", "sll_buy_dvsn_cd_name": "매도",
                    "ord_tmd": "142233"}]))
        r = res[0]
        assert r["confidence"] == L.CONF_LOW and r["candidates"] == 2
        assert L.is_locked("TSLA")                # 잠금 유지 — 수동 검토
    print("[PASS] LOW: 같은 초 쌍둥이 주문 2후보 → 자동해소 금지·잠금 유지")


def test_known_odno_excluded():
    """이미 ODNO가 결속된(우리가 아는) 주문의 행은 다른 UNKNOWN의 후보에서 제외 →
    남는 1행으로 HIGH 확정."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        # 주문 A: 정상 접수(ODNO 4001 결속), 아직 미체결(in-flight)
        L.record_submit("pa#1", "NVDA", 2, "손절", meta={"side": "SELL"})
        L.bind_broker_order("pa#1", "4001", ord_tmd="150000")
        # 주문 B: 타임아웃 UNKNOWN(같은 종목·같은 수량)
        L.record_submit("pb#1", "NVDA", 2, "손절", meta={"side": "SELL"})
        L.on_result("pb#1", "unknown", 0)
        res = R.reconcile_unknowns(
            _nccs([{"odno": "4001", "pdno": "NVDA", "ft_ord_qty": "2",
                    "nccs_qty": "2", "sll_buy_dvsn_cd_name": "매도",
                    "ord_tmd": "150000"},
                   {"odno": "4002", "pdno": "NVDA", "ft_ord_qty": "2",
                    "nccs_qty": "2", "sll_buy_dvsn_cd_name": "매도",
                    "ord_tmd": "150002"}]),
            _ccnl([]))
        rb = [r for r in res if r["key"] == "pb#1"][0]
        assert rb["confidence"] == L.CONF_HIGH and rb["candidates"] == 1
        assert L.odno_of("pb#1") == "4002"        # 4001(기결속)이 아닌 4002로
    print("[PASS] 교차 오귀속 방지: 기결속 ODNO 제외 → 남는 행으로 HIGH")


def test_time_window_filter():
    """±120초 밖 행은 후보 제외 → 후보 0 → LOW·잠금 유지."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("p5#1", "AMD", 3, "손절", meta={"side": "SELL"})
        # 제출 시각을 원장에 남긴다(ord_tmd) — bind 없이 이벤트로만
        L._append({"ev": "bind", "key": "p5#1", "odno": "",
                   "ord_tmd": "140000"})
        L.on_result("p5#1", "unknown", 0)
        res = R.reconcile_unknowns(
            _nccs([{"odno": "5001", "pdno": "AMD", "ft_ord_qty": "3",
                    "nccs_qty": "3", "sll_buy_dvsn_cd_name": "매도",
                    "ord_tmd": "153000"}]),      # 90분 뒤 — 다른 주문
            _ccnl([]))
        r = res[0]
        assert r["confidence"] == L.CONF_LOW and r["candidates"] == 0
        assert L.is_locked("AMD")
    print("[PASS] 시간 윈도우: ±120초 밖 행 제외 → LOW·잠금 유지")


def main():
    test_normalize_merge()
    test_high_fully_filled_found_in_ccnl_only()
    test_low_twin_orders_same_second()
    test_known_odno_excluded()
    test_time_window_filter()
    print("\n모든 KIS 대사 테스트 통과 — UNKNOWN 복구(HIGH/LOW)·오매칭 방지.")


if __name__ == "__main__":
    main()
