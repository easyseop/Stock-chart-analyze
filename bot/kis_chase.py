"""R5 — 마켓터블 지정가 chase 상태기계: KIS 미국주 손절의 '시장가 없음' 보완.

문제: KIS 미국주는 연속장 시장가가 없어 손절이 지정가다 → 급락 시 미체결로
남으면 무방비. 그렇다고 무한 재던지기(chase)를 하면 취소 UNKNOWN 상태에서
이중매도가 난다(리뷰 B4 시나리오). 이 상태기계가 그 사이 균형을 강제한다:

  working ──(재게시 시간 경과·미체결)──▶ 취소 → 더 공격적 가격으로 재발주
     │                                     (슬리피지 사다리, floor 하한)
     ├─ 전량 체결 ────────────────▶ done
     ├─ max_chase 소진 ──────────▶ exhausted  (P0 — 수동 런북)
     ├─ max_time_to_exit 초과 ───▶ timeout    (P0 — 수동 런북)
     └─ 취소/발주 응답 유실 ─────▶ manual_lock (원장이 종목 잠금 — 자동 재개 금지)

핵심 규칙(리뷰 R5):
  · cancel/replace 전 **반드시 체결 잔량 재확인**(filled_total) — 이미 다 팔렸으면 done.
  · **취소가 '확정'돼야만** 재발주 — 취소 거부(이미 체결 가능성)면 현 주문 유지 후
    다음 step에서 체결 재확인. 취소 UNKNOWN이면 즉시 manual_lock(이중매도 차단).
  · 가격 사다리: ref×(1 − (base+i·step)bp), **floor = ref×(1 − max_slippage)** 아래로
    절대 안 내려감(덤핑 방지). floor에서도 미체결이면 exhausted로 사람 호출.
  · 모든 발주/취소는 kis_orders 게이트(잠금·in-flight·유량)를 그대로 통과 —
    이 모듈은 '언제/얼마에'만 결정하고 안전장치를 우회하지 않는다.

의존성 주입(deps)으로 테스트 가능: place/cancel/filled_total/quote/now.
실전 배선은 파수꾼(또는 서버 루프C)이 poll마다 step()을 부른다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChaseConfig:
    base_slippage_bps: int = 30      # 1차 발주: 현재가 −30bp
    step_bps: int = 40               # 재발주마다 40bp씩 공격적으로
    max_slippage_bps: int = 200      # floor: ref −2% 아래로는 절대 안 팖
    max_chase: int = 3               # 발주 시도 총 횟수(1차 포함)
    repost_after_s: float = 20.0     # 미체결 이 초 경과 시 취소→재발주
    max_time_to_exit_s: float = 120.0  # 전체 손절 SLA — 초과 시 사람 호출


TERMINAL = {"done", "exhausted", "timeout", "manual_lock"}


@dataclass
class Chase:
    """한 포지션의 손절 chase. step()을 폴링마다 호출."""
    pos_key: str                     # 포지션 정체성 키(주문키는 #n으로 파생)
    symbol: str
    qty: int                         # 팔아야 할 총 수량
    ref_price: float                 # 기준가(트리거 시점 시세) — 사다리·floor 기준
    deps: dict                       # place/cancel/filled_total/quote/now
    cfg: ChaseConfig = field(default_factory=ChaseConfig)
    status: str = "idle"
    attempts: int = 0
    current: dict | None = None      # 살아있는 주문 {key, odno, price, placed_at}
    t0: float | None = None

    def _notify(self, text: str) -> None:
        try:
            from bot import notify
            notify.send(text, critical=True)
        except Exception:
            pass

    def _floor(self) -> float:
        return round(self.ref_price * (1 - self.cfg.max_slippage_bps / 1e4), 2)

    def _ladder_price(self) -> float:
        """이번 시도의 지정가 — 최신 시세와 ref 중 낮은 쪽 기준, floor 하한."""
        q = None
        try:
            q = self.deps["quote"]()
        except Exception:
            pass
        base = min(self.ref_price, q) if q else self.ref_price
        bps = self.cfg.base_slippage_bps + self.attempts * self.cfg.step_bps
        px = base * (1 - bps / 1e4)
        return round(max(px, self._floor()), 2)

    def step(self) -> str:
        if self.status in TERMINAL:
            return self.status
        now = self.deps["now"]()
        if self.t0 is None:
            self.t0 = now
            self.status = "working"

        # 0) 체결 재확인이 항상 먼저 — cancel/replace보다 앞(리뷰 R5)
        filled = int(self.deps["filled_total"]())
        if filled >= self.qty:
            self.status = "done"
            return self.status

        # 1) 전체 SLA
        if now - self.t0 > self.cfg.max_time_to_exit_s:
            self.status = "timeout"
            self._notify(f"🚨 손절 SLA 초과 — {self.symbol} 잔여 "
                         f"{self.qty - filled}주 미체결. 수동 런북 필요.")
            return self.status

        # 2) 살아있는 주문이 없으면 발주(첫 시도 또는 취소 확정 후)
        if self.current is None:
            if self.attempts >= self.cfg.max_chase:
                self.status = "exhausted"
                self._notify(f"🚨 손절 chase 소진({self.attempts}회) — "
                             f"{self.symbol} 잔여 {self.qty - filled}주. floor="
                             f"{self._floor()}. 수동 개입 필요.")
                return self.status
            px = self._ladder_price()
            key = f"{self.pos_key}#c{self.attempts + 1}"
            r = self.deps["place"](key, self.symbol, self.qty - filled, px)
            act = r.get("act")
            if act == "ack":
                self.attempts += 1
                self.current = {"key": key, "odno": r.get("odno", ""),
                                "price": px, "placed_at": now,
                                "qty": self.qty - filled}
            elif act == "unknown":
                self.status = "manual_lock"       # 원장이 종목 잠금 — 자동 재개 금지
                return self.status
            elif act == "blocked":
                pass                              # 게이트(간격/잠금) — 다음 step 재시도
            else:                                 # reject/rate_limited — 시도 소모
                self.attempts += 1
            return self.status

        # 3) 살아있는 주문 — 재게시 시간 지났으면 취소 시도
        if now - self.current["placed_at"] >= self.cfg.repost_after_s:
            c = self.deps["cancel"](self.current["key"] + ":cxl", self.symbol,
                                    self.current["odno"], self.current["qty"])
            act = c.get("act")
            if act == "canceled":
                self.current = None               # 다음 step에서 더 공격적 재발주
            elif act == "unknown":
                self.status = "manual_lock"       # 원주문 생사 불명 — 이중매도 차단
                return self.status
            # 취소 거부(이미 체결됐을 가능성) — 현 주문 유지, 다음 step에서
            # filled_total 재확인이 잡는다. 새 주문은 in-flight 게이트가 차단.
        return self.status
