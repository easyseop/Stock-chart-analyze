"""X1 — 매수 실행기: 신호 → **게이트 체인 전부 통과해야** place_buy 1건.

게이트 순서(전부 fail-closed — 어느 하나라도 판정 불가면 매수 없음):
   0. ALLOW_BUY="1" (I4 환경 분리 — 미설정=매수 경로 자체 봉인)
   1. kill-switch: allows("buy_new") (I6 — L1부터 신규 금지)
   2. 부팅 대사 완료: kis_boot.trading_allowed() (O4 — 대사 전 매매 금지)
   3. 파수꾼 생존성: heartbeat.entry_allowed(has_positions) (R4 —
      보호자가 죽어있으면 새 리스크 안 늘림)
   4. 롤아웃 가드: rollout.check_new_entry (I7+I1 — Stage 캡·allowlist·
      US 정규장만·whole-share·하루 한도)
   5. 계좌 격리: ownership.buy_denied (IS2 — baseline denylist·동결)
   6. 원장: ledger.can_submit (UNKNOWN 잠금·동일종목 in-flight·간격)
   7. 사이징: envelope.size_buy — 분모 SEED·총량 게이트(deployable)·
      feasibility=kis.buying_power(하향 클램프, 미확인=0주)
   8. 전송: kis_orders.place_buy (자체 게이트 재검사 — 모의 전용 하드블록 포함)

fx: 미국주 가격은 USD — SEED(KRW) 사이징을 위해 환율(krw_per_usd)을 인자로 받는다
(판정 불가 시 매수 없음). cost 확정은 체결 시 costbook이 담당(fill 시점 fx 고정).

이 모듈은 스스로 실행되지 않는다 — 서버 루프B(또는 검증 스크립트)가 명시 호출.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from bot import (costbook, daily_loss, envelope, heartbeat, kill, kis, kis_boot,
                 kis_orders, ledger, ownership, rollout)


@dataclass
class BuyDecision:
    ok: bool
    gate: str            # 어느 게이트에서 멈췄나("sent"면 전송됨)
    why: str
    qty: int = 0
    planned_qty: int = 0   # 게이트·사이징이 허용한 전체 수량(half 분할 기준)
    order: dict | None = None


def execute_entry(pos_key: str, symbol: str, *, price_usd: float,
                  per_share_risk_usd: float, krw_per_usd: float,
                  excg: str = "NASD", open_positions: int | None = None,
                  risk_pct: float = envelope.DEFAULT_RISK_PCT,
                  reason: str = "진입",
                  market: str | None = None,
                  open_cost_krw: float | None = None,
                  total_open_cost_krw: float | None = None,
                  held_cost_krw: float | None = None,
                  total_held_cost_krw: float | None = None,
                  operating_limit_krw: float | None = None,
                  hldg_before: int | None = None,
                  seed_krw: float | None = None,
                  sleeve: str = "A",
                  limit_price: float | None = None,
                  qty_cap: int | None = None,
                  qty_fraction: float = 1.0,
                  order_meta: dict | None = None) -> BuyDecision:
    """신규 진입 1건 시도. 반환: 어느 게이트에서 왜 멈췄는지까지 항상 보고.

    가격/리스크 인자는 **해당 시장의 표시통화**(US=USD, KR=KRW)로 받는다.
    KR이면 이미 원화이므로 krw_per_usd는 1.0을 넘긴다(환산 없음).
    market=None이면 심볼로 자동 판별(국내 6자리 숫자=KR).

    open_cost_krw: 호출부(매수 루프)가 브로커 잔고에서 계산한 **봇 포지션 투입원가
      (KRW)**. costbook이 확정체결 배선(#25) 전까지 비어 있어 총량 게이트(deployable=
      SEED−open_cost)와 불변식이 무력해지는 구멍(2026-07-15 검토)을 브로커-진실로
      메운다. costbook 값과 **max**로 합성(보수적) — 배선 후에도 안전.
    hldg_before: 주문 직전 그 종목 보유수량(브로커-진실). 원장 meta로 남아
      ack(접수)→체결 확정의 잔고-delta 대사 기준이 된다."""
    symbol = symbol.upper()
    market = market or kis.market_of_symbol(symbol)
    # 원화 환산계수: US는 fx(krw_per_usd), KR은 표시통화가 이미 원화 → 1.0.
    fx = 1.0 if market == "KR" else krw_per_usd

    # 0) 환경 분리(I4)
    if os.environ.get("ALLOW_BUY") != "1":
        return BuyDecision(False, "env", "ALLOW_BUY != 1 (매수 경로 봉인)")
    # NaN은 `<= 0` 비교가 전부 False라 이 게이트를 미끄러지고, bool은 float
    #   1.0/0.0으로 둔갑한다(`float(True)==1.0`, isfinite(True)==True — 특히
    #   환율 1.0은 사이징 단위를 통째로 왜곡, Codex V5 P1-3). 호출부 검사와
    #   독립인 이중방어로 명시 차단한다.
    if any(isinstance(v, bool) for v in (price_usd, per_share_risk_usd, fx)) \
            or isinstance(limit_price, bool):
        return BuyDecision(False, "input", "price/risk/fx 무효(boolean)")
    if not (math.isfinite(price_usd) and math.isfinite(per_share_risk_usd)
            and math.isfinite(fx)):
        return BuyDecision(False, "input", "price/risk/fx 무효(NaN·inf)")
    if price_usd <= 0 or per_share_risk_usd <= 0 or fx <= 0:
        return BuyDecision(False, "input", "price/risk/fx 무효")
    # order_meta.stop이 **제공된 경우** finite·양수 재검사(Codex V2 P1 이중방어).
    #   무효 stop이 원장 메타로 남으면 체결 후 회계·보호원장이 stop<=0을 거부해
    #   실보유만 있고 보호 없는 상태가 된다 — 주문 전에 끊는다.
    if order_meta is not None and "stop" in order_meta:
        if isinstance(order_meta["stop"], bool):
            return BuyDecision(False, "input", "order_meta.stop 무효(boolean)")
        try:
            meta_stop = float(order_meta["stop"])
        except (TypeError, ValueError):
            return BuyDecision(False, "input", "order_meta.stop 형식 오류")
        if not (math.isfinite(meta_stop) and meta_stop > 0):
            return BuyDecision(False, "input",
                               f"order_meta.stop 무효({order_meta['stop']})")

    # 1) kill-switch(I6)
    if not kill.allows("buy_new"):
        return BuyDecision(False, "kill", f"kill-switch L{kill.level()} — 신규 금지")

    # 2) 일일 실현손실 — 신규 매수만 차단, 손절·청산 경로와 독립.
    ok, why = daily_loss.entry_allowed()
    if not ok:
        return BuyDecision(False, "daily_loss", why)

    # 3) 부팅 대사(O4)
    if not kis_boot.trading_allowed():
        return BuyDecision(False, "boot", "부팅 대사 미완료(fail-closed)")

    # 4) 파수꾼 생존성(R4) — 첫 포지션도 보호자 heartbeat 없이는 진입 금지
    has_pos = (open_positions or 0) > 0 or costbook.open_cost_total(sleeve) > 0
    if not heartbeat.entry_allowed(has_pos):
        return BuyDecision(False, "sla", "파수꾼 heartbeat 없음/120초 초과")

    # 5) 롤아웃 가드(I7+I1)
    n_open = open_positions if open_positions is not None else sum(
        1 for l in costbook._fold()["lots"].values()
        if l["qty"] > 0 and l.get("sleeve", "A") == sleeve)
    ok, why = rollout.check_new_entry(symbol, open_positions=n_open,
                                      risk_pct=risk_pct, market=market)
    if not ok:
        return BuyDecision(False, "rollout", why)

    # 6) 계좌 격리(IS2)
    denied, why = ownership.buy_denied(symbol)
    if denied:
        return BuyDecision(False, "ownership", why)

    # 7) 원장 게이트
    if not ledger.can_submit(symbol):
        return BuyDecision(False, "ledger", "원장 게이트(잠금/in-flight/간격)")

    # 8) 사이징(IS3/IS4 — 분모 SEED·총량 게이트·feasibility 하향 클램프)
    #   seed_krw 오버라이드: 슬리브 B(매물대)는 자체 SEED로 사이징(A와 예산 분리).
    seed = (float(seed_krw) if seed_krw is not None
            else envelope.sleeve_limit_krw(sleeve))
    t = costbook.totals(sleeve)
    # 투입원가: costbook(회계)과 브로커-진실(호출부 전달) 중 **큰 쪽**(보수적).
    #   costbook 미배선(#25) 동안엔 broker 값이 유일한 실측 — 이게 없으면 총량
    #   게이트가 SEED 전액을 매 사이클 재배포 가능으로 오판한다(검토 2026-07-15).
    open_cost = costbook.open_cost_total(sleeve)
    if open_cost_krw is not None:
        #   슬리브 SEED 오버라이드(B) 시엔 호출부의 슬리브별 원가만 사용 — costbook은
        #   전역(슬리브 무구분)이라 max()로 섞으면 A의 원가가 B의 작은 SEED와 비교돼
        #   불변식 위반→kill L1(전체 매수 중단) 오발동 위험(정합성 점검 2026-07-24).
        open_cost = (float(open_cost_krw) if seed_krw is not None
                     else max(open_cost, float(open_cost_krw)))
    if not envelope.invariant_ok(seed, open_cost):
        kill.raise_level(1, "kis_buy", f"불변식 위반 open_cost {open_cost:.0f} > SEED")
        return BuyDecision(False, "invariant", "open_cost > SEED — 회계 버그, 신규 중지")
    sleeve_dep = envelope.deployable(
        seed, envelope.bot_cash(seed, t["buy_cost"], t["sell_proceeds"]), open_cost)
    total_open = (float(total_open_cost_krw) if total_open_cost_krw is not None
                  else costbook.open_cost_total("A") + costbook.open_cost_total("B"))
    operating_limit = (float(operating_limit_krw)
                       if operating_limit_krw is not None
                       else envelope.operating_limit_krw())
    if total_open > operating_limit + 1e-6:
        kill.raise_level(
            1, "kis_buy",
            f"A+B 예약원가 {total_open:.0f} > 운영한도 {operating_limit:.0f}")
        return BuyDecision(
            False, "total_invariant",
            "A+B held+in-flight+planned > operating limit — 신규 중지")
    dep = min(sleeve_dep, envelope.combined_deployable(
        total_open, operating_limit=operating_limit))
    bp_native = kis.buying_power_of(symbol, price_usd, market=market, excg=excg)
    feas_krw = bp_native * fx if bp_native is not None else None
    r = envelope.size_buy(price_usd * fx,
                          per_share_risk_usd * fx,
                          seed=seed,
                          open_cost_symbol=costbook.open_cost_symbol(symbol, sleeve),
                          deployable_amt=dep, feasibility=feas_krw,
                          risk_pct=risk_pct)
    planned_qty = r.qty
    frac = min(1.0, max(0.0, float(qty_fraction)))
    # sizing 0을 half 전술이 1주로 승격시키면 fail-closed 사슬이 깨진다.
    send_qty = r.qty if frac >= 1.0 else (
        max(1, int(r.qty * frac)) if r.qty >= 1 and frac > 0 else 0)
    if qty_cap is not None:
        send_qty = min(send_qty, max(0, int(qty_cap)))
    if send_qty < 1:
        return BuyDecision(False, "sizing",
                           f"수량 0 (binding={r.binding}, cap={r.cap_krw:.0f}KRW)",
                           planned_qty=planned_qty)
    if held_cost_krw is None or total_held_cost_krw is None:
        # 최종 원자 게이트는 브로커가 확인한 A/B 보유원가와 원장의 모든 예약을
        # 같은 flock 안에서 합쳐야 한다. 호출부가 이 스냅샷을 주지 않으면
        # 개별 사이징이 맞아도 다른 프로세스와의 합산을 증명할 수 없으므로 전송 금지.
        return BuyDecision(
            False, "budget",
            "브로커 총시드 스냅샷 없음 — 원자 예산 게이트 불가",
            planned_qty=planned_qty)

    # 9) 전송 — kis_orders가 모의 전용 하드블록 등 자체 게이트 재검사
    #   미국주는 실제 상장 거래소로 라우팅(감사 수정 #1: NASD 고정 시 NYSE/AMEX
    #   매수가 live에서 거부). KR은 excg 무관.
    if market != "KR":
        excg = kis.us_excg_of(symbol)
    limit = (float(limit_price) if limit_price is not None
             else kis_orders.marketable_limit_price(price_usd, "BUY", market=market))
    reservation_qty = (min(planned_qty, max(0, int(qty_cap)))
                       if qty_cap is not None else planned_qty)
    meta = {
        "pos_key": pos_key, "sleeve": sleeve, "fx": fx,
        "ccy": "KRW" if market == "KR" else "USD",
        # half는 1차+후속 계획 전체를 첫 submit부터 예약한다. 후속 plan은 parent가
        # 열려 있는 동안 원장 합산에서 제외되고, parent 종료 뒤 예약을 이어받는다.
        "reservation_cost_krw": reservation_qty * limit * fx,
        **(order_meta or {}),
    }
    # 잔고 대사 직후 KIS holdings 반영이 늦을 수 있으므로, 체결회계 원장과
    # 브로커 보유원가 중 큰 값을 최종 held 바닥으로 사용한다. 이미 반영된 값은
    # 서로 중복합산하지 않고 max로 합성해 과대·과소 예약을 모두 피한다.
    accounted_sleeve = costbook.open_cost_total(sleeve)
    accounted_total = costbook.open_cost_total("A") + costbook.open_cost_total("B")
    meta.update({
        "budget_total_held_krw": max(
            float(total_held_cost_krw), accounted_total),
        "budget_total_limit_krw": operating_limit,
        "budget_sleeve_held_krw": max(
            float(held_cost_krw), accounted_sleeve),
        "budget_sleeve_limit_krw": seed,
    })
    res = kis_orders.place_buy(pos_key, symbol, send_qty, limit,
                               excg=excg, reason=reason, market=market,
                               hldg_before=hldg_before, order_meta=meta)
    if res.get("ok"):
        return BuyDecision(True, "sent", f"ack ODNO={res.get('odno')}",
                           qty=send_qty, planned_qty=planned_qty, order=res)
    return BuyDecision(False, "orders", f"{res.get('act')}: {res.get('why')}",
                       qty=send_qty, planned_qty=planned_qty, order=res)
