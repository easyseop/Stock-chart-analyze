#!/usr/bin/env python3
"""슬리브 B 청산 부검 — "왜 지는가"를 종료 사유로 분해한다(읽기 전용).

배경(2026-08-25 실측): 확정 매도 50건에서 A는 36건·승률 77.8%·평균 +5.83%인데
B는 14건·승률 28.6%·평균 −2.60%다. Fisher 양측 p=0.0024로 "B가 A보다 나쁘다"는
확정이지만, **어디가 나쁜지**는 승률만으로 알 수 없다.

B의 청산은 세 갈래뿐이다(bot/kis_exits.py:decide_b + 파수꾼 손절):
    stop        — 반등저점 아래 손절
    take_profit — 목표 VAH 도달 전량
    time_stop   — 21일 경과 전량 정리

이 셋의 비율과 각각의 수익률 분포가 처방을 가른다:
  · stop 비중이 크고 손실이 깊다      → 진입이 이르다(더 낮게 진입이 정답 방향)
  · time_stop 비중이 크다             → 신호가 애초에 안 움직인다(진입 깊이 무관)
  · take_profit인데 평균이 얇다       → 목표가 가깝다(진입이 아니라 목표 문제)

주문 0 · 계좌 조회 0 — 로컬 저널만 읽는다.
실행: python3 -m scripts.sleeve_b_postmortem  또는  python3 scripts/sleeve_b_postmortem.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import trade_history  # noqa: E402

_KIND_LABEL = {
    "stop": "손절(반등저점 이탈)",
    "take_profit": "목표 VAH 도달",
    "time_stop": "타임스탑 21일",
    "trail": "트레일",
    "other": "기타/미분류",
}


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "avg": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def pair_entry_days(rows: list[dict]) -> dict[int, str]:
    """매도 행 → 그 종목의 직전 매수일.

    매도 행에는 ``opened``가 없다. 종전 구현은 그 필드를 읽어 **모든** 보유일이
    조용히 0으로 찍혔다(실측 2026-08-25: 50건 전부 0.0일). 없는 값을 0으로
    보여주면 "샀다가 당일 청산"으로 읽혀서 진입 타이밍 판단을 정반대로 몬다.
    시간순으로 훑어 같은 종목의 마지막 선행 매수와 짝짓고, 짝이 없으면
    None으로 남긴다 — 0이 아니라 '모름'이다.
    """
    entry_day: dict[int, str] = {}
    last_buy: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: str(r.get("executed_at") or "")):
        code = str(row.get("code") or "").upper()
        if str(row.get("side") or "") == "buy":
            last_buy[code] = str(row.get("day") or "")
        elif str(row.get("side") or "") == "sell" and last_buy.get(code):
            entry_day[id(row)] = last_buy[code]
    return entry_day


def _hold_days(row: dict, entry_day: dict[int, str]) -> float | None:
    """보유일수 — 짝지은 매수일과 청산일의 차이. 짝이 없으면 None."""
    import datetime
    opened, closed = entry_day.get(id(row), ""), str(row.get("day") or "")
    try:
        d0 = datetime.date.fromisoformat(opened[:10])
        d1 = datetime.date.fromisoformat(closed[:10])
    except (ValueError, TypeError):
        return None
    return (d1 - d0).days


def analyze(rows: list[dict], entry_day: dict[int, str] | None = None) -> dict:
    """한 슬리브의 매도 행 → 종료 사유별 분해."""
    entry_day = {} if entry_day is None else entry_day
    by_kind: dict[str, list[dict]] = {}
    for row in rows:
        by_kind.setdefault(str(row.get("reason_kind") or "other"), []).append(row)

    out: dict = {"closed": len(rows), "by_kind": {}}
    returns_all = [float(r["return_pct"]) for r in rows
                   if r.get("return_pct") is not None]
    out["overall"] = _stats(returns_all)
    for kind, group in sorted(by_kind.items(),
                              key=lambda kv: -len(kv[1])):
        returns = [float(r["return_pct"]) for r in group
                   if r.get("return_pct") is not None]
        wins = [v for v in returns if v > 0]
        holds = [d for d in (_hold_days(r, entry_day) for r in group)
                 if d is not None]
        out["by_kind"][kind] = {
            "label": _KIND_LABEL.get(kind, kind),
            "n": len(group),
            "share_pct": round(len(group) / len(rows) * 100, 1) if rows else None,
            "win_n": len(wins),
            "returns": _stats(returns),
            "hold_days": _stats([float(d) for d in holds]),
            # 원문 사유는 브로커 문자열이 아니라 우리 코드가 붙인 라벨이라
            # 그대로 보여도 안전하다. 진단에 가장 유용한 단서다.
            "reasons": sorted({str(r.get("reason") or "") for r in group}),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="슬리브 B 청산 부검(읽기 전용)")
    ap.add_argument("--sleeve", default="B", choices=("A", "B", "ALL"))
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", action="store_true", help="원시 JSON만 출력")
    args = ap.parse_args(argv)

    snap = trade_history.snapshot(limit=args.limit)
    if not isinstance(snap, dict) or not snap.get("available"):
        print("✗ 거래이력을 신뢰할 수 없습니다(원장 무결성) — 분석 중단")
        return 2

    sells = [r for r in (snap.get("trades") or [])
             if str(r.get("side") or "").lower() == "sell"]
    if not sells:
        print("확정 매도가 없습니다.")
        return 1

    wanted = ("A", "B") if args.sleeve == "ALL" else (args.sleeve,)
    # 짝짓기는 매수 행이 필요하므로 전체 행으로 한 번만 만든다.
    entry_day = pair_entry_days(snap.get("trades") or [])
    result = {s: analyze([r for r in sells
                          if str(r.get("sleeve") or "A").upper() == s],
                         entry_day)
              for s in wanted}
    result["_partial"] = bool(snap.get("partial"))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    for sleeve in wanted:
        data = result[sleeve]
        overall = data["overall"]
        print(f"\n{'='*62}")
        print(f" 슬리브 {sleeve} — 확정 매도 {data['closed']}건")
        print(f"{'='*62}")
        if overall.get("n"):
            print(f" 전체 수익률: 평균 {overall['avg']:+.2f}% · "
                  f"중앙 {overall['median']:+.2f}% · "
                  f"범위 {overall['min']:+.2f}% ~ {overall['max']:+.2f}%")
        print()
        print(f" {'종료 사유':<20} {'건수':>5} {'비중':>7} {'승':>4} "
              f"{'평균':>8} {'중앙':>8} {'최악':>8} {'보유일':>7}")
        print(f" {'-'*20} {'-'*5} {'-'*7} {'-'*4} "
              f"{'-'*8} {'-'*8} {'-'*8} {'-'*7}")
        for kind, info in data["by_kind"].items():
            r, h = info["returns"], info["hold_days"]
            hold = f"{h['avg']:>6.1f}일" if h.get("n") else "     ?"
            miss = "" if h.get("n", 0) == info["n"] else \
                f"  (보유일 {info['n'] - h.get('n', 0)}건 미상)"
            print(f" {info['label']:<20} {info['n']:>5} "
                  f"{info['share_pct']:>6.1f}% {info['win_n']:>4} "
                  f"{r.get('avg', 0):>+7.2f}% {r.get('median', 0):>+7.2f}% "
                  f"{r.get('min', 0):>+7.2f}% {hold}{miss}")
        print()
        for kind, info in data["by_kind"].items():
            for text in info["reasons"]:
                if text:
                    print(f"   · [{info['label']}] {text}")
    if result["_partial"]:
        print("\n⚠ 일부 행이 미확정/추정가입니다(partial) — 경향 판단엔 쓰되 "
              "정밀 수치로 인용하지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
