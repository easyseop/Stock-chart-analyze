"""거래 성적(승률) 공개용 요약 — 금액 없이 비율·건수만 발행.

사용자 요청(2026-08-15): 폰에서 승률을 보고 싶다. 승률은 이미 개인 서버
대시보드(`/api/trades.json`)에 있지만 그 API는 Oracle에만 있고 공개 사이트에는
없다(404). 원본에는 체결가·수량·실현손익 금액이 들어 있어 그대로 공개할 수 없다.

이 모듈은 확정 체결 원장에서 **건수·비율만** 뽑아 ntfy로 발행하고, GitHub Actions
빌드(`scanner/perf_site.py`)가 그것을 공개 사이트 JSON으로 굽는다.

공개 금지: 금액(KRW/USD), 수량, 평단, 종목코드/이름, 계좌·토큰.
공개 허용: 승/패 건수, 승률(%), 평균·중앙 수익률(%), 전략(A/B)별 분해, 월별 건수.

실행(수동 1회): python -m bot.trade_stats
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.request

PUBLISH_INTERVAL_S = int(
    os.environ.get("TRADE_STATS_INTERVAL_S", "900") or 900)      # 기본 15분
_last_publish = 0.0


def _topic() -> str:
    from bot import settings
    return os.environ.get("NTFY_TRADE_STATS_TOPIC",
                          getattr(settings, "TRADE_STATS_TOPIC", ""))


def _rate(wins: int, losses: int) -> float | None:
    decided = wins + losses
    return round(wins / decided * 100, 2) if decided else None


def _bucket(rows: list[dict]) -> dict:
    """매도 행 묶음 → 승/패·승률·수익률 통계(금액 제외)."""
    decided = [r for r in rows if r.get("realized_pnl_krw") is not None]
    wins = sum(1 for r in decided if float(r["realized_pnl_krw"]) > 0)
    losses = sum(1 for r in decided if float(r["realized_pnl_krw"]) < 0)
    returns = [float(r["return_pct"]) for r in rows
               if r.get("return_pct") is not None]
    win_returns = [float(r["return_pct"]) for r in decided
                   if float(r["realized_pnl_krw"]) > 0
                   and r.get("return_pct") is not None]
    loss_returns = [float(r["return_pct"]) for r in decided
                    if float(r["realized_pnl_krw"]) < 0
                    and r.get("return_pct") is not None]
    return {
        "closed": len(rows), "decided": wins + losses,
        "wins": wins, "losses": losses, "win_rate": _rate(wins, losses),
        "avg_return_pct": round(statistics.fmean(returns), 2) if returns else None,
        "median_return_pct": (round(statistics.median(returns), 2)
                              if returns else None),
        "avg_win_pct": (round(statistics.fmean(win_returns), 2)
                        if win_returns else None),
        "avg_loss_pct": (round(statistics.fmean(loss_returns), 2)
                         if loss_returns else None),
    }


def summary(limit: int = 500) -> dict | None:
    """공개 가능한 거래 성적 요약. 원장 불신이면 None(지어내지 않는다)."""
    try:
        from bot import trade_history
        snap = trade_history.snapshot(limit=limit)
    except Exception:
        return None
    if not isinstance(snap, dict) or not snap.get("available"):
        return None
    rows = [r for r in (snap.get("trades") or [])
            if str(r.get("side") or "").lower() == "sell"]
    by_sleeve = {
        sleeve: _bucket([r for r in rows
                         if str(r.get("sleeve") or "A").upper() == sleeve])
        for sleeve in ("A", "B")
    }
    months: dict[str, dict] = {}
    for row in rows:
        month = str(row.get("ts") or "")[:7]                 # YYYY-MM
        if len(month) == 7:
            months.setdefault(month, []).append(row)
    return {
        "version": 1,
        "generated_at": snap.get("generated_at"),
        "partial": bool(snap.get("partial")),
        "total": _bucket(rows),
        "by_sleeve": by_sleeve,
        "by_month": {m: _bucket(rs) for m, rs in sorted(months.items())[-12:]},
        "note": "확정 매도 체결 기준 · 금액·수량·종목은 공개하지 않습니다.",
    }


def publish(payload: dict | None = None) -> bool:
    """ntfy 발행. 토픽 미설정·실패는 무해(False)."""
    topic = _topic()
    if not topic:
        return False
    data = summary() if payload is None else payload
    if not data:
        return False
    try:
        body = json.dumps(data, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic, data=body, method="POST",
            headers={"Title": "trade-stats", "Priority": "min",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False


def maybe_publish() -> bool:
    """주기 발행(기본 15분). 호출 비용이 없도록 시간부터 확인."""
    global _last_publish
    now = time.time()
    if now - _last_publish < max(300, PUBLISH_INTERVAL_S):
        return False
    _last_publish = now                    # 실패해도 갱신(폭주 방지)
    return publish()


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=1))
