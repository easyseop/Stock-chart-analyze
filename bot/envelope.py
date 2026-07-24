"""IS3/IS4 — 시드 봉투 + 라이브 사이징 (확정 버그 2개의 수정 구현).

버그(07 파일 IS4, 다중에이전트 CONFIRMED — 별도 계좌든 공유든 반드시 수정):
  ① 현행 사이징이 계좌 equity/매수여력 기준 → 공유계좌면 사용자 자산까지 분모가 돼
     봇이 최대 10배 크게 주문. → **분모를 고정 SEED로.**
  ② 총량 게이트 부재 — MAX_POSITIONS(5)×POS_CAP(1/3)=시드의 1.67배까지 투입 가능.
     → **deployable = min(bot_cash, SEED − bot_open_cost)** 게이트 신설.

원칙(IS3·IS4):
  · SEED는 KRW 고정(환경변수 BOT_SEED_KRW). 봇 예산은 SEED+봇 원장 회계로만 산출.
  · 계좌 현금/매수여력(feasibility)은 '예산'이 아니라 **하향 클램프**로만 —
    사용자가 입금/매도해 현금이 늘어도 봇 주문이 커지지 않는다.
  · 불변식: bot_open_cost ≤ SEED (총량 게이트가 항상 보장).
  · 미국주는 fill 시점 환율로 cost_krw 고정(이후 환율은 평가액만 바꿈) — 원가축은
    X1 체결 콜백에서 기록(이 모듈은 계산만).

X1(라이브 매수 실행기)은 반드시 이 모듈의 size_buy()만 사용한다 — equity 금지.
순수 계산 모듈: 브로커 호출·주문 없음.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_RISK_PCT = 0.01          # 포지션당 리스크 1% (of SEED)
DEFAULT_POS_CAP = 1.0 / 3.0      # 종목당 최대 SEED의 1/3
DEFAULT_OPERATING_BUFFER_PCT = 0.05


def seed_krw() -> float:
    """봇 시드(KRW) — BOT_SEED_KRW 환경변수. 미설정=0(라이브 사이징 전면 차단)."""
    try:
        return float(os.environ.get("BOT_SEED_KRW", "0"))
    except ValueError:
        return 0.0


def seed_krw_sb() -> float:
    """슬리브 B(매물대 반등) 전용 시드(KRW) — BOT_SEED_SB_KRW. 미설정=0(B 비활성)."""
    try:
        return float(os.environ.get("BOT_SEED_SB_KRW", "0"))
    except ValueError:
        return 0.0


def operating_total_krw() -> float:
    """A+B가 공유하는 단일 총시드.

    새 배포는 BOT_OPERATING_TOTAL_KRW를 명시한다. 전환 전 환경과 테스트 호환을 위해
    미설정이면 A/B 명목시드 합을 사용하지만, 교차 게이트는 어느 경우든 이 합계 하나만
    본다.
    """
    try:
        explicit = float(os.environ.get("BOT_OPERATING_TOTAL_KRW", "0"))
    except ValueError:
        explicit = 0.0
    return explicit if explicit > 0 else max(0.0, seed_krw()) + max(0.0, seed_krw_sb())


def operating_buffer_pct() -> float:
    try:
        value = float(os.environ.get(
            "BOT_OPERATING_BUFFER_PCT", str(DEFAULT_OPERATING_BUFFER_PCT)))
    except ValueError:
        value = DEFAULT_OPERATING_BUFFER_PCT
    return min(0.25, max(0.0, value))


def operating_limit_krw() -> float:
    """실제 A+B 주문에 쓸 수 있는 총액 = 총시드 − 운영 완충."""
    return operating_total_krw() * (1.0 - operating_buffer_pct())


def sleeve_limit_krw(sleeve: str) -> float:
    """운영한도 안의 A/B 배분. 명목 30:5 비율을 유지하고 합은 운영한도 이하."""
    a = max(0.0, seed_krw())
    b = max(0.0, seed_krw_sb())
    denom = a + b
    if denom <= 0:
        return 0.0
    weight = b / denom if str(sleeve).upper() == "B" else a / denom
    return operating_limit_krw() * weight


def combined_deployable(total_open_cost: float,
                        *, operating_limit: float | None = None) -> float:
    """A+B 확정·예약원가 합계에 대한 교차 게이트 잔여액."""
    limit = operating_limit_krw() if operating_limit is None else float(operating_limit)
    return max(0.0, limit - max(0.0, float(total_open_cost)))


def bot_cash(seed: float, total_buy_cost: float, total_sell_proceeds: float,
             topup: float = 0.0, withdraw: float = 0.0) -> float:
    """봇 현금 = SEED + 입금 − 출금 − Σ매수원가 + Σ매도실현액(실현손익 반영).
    매도는 proceeds(실현액) 환입 — cost 반환이 아님(손실 후 과대계상 방지)."""
    return seed + topup - withdraw - total_buy_cost + total_sell_proceeds


def deployable(seed: float, cash: float, open_cost: float) -> float:
    """신규 투입 가능 총액 = min(bot_cash, SEED − bot_open_cost) — 총량 게이트(버그②).
    · 실현손실 후 → bot_cash 바인딩(시드 줄어 덜 투입)
    · 실현이익 후 → SEED−open_cost 바인딩(footprint를 SEED 초과로 안 키움)"""
    return max(0.0, min(cash, seed - open_cost))


@dataclass
class SizeResult:
    qty: int
    cap_krw: float               # 최종 상한(모든 게이트의 min)
    binding: str                 # 어느 게이트가 물렸나(진단용)
    detail: dict


def size_buy(price_krw: float, per_share_risk_krw: float, *,
             seed: float | None = None,
             open_cost_symbol: float = 0.0,
             deployable_amt: float | None = None,
             feasibility: float | None = None,
             stage_cap: float | None = None,
             risk_pct: float = DEFAULT_RISK_PCT,
             pos_cap: float = DEFAULT_POS_CAP) -> SizeResult:
    """라이브 매수 수량(whole-share) — 분모는 SEED(버그① 수정), equity 금지.

      risk_notional = (risk_pct × SEED / per_share_risk) × price
      symbol_cap    = pos_cap × SEED − open_cost(symbol)
      cap = max(0, min(deployable, risk_notional, symbol_cap, feasibility, stage_cap))
      qty = int(cap // price)

    feasibility(계좌 매수여력)는 **하향 클램프로만** — 없으면(None) 미적용이 아니라
    보수적으로 0 취급(주문 직전 재확인 실패 = 사이징 불가).
    """
    s = seed_krw() if seed is None else float(seed)
    if s <= 0 or price_krw <= 0 or per_share_risk_krw <= 0:
        return SizeResult(0, 0.0, "invalid", {"seed": s})
    gates = {
        "risk": (risk_pct * s / per_share_risk_krw) * price_krw,
        "symbol_cap": pos_cap * s - max(0.0, open_cost_symbol),
        "deployable": s if deployable_amt is None else float(deployable_amt),
        "feasibility": 0.0 if feasibility is None else float(feasibility),
    }
    if stage_cap is not None:
        gates["stage_cap"] = float(stage_cap)
    binding = min(gates, key=lambda k: gates[k])
    cap = max(0.0, gates[binding])
    qty = int(cap // price_krw)
    return SizeResult(qty, cap, binding, gates)


def invariant_ok(seed: float, open_cost: float) -> bool:
    """불변식: bot_open_cost ≤ SEED. 위반=사이징/회계 버그 → 신규 매수 금지+P0."""
    return open_cost <= seed + 1e-6
