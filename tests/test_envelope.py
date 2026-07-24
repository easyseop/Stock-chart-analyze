"""IS3/IS4 시드 봉투·사이징 검증 — 확정 버그 2개가 실제로 고쳐졌는지 재현.

  1) 버그① equity 사이징: 계좌 equity 1억(사용자 9천만 포함)이어도 분모는
     SEED 1천만 → 수량이 equity 기준의 1/10
  2) 버그② 총량 게이트: open_cost가 SEED에 근접하면 symbol_cap이 남아도
     deployable이 물려 초과 투입 불가(1.67× 시나리오 차단)
  3) feasibility는 하향 클램프만(사용자 입금으로 커져도 무시) · 미확인(None)=0
  4) 실현손실 후 bot_cash 바인딩 / 실현이익 후 SEED−open_cost 바인딩
  5) whole-share·SEED 미설정=0주·불변식

실행: python -m tests.test_envelope
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import envelope as E

SEED = 10_000_000.0          # 1천만원


def test_bug1_seed_not_equity():
    # price 10만·주당리스크 5천 → risk_notional=(0.01×1천만/5천)×10만=200만 → 20주
    r = E.size_buy(100_000, 5_000, seed=SEED, deployable_amt=SEED,
                   feasibility=90_000_000)      # 계좌엔 사용자 돈 9천만이 있어도
    assert r.qty == 20 and r.binding == "risk"
    # (버그였다면) equity 1억 분모 → 200주가 나왔을 것
    wrong = E.size_buy(100_000, 5_000, seed=100_000_000,
                       deployable_amt=100_000_000, feasibility=90_000_000)
    assert wrong.qty == 200                      # 대조군 — 분모 차이 10배 확인
    print("[PASS] 버그① 수정: 분모=SEED(20주) — equity(200주) 아님")


def test_bug2_aggregate_gate():
    # 이미 SEED의 90%(900만) 투입 — symbol_cap(333만)이 남아도 deployable(100만)이 문다
    dep = E.deployable(SEED, cash=1_000_000, open_cost=9_000_000)
    assert dep == 1_000_000
    r = E.size_buy(100_000, 5_000, seed=SEED, deployable_amt=dep,
                   feasibility=50_000_000)
    assert r.qty == 10 and r.binding == "deployable"   # 100만원어치만
    # 총량 불변식: open_cost 950만 + 신규 100만 ≤ SEED → OK / 초과 시나리오 차단
    assert E.invariant_ok(SEED, 9_000_000 + r.qty * 100_000)
    print("[PASS] 버그② 수정: 총량 게이트(deployable)가 1.67× 초과투입 차단")


def test_feasibility_downward_only():
    # 매수여력이 작으면 그걸로 클램프(하향)
    r = E.size_buy(100_000, 5_000, seed=SEED, deployable_amt=SEED,
                   feasibility=500_000)
    assert r.qty == 5 and r.binding == "feasibility"
    # 미확인(None)=0 — 주문 직전 재확인 실패면 사이징 불가(보수적)
    r2 = E.size_buy(100_000, 5_000, seed=SEED, deployable_amt=SEED,
                    feasibility=None)
    assert r2.qty == 0
    print("[PASS] feasibility: 하향 클램프만·미확인=0(보수적)")


def test_pnl_binding_directions():
    # 실현손실 50만: bot_cash=950만 < SEED−open_cost(0)=1000만 → cash 바인딩
    cash_after_loss = E.bot_cash(SEED, total_buy_cost=3_000_000,
                                 total_sell_proceeds=2_500_000)
    assert cash_after_loss == 9_500_000
    assert E.deployable(SEED, cash_after_loss, open_cost=0) == 9_500_000
    # 실현이익 50만 + open 400만: SEED−open=600만 < cash 1050만−400만? →
    cash_after_gain = E.bot_cash(SEED, total_buy_cost=7_000_000,
                                 total_sell_proceeds=3_500_000)   # 열린 400만 별도
    assert cash_after_gain == 6_500_000
    assert E.deployable(SEED, cash_after_gain, open_cost=4_000_000) == 6_000_000
    print("[PASS] 실현손실→cash 바인딩 / 실현이익→SEED−open_cost 바인딩")


def test_whole_share_and_guards():
    r = E.size_buy(333_333, 5_000, seed=SEED, deployable_amt=SEED,
                   feasibility=SEED, stage_cap=1_000_000)
    assert r.qty == 3 and r.binding == "stage_cap"     # 100만//33.3만=3주
    os.environ.pop("BOT_SEED_KRW", None)
    r2 = E.size_buy(100_000, 5_000, deployable_amt=SEED, feasibility=SEED)
    assert r2.qty == 0                                  # SEED 미설정=차단
    assert not E.invariant_ok(SEED, SEED + 1)
    print("[PASS] whole-share·stage_cap·SEED 미설정 차단·불변식")


def test_combined_operating_seed_and_buffer():
    old = {k: os.environ.get(k) for k in (
        "BOT_OPERATING_TOTAL_KRW", "BOT_OPERATING_BUFFER_PCT",
        "BOT_SEED_KRW", "BOT_SEED_SB_KRW")}
    try:
        os.environ.update({
            "BOT_OPERATING_TOTAL_KRW": "35000000",
            "BOT_OPERATING_BUFFER_PCT": "0.05",
            "BOT_SEED_KRW": "30000000",
            "BOT_SEED_SB_KRW": "5000000",
        })
        assert E.operating_total_krw() == 35_000_000
        assert E.operating_limit_krw() == 33_250_000
        assert E.sleeve_limit_krw("A") == 28_500_000
        assert E.sleeve_limit_krw("B") == 4_750_000
        assert E.combined_deployable(33_000_000) == 250_000
        assert E.combined_deployable(34_000_000) == 0
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("[PASS] 총시드 3500만 단일진실·5% 완충·A/B 30:5 배분·교차잔여")


def main():
    test_bug1_seed_not_equity()
    test_bug2_aggregate_gate()
    test_feasibility_downward_only()
    test_pnl_binding_directions()
    test_whole_share_and_guards()
    test_combined_operating_seed_and_buffer()
    print("\n모든 봉투/사이징 테스트 통과 — 확정 버그 2개 수정 검증.")


if __name__ == "__main__":
    main()
