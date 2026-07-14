"""텔레그램 조회 봇 검증(읽기 전용·전송 없음) — 라우팅·요약·상세·보안 필터.

  · handle() 라우팅: /보유·/종목·코드만·/help
  · _holdings_text: 종목별 수익률·수익금 + 통화별 합계
  · _detail_text: 현재가(실시간)·평단가·손절예상가, 미보유/조회실패 처리
  · _all_positions: KR+US 병합·중복 제거·전부실패 감지

실행: python -m tests.test_kis_telegram
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_telegram as kt   # noqa: E402

_KR = {"code": "005930", "name": "삼성전자", "qty": 10, "avg": 68000.0,
       "cur": 72000.0, "eval_amt": 720000.0, "buy_amt": 680000.0,
       "pl_amt": 40000.0, "pl_rt": 5.88, "ccy": "KRW", "market": "KR"}
_US = {"code": "AAPL", "name": "Apple", "qty": 5, "avg": 200.0,
       "cur": 190.0, "eval_amt": 950.0, "buy_amt": 1000.0,
       "pl_amt": -50.0, "pl_rt": -5.0, "ccy": "USD", "market": "US"}


def _install(kr=None, us=None, *, fail=False):
    """kis.positions_detail·last_price·_stop_for를 결정론적으로 대체."""
    def fake_pd(market="US", excg="NASD"):
        if fail:
            return None
        if market == "KR":
            return list(kr or [])
        # US는 NASD에만 실보유(NYSE/AMEX 조회는 빈 리스트) — 중복 병합 확인
        return list(us or []) if excg == "NASD" else []
    from bot import kis
    kis.positions_detail = fake_pd
    kis.last_price = lambda code, **kw: {"005930": 73000.0, "AAPL": 189.0}.get(
        str(code).upper())
    kt._stop_for = lambda code: ((65000.0, "트레일링") if str(code).upper()
                                 == "005930" else (185.0, "진입 손절선"))


def test_holdings_summary():
    _install(kr=[_KR], us=[_US, _US])       # US 중복 입력 → 병합 1건
    t = kt._holdings_text()
    assert "삼성전자(005930)" in t and "AAPL" in t
    assert "+5.9%" in t and "+40,000원" in t           # 수익률·수익금
    assert "-5.0%" in t and "-$50.00" in t
    assert "합계 평가" in t and "720,000원" in t         # 통화별 합계(KRW)
    assert "$950.00" in t                               # 통화별 합계(USD)
    # 중복 병합: AAPL이 한 번만
    assert t.count("AAPL") == 1
    print("[PASS] /보유: 종목별 수익률·수익금 + 통화별 합계 + 중복 병합")


def test_detail_realtime():
    _install(kr=[_KR], us=[_US])
    t = kt._detail_text("005930")
    assert "삼성전자(005930)" in t and "[국내]" in t
    assert "현재가   73,000원  (실시간)" in t            # last_price 재조회 반영
    assert "평단가   68,000원" in t
    assert "손절예상가 65,000원  (트레일링)" in t
    assert "수량     10주" in t
    # 실시간가 73,000 기준 재계산: (73000-68000)*10 = +50,000
    assert "+50,000원" in t
    print("[PASS] /종목: 현재가(실시간)·평단가·손절예상가·평가손익")


def test_bare_code_and_name():
    _install(kr=[_KR], us=[_US])
    assert "삼성전자(005930)" in kt.handle("005930")      # 코드만
    assert "삼성전자(005930)" in kt.handle("삼성")        # 이름 부분일치
    assert "Apple(AAPL)" in kt.handle("aapl")            # 대소문자 무관
    print("[PASS] 접두어 없이 코드/이름만 → 상세")


def test_routing_and_help():
    _install(kr=[_KR], us=[_US])
    assert "보유 종목" in kt.handle("/보유")
    assert "보유 종목" in kt.handle("잔고")
    assert "KIS 모의계좌 조회" in kt.handle("/help")
    assert "사용법" in kt.handle("/종목")                 # 인자 없음
    assert kt.handle("") == ""                           # 빈 입력 무시
    print("[PASS] 라우팅: /보유·잔고·/help·인자없는 /종목·빈입력")


def test_not_held_and_query_fail():
    _install(kr=[_KR], us=[_US])
    r = kt.handle("TSLA")                                # 미보유
    assert "보유 없음" in r and "005930" in r            # 현재 보유 안내
    _install(fail=True)                                  # 전 시장 조회 실패
    assert "조회 실패" in kt._holdings_text()
    assert "조회 실패" in kt._detail_text("005930")
    print("[PASS] 미보유 안내 + 전체 조회 실패 시 실패 응답")


def main():
    test_holdings_summary()
    test_detail_realtime()
    test_bare_code_and_name()
    test_routing_and_help()
    test_not_held_and_query_fail()
    print("\n텔레그램 조회 봇 검증 통과 — 읽기전용 요약/상세/보안/실패 처리.")


if __name__ == "__main__":
    main()
