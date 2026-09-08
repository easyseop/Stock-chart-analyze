#!/usr/bin/env python3
"""월간 결산 보고 — 확정 매도 기준, 슬리브별·종료사유별·지수 대비(읽기 전용).

왜 지금(2026-09-08): PAAS·TRUP·OMCL 회계 복구가 끝나 8월 장부가 처음으로
브로커와 정합한다. 그 전에 결산하면 PAAS +10.14%(B의 이긴 거래)가 빠진 숫자를
보게 된다. 월 귀속은 `day`(KST 실현일) 기준 — trade_stats.by_month와 같은 축.

주문 0 · 계좌 조회 0 · 쓰기 0. 금액(KRW)이 포함되므로 운영자 화면 전용이다.
실행: python3 scripts/monthly_close.py --month 2026-08
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha, trade_history  # noqa: E402

KIND = {"stop": "손절", "take_profit": "익절/목표", "time_stop": "타임스탑",
        "trail": "트레일", "other": "기타"}


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def month_rows(rows: list[dict], month: str) -> list[dict]:
    return [r for r in rows
            if str(r.get("side") or "").lower() == "sell"
            and str(r.get("day") or r.get("executed_at") or "")[:7] == month]


def bucket(rows: list[dict]) -> dict:
    rets = [x for x in (_f(r.get("return_pct")) for r in rows) if x is not None]
    pnls = [x for x in (_f(r.get("realized_pnl_krw")) for r in rows) if x is not None]
    wins = [x for x in rets if x > 0]; losses = [x for x in rets if x <= 0]
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(rets) * 100, 1) if rets else None,
        "avg": round(statistics.fmean(rets), 2) if rets else None,
        "median": round(statistics.median(rets), 2) if rets else None,
        "avg_win": round(statistics.fmean(wins), 2) if wins else None,
        "avg_loss": round(statistics.fmean(losses), 2) if losses else None,
        "pnl_krw": round(sum(pnls)) if pnls else None,
        "pnl_n": len(pnls),
        "best": max(rets) if rets else None, "worst": min(rets) if rets else None,
    }


def report(month: str) -> int:
    snap = trade_history.snapshot(limit=500)
    if not isinstance(snap, dict) or not snap.get("available"):
        print("✗ 거래이력 신뢰 불가(원장 무결성) — 결산 중단"); return 2
    rows = month_rows(snap.get("trades") or [], month)
    print(f"\n{'='*64}\n 월간 결산 {month} — 확정 매도 {len(rows)}건\n{'='*64}")
    if snap.get("partial"):
        print(" ⚠ 일부 행이 미확정/추정가(partial) — 수치 인용 시 주의")
    if not rows:
        print(" 해당 월 확정 매도 없음"); return 1

    print(f"\n {'구분':<10}{'건수':>5}{'승':>4}{'패':>4}{'승률':>8}{'평균':>9}{'중앙':>9}"
          f"{'평균승':>8}{'평균패':>8}{'실현손익(원)':>14}")
    print(" " + "-" * 82)
    groups = [("전체", rows)] + [
        (f"슬리브 {s}", [r for r in rows if str(r.get("sleeve") or "A").upper() == s])
        for s in ("A", "B")]
    for label, g in groups:
        if not g: continue
        b = bucket(g)
        pnl = f"{b['pnl_krw']:+,}" if b["pnl_krw"] is not None else "?"
        print(f" {label:<10}{b['n']:>5}{b['wins']:>4}{b['losses']:>4}"
              f"{(b['win_rate'] or 0):>7.1f}%{(b['avg'] or 0):>+8.2f}%{(b['median'] or 0):>+8.2f}%"
              f"{(b['avg_win'] or 0):>+7.2f}%{(b['avg_loss'] or 0):>+7.2f}%{pnl:>14}")

    print("\n 종료 사유별")
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(str(r.get("reason_kind") or "other"), []).append(r)
    for k, g in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        b = bucket(g)
        print(f"   {KIND.get(k, k):<10}{b['n']:>4}건 · 승 {b['wins']} · "
              f"평균 {(b['avg'] or 0):+.2f}%")

    ranked = sorted((r for r in rows if _f(r.get("return_pct")) is not None),
                    key=lambda r: float(r["return_pct"]))
    print("\n 최고 / 최저 3건")
    for r in list(reversed(ranked[-3:])) + ranked[:3]:
        print(f"   {str(r.get('code')):<8}{str(r.get('sleeve') or 'A'):<3}"
              f"{float(r['return_pct']):>+8.2f}%  {r.get('day','')}  "
              f"{KIND.get(str(r.get('reason_kind')), '')}")

    print("\n 지수 대비(알파) — 해당 월 세션")
    try:
        days = [d for d in (alpha._load().get("days") or [])
                if str(d.get("d") or "").startswith(month)]
        for mkt in ("US", "KR"):
            sub = [d for d in days if str(d.get("mkt")) == mkt]
            print(f"   [{mkt}] {len(sub)}세션 · {alpha.capture_stats(sub, mkt) if sub else '기록 없음'}")
    except Exception as exc:
        print(f"   알파 집계 실패: {type(exc).__name__}: {exc}")

    print("\n 참고: 슬리브 B 200일선 필터(B1)는 08-26 배포 — 8월 B 거래는 사실상 전부 필터 이전 진입분.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="월간 결산(읽기 전용)")
    ap.add_argument("--month", required=True, help="YYYY-MM (KST 실현일 기준)")
    a = ap.parse_args(argv)
    if len(a.month) != 7 or a.month[4] != "-":
        print("✗ --month는 YYYY-MM"); return 2
    return report(a.month)


if __name__ == "__main__":
    raise SystemExit(main())
