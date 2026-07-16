"""O4 — 부팅/재시작 대사: 원장의 미해소 UNKNOWN을 브로커 조회로 자동 대사.

왜(설계 O4·리뷰 R4): 크래시·재시작 직후가 가장 위험하다 — 프로세스가 죽기 직전에
보낸 주문의 결과를 모른 채(UNKNOWN) 다시 뜨면, 대사 없이 매매를 재개할 경우
이중주문/초과매도가 난다. 그래서:

  · 시작 시 nccs(미체결)+ccnl(체결내역)을 읽어 `kis_reconcile.reconcile_unknowns`.
  · **대사가 끝나기 전(또는 실패 시) 신규 매매 금지** — `trading_allowed()` 게이트.
  · LOW(후보 0/2+)는 자동 해소 금지 → 잠금 유지 + P0 알림(수동 검토).
  · 조회 자체가 실패하면(네트워크 등) fail-closed: 게이트 닫힌 채 유지.

사용(상시 서버/파수꾼 시작 시퀀스):
    from bot import kis_boot
    summary = kis_boot.boot_reconcile()      # 1회 대사
    if kis_boot.trading_allowed():           # True여야 신규 매매 허용
        ...
읽기·계산 전용 — 주문 없음.
"""
from __future__ import annotations

import time

from bot import kis, kis_reconcile, ledger

# 모듈 상태 — 이 프로세스에서 부팅 대사가 성공적으로 끝났는가.
_STATE = {"done": False, "low": 0}


def _notify(text: str, *, critical: bool = False) -> None:
    try:
        from bot import notify
        notify.send(text, critical=critical)
    except Exception:
        pass


def pending_unknowns() -> list[dict]:
    """원장에서 미해소 UNKNOWN 주문 목록."""
    return [o for o in ledger.open_orders()
            if o.get("state") == "unknown" and not o.get("reconciled")]


def _resolve_acks() -> list[dict]:
    """접수(ack) 주문의 잔고 기반 체결 확정 — 매 대사 사이클 함께 수행.

    ack가 영원히 미해소로 남으면 파수꾼이 그 종목 손절을 스킵(치명)·재진입 영구
    차단·미러 캡 인플레이트(2026-07-15 검토). 여기서 잔고로 full-fill을 증명해 푼다.
    실패(잔고 조회 불가 등)는 조용히 다음 사이클 — 매매 게이트(_STATE)와 무관
    (ack 관련 위험은 그 자체가 전부 fail-closed 방향: 매도 보류·재진입 차단).
    """
    try:
        aged = [o for o in ledger.open_orders()
                if o.get("state") in ("submitted", "ack")
                and (o.get("side") or "").upper() in ("BUY", "SELL")
                and time.time() - float(o.get("submitted_at") or 0)
                >= kis_reconcile.ACK_AGE_MIN_S]
        if not aged:
            return []

        def _is_kr(u: dict) -> bool:
            return (u.get("market") == "KR"
                    or kis.market_of_symbol(u.get("symbol", "")) == "KR")

        hmaps: dict[str, dict | None] = {}
        if any(_is_kr(o) for o in aged):
            hmaps["KR"] = kis.holdings("KR")
        if any(not _is_kr(o) for o in aged):
            merged: dict | None = {}
            for ex in ("NASD", "NYSE", "AMEX"):
                h = kis.holdings("US", excg=ex)
                if h is None:                      # 하나라도 실패 = US 전체 신뢰 불가
                    merged = None
                    break
                merged.update(h)
            hmaps["US"] = merged
        rs = kis_reconcile.resolve_acks_by_balance(hmaps)
        for r in rs:
            # 매도 full-fill 확정 → 진입 손절선 기록 정리(보호 대상 아님을 명시)
            if r.get("side") == "SELL" and int(r.get("residual") or 0) == 0:
                try:
                    from bot import kis_positions
                    kis_positions.close(str(r.get("symbol")))
                except Exception:
                    pass
            _notify(f"✅ 체결 확정(잔고대사) — {r.get('symbol')} "
                    f"{'매수' if r.get('side') == 'BUY' else '매도'} "
                    f"{r.get('filled')}주", critical=(r.get("side") == "SELL"))
        return rs
    except Exception:
        return []                                  # 대사 실패가 부팅을 못 깨게


