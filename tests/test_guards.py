"""자가 감시 가드 검증 — '이상 발생 전/즉시' 대비 (사용자 지적).

  ① autopaper: 규칙/비중 위반 감지 시 텔레그램 즉시 경보 + 당일 중복 억제
  ② advisor: 하루 총량 서킷브레이커(회당 상한이 깨져도 누적 폭주 차단)
  ③ advisor: 장 닫힌 시장은 전송 0건(전송 직전 재확인)

실행: python -m tests.test_guards
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner.autopaper as ap
from bot import advisor
from bot import settings as cfg


def test_violation_escalation() -> list[str]:
    """위반 주입 → 경보 1회, 같은 위반 재빌드 → 침묵, 위반 바뀌면 재경보."""
    sent = []
    import bot.notify as notify
    notify.send = lambda text, **kw: sent.append(text) or True   # 캡처

    st = {"pos": {}, "pending": {}, "log": []}
    v1 = [{"code": "AAA", "name": "위반주", "pct": 50.0}]
    ap._report_violations(st, v1, [])
    ap._report_violations(st, v1, [])                       # 같은 위반 — 침묵해야
    if len(sent) != 1:
        return [f"중복 억제 실패: {len(sent)}회 발송(기대 1)"]
    if "규칙 위반" not in sent[0] or "AAA" not in sent[0]:
        return [f"경보 내용 이상: {sent[0]}"]
    ap._report_violations(st, v1, ["하루 신규 진입 5건 > 상한 3"])   # 위반 추가 → 재경보
    if len(sent) != 2:
        return [f"위반 변경 시 재경보 실패: {len(sent)}회"]
    print(f"  [PASS] 위반 경보: 1회 발송·중복 억제·변경 시 재발송 ({len(sent)}회)")
    return []


def test_daily_budget() -> list[str]:
    """오늘 누적이 예산에 가까우면 회당 상한보다 먼저 걸려 총량이 안 넘음."""
    orig_open = cfg.market_open
    cfg.market_open = lambda ccy: True
    try:
        state = {"day_sent": {cfg.today_kst(): cfg.DAILY_ALERT_BUDGET - 2}}
        sigs = [{"group": "now", "id": f"S{i}", "code": f"S{i}", "name": f"종목{i}",
                 "ccy": "USD", "entry": 100, "stop": 95, "fresh": True,
                 "stage": 3, "norm": 50, "tactic": {}, "earnings_d": None}
                for i in range(8)]                          # 8개 신호(회당 상한=8)
        budget = max(0, cfg.DAILY_ALERT_BUDGET - advisor._sent_today(state))
        nb = advisor.check_buy_signals(sigs, state, dry_run=True, budget=budget)
        if nb != 2:
            return [f"예산 캡 실패: {nb}건 발송(잔여 예산 2 기대)"]
        advisor._record_sent(state, nb)
        if advisor._sent_today(state) != cfg.DAILY_ALERT_BUDGET:
            return [f"누적 집계 오류: {advisor._sent_today(state)}"]
        print(f"  [PASS] 일일 서킷브레이커: 8신호 중 잔여예산 {nb}건만 발송")
        return []
    finally:
        cfg.market_open = orig_open


def test_market_closed_silence() -> list[str]:
    """장 닫힌 시장 신호는 0건 발송(필터 + 전송 직전 재확인)."""
    orig_open = cfg.market_open
    cfg.market_open = lambda ccy: False                     # 전 시장 닫힘
    try:
        state = {}
        sigs = [{"group": "now", "id": "X", "code": "X", "name": "종목X",
                 "ccy": "USD", "entry": 100, "stop": 95, "fresh": True,
                 "stage": 3, "norm": 50, "tactic": {}, "earnings_d": None}]
        nb = advisor.check_buy_signals(sigs, state, dry_run=True, budget=99)
        if nb != 0:
            return [f"장 닫힘인데 {nb}건 발송"]
        print("  [PASS] 장 닫힘: 발송 0건")
        return []
    finally:
        cfg.market_open = orig_open


def main() -> int:
    fails = []
    print("[①] autopaper 위반 즉시 경보:")
    fails += test_violation_escalation()
    print("[②] advisor 하루 총량 서킷브레이커:")
    fails += test_daily_budget()
    print("[③] advisor 장 닫힘 침묵:")
    fails += test_market_closed_silence()
    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 자가 감시 가드 3종 전부 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
