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


def boot_reconcile(excgs: tuple[str, ...] = ("NASD", "NYSE", "AMEX")) -> dict:
    """부팅 대사 1회. 반환 {ok, unknowns, resolved, low, results}.

    · UNKNOWN이 없으면 즉시 ok(게이트 열림).
    · 있으면 거래소별 nccs+ccnl을 모아 대사 — HIGH는 해소, LOW는 잠금 유지+P0.
    · 조회 실패(None)는 fail-closed: ok=False, 게이트 닫힘(재시도는 호출부 몫).
    """
    _STATE["done"] = False
    unknowns = pending_unknowns()
    if not unknowns:
        _STATE.update(done=True, low=0)
        return {"ok": True, "unknowns": 0, "resolved": 0, "low": 0, "results": []}

    # 필요한 거래소만 조회(유량 절약) — meta.excg 없으면 전체.
    need = {u.get("excg") for u in unknowns if u.get("excg")} or set(excgs)
    all_nccs_rows, all_ccnl_rows = [], []
    for ex in sorted(need):
        n = kis.open_orders(excg=ex)
        c = kis.fills(excg=ex)
        if n is None or c is None:                 # 조회 실패 — fail-closed
            _notify(f"🚨 부팅 대사 조회 실패({ex}) — 매매 게이트 닫힌 채 유지",
                    critical=True)
            return {"ok": False, "unknowns": len(unknowns), "resolved": 0,
                    "low": 0, "results": []}
        all_nccs_rows += (n.get("output") or [])
        all_ccnl_rows += (c.get("output") or [])

    results = kis_reconcile.reconcile_unknowns(
        {"rt_cd": "0", "output": all_nccs_rows},
        {"rt_cd": "0", "output": all_ccnl_rows})
    low = [r for r in results if r.get("confidence") == ledger.CONF_LOW]
    resolved = [r for r in results if r.get("confidence") == ledger.CONF_HIGH]
    for r in low:
        _notify(f"🚨 부팅 대사 LOW — {r.get('symbol')}(후보 {r.get('candidates')}) "
                f"잠금 유지, 수동 검토 필요(MANUAL_REVIEW)", critical=True)
    if resolved:
        _notify("🔁 부팅 대사 — " + ", ".join(
            f"{r.get('symbol')} {r.get('state')}(체결 {r.get('filled')})"
            for r in resolved), critical=True)
    _STATE.update(done=True, low=len(low))
    return {"ok": True, "unknowns": len(unknowns), "resolved": len(resolved),
            "low": len(low), "results": results}


def trading_allowed() -> bool:
    """신규 매매 허용 게이트 — 부팅 대사가 이 프로세스에서 완료됐어야 True.
    (LOW 잔존은 종목별 잠금이 이미 막으므로 전체 게이트는 열되, 그 종목은 잠김.)"""
    return bool(_STATE["done"])