def boot_reconcile(excgs: tuple[str, ...] = ("NASD", "NYSE", "AMEX")) -> dict:
    """부팅 대사 1회. 반환 {ok, unknowns, resolved, low, results}.

    · UNKNOWN이 없으면 즉시 ok(게이트 열림).
    · 있으면 거래소별 nccs+ccnl을 모아 대사 — HIGH는 해소, LOW는 잠금 유지+P0.
    · 조회 실패(None)는 fail-closed: ok=False, 게이트 닫힘(재시도는 호출부 몫).
    """
    _STATE["done"] = False
    acks = _resolve_acks()                        # 접수(ack)→체결 확정(잔고대사)
    unknowns = pending_unknowns()
    if not unknowns:
        _STATE.update(done=True, low=0)
        return {"ok": True, "unknowns": 0, "resolved": 0, "low": 0,
                "results": [], "ack_resolved": len(acks)}

    def _is_kr(u: dict) -> bool:
        return (u.get("market") == "KR"
                or kis.market_of_symbol(u.get("symbol", "")) == "KR")

    # 국내(KR) UNKNOWN — 잔고 delta 기반 대사(reconcile_unknowns_kr). SELL만 정확
    #   full-fill 확정 시 자동해소, 나머지(BUY·부분·불명·조회실패)는 LOW 잠금 유지.
    #   국내 nccs 모의 미지원·costbook 미배선이라 잔고를 유일 근거로 쓴다(안전 정리).
    kr_results = []
    if any(_is_kr(u) for u in unknowns):
        kr_results = kis_reconcile.reconcile_unknowns_kr(kis.domestic_balance())

    all_nccs_rows, all_ccnl_rows = [], []

    def _notify_low(rs):
        for r in rs:
            if r.get("confidence") != ledger.CONF_LOW or r.get("already_low"):
                continue
            why = r.get("kr_reason") or f"후보 {r.get('candidates')}"
            _notify(f"🚨 부팅 대사 LOW — {r.get('symbol')}({why}) "
                    f"잠금 유지, 수동 검토 필요(MANUAL_REVIEW)", critical=True)

    # 미국(US) UNKNOWN — 필요한 거래소만 조회(유량 절약). meta.excg 없으면 전체.
    us = [u for u in unknowns if not _is_kr(u)]
    if us:
        need = {u.get("excg") for u in us if u.get("excg")} or set(excgs)
        for ex in sorted(need):
            n = kis.open_orders(excg=ex)
            c = kis.fills(excg=ex)
            if n is None or c is None:             # 조회 실패 — fail-closed
                _notify(f"🚨 부팅 대사 조회 실패({ex}) — 매매 게이트 닫힌 채 유지",
                        critical=True)
                # KR은 이미 대사됨(원장 반영) — 결과·알림 보존(관측성). 게이트만 닫는다.
                _notify_low(kr_results)
                kr_low = sum(1 for r in kr_results
                             if r.get("confidence") == ledger.CONF_LOW)
                return {"ok": False, "unknowns": len(unknowns),
                        "resolved": sum(1 for r in kr_results
                                        if r.get("confidence") == ledger.CONF_HIGH),
                        "low": kr_low, "results": kr_results,
                        "ack_resolved": len(acks)}
            all_nccs_rows += (n.get("output") or [])
            all_ccnl_rows += (c.get("output") or [])

    # US는 nccs/ccnl per-order 매칭(reconcile_unknowns는 KR을 건너뛴다).
    results = kis_reconcile.reconcile_unknowns(
        {"rt_cd": "0", "output": all_nccs_rows},
        {"rt_cd": "0", "output": all_ccnl_rows}) + kr_results
    low = [r for r in results if r.get("confidence") == ledger.CONF_LOW]
    resolved = [r for r in results if r.get("confidence") == ledger.CONF_HIGH]
    _notify_low(results)                          # already_low는 내부에서 억제
    if resolved:
        _notify("🔁 부팅 대사 — " + ", ".join(
            f"{r.get('symbol')} {r.get('state')}(체결 {r.get('filled')})"
            for r in resolved), critical=True)
    _STATE.update(done=True, low=len(low))
    return {"ok": True, "unknowns": len(unknowns), "resolved": len(resolved),
            "low": len(low), "results": results, "ack_resolved": len(acks)}


def trading_allowed() -> bool:
    """신규 매매 허용 게이트 — 부팅 대사가 이 프로세스에서 완료됐어야 True.
    (LOW 잔존은 종목별 잠금이 이미 막으므로 전체 게이트는 열되, 그 종목은 잠김.)"""
    return bool(_STATE["done"])
