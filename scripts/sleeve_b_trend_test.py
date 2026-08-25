#!/usr/bin/env python3
"""B1 가설 검정 — 진입 시점 200일선 위/아래로 슬리브 B 성과를 가른다(읽기 전용).

왜 이 가설인가: 이 데이터를 **보기 전에** 등록된 가설이다. 외부검토 5.2가
B′(두 변수 동시 변경)를 기각하고 B0(기준)/B1(추세 필터만)/B2(별도 계열)로
쪼개도록 한 그 B1이다. 14건에 맞춰 임계값을 뒤지면 과적합이지만, 미리 정해둔
단일 변수를 검정하는 것은 다르다.

무엇을 재나(2026-08-26 소급 부검 결과에 대한 응답):
  · 손절 10건의 MFE 중앙값이 0.37R — '올랐다 반납'이 아니라 **안 오른다**.
  · 진입가(반사실 최대 +0.98%p)도 청산규칙(+1.44%p)도 B를 흑자로 못 돌린다.
  → 남은 용의자는 신호 자체다. 200일선 위/아래가 그것을 가르는지 본다.

원장에는 `trend_above_200` 태그가 없다(신호 JSON에만 있고 주문 meta로 흐르지
않는다). 그래서 저장된 태그에 의존하지 않고 **진입일의 200일 이동평균을 야후
일봉으로 직접 계산**한다 — 태그 배선과 무관하게 성립하는 독립 측정이다.

fail-closed: 200일치 봉이 모자라면 그 건은 '판정 보류'로 남기고 어느 쪽으로도
세지 않는다. 표본을 부풀리는 것이 이 분석에서 가장 쉬운 자기기만이다.

주문 0 · 계좌 조회 0 · 쓰기 0. 야후 조회는 종목당 1회.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request

from bot import ledger, trade_history

_UA = {"User-Agent": "Mozilla/5.0"}
MA_WINDOW = 200


def _bars(sym: str, rng: str = "2y") -> list[dict] | None:
    """일봉 (날짜, 종가, 고가, 저가). 실패=None(빈 결과와 구분)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym) + f"?range={rng}&interval=1d")
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=_UA), timeout=20) as resp:
            res = json.load(resp)["chart"]["result"][0]
        offset = int((res["meta"] or {}).get("gmtoffset") or 0)
        stamps = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except Exception:
        return None
    out = []
    for i, stamp in enumerate(stamps):
        try:
            close, hi, lo = q["close"][i], q["high"][i], q["low"][i]
            if close is None or hi is None or lo is None:
                continue
            day = (datetime.datetime.utcfromtimestamp(int(stamp))
                   + datetime.timedelta(seconds=offset)).date().isoformat()
            close, hi, lo = float(close), float(hi), float(lo)
        except (TypeError, ValueError, IndexError, KeyError,
                OSError, OverflowError):
            continue
        if min(close, hi, lo) > 0 and all(map(math.isfinite, (close, hi, lo))):
            out.append({"d": day, "c": close, "hi": hi, "lo": lo})
    out.sort(key=lambda r: r["d"])
    return out


def _syms(code: str, market: str) -> list[str]:
    code = str(code).upper()
    if market == "KR" or (code.isdigit() and len(code) == 6):
        return [f"{code}.KS", f"{code}.KQ"]
    return [code]


def trend_at(bars: list[dict], day: str) -> tuple[float | None, float | None]:
    """(진입일 종가, 그날까지의 200일 이동평균). 봉이 모자라면 (종가, None)."""
    prior = [b for b in bars if b["d"] <= day]
    if not prior:
        return None, None
    price = prior[-1]["c"]
    if len(prior) < MA_WINDOW:
        return price, None                  # 표본 부족 — 추정하지 않는다
    return price, statistics.fmean(b["c"] for b in prior[-MA_WINDOW:])


def _stop_of(code: str) -> float | None:
    best = None
    for order in ledger.orders_for(code, side="BUY"):
        try:
            stop = float(order.get("stop"))
        except (TypeError, ValueError):
            continue
        ts = float(order.get("submitted_at") or 0)
        if stop > 0 and (best is None or ts >= best[0]):
            best = (ts, stop)
    return None if best is None else best[1]


def _pair(rows: list[dict]) -> dict[int, str]:
    entry_day, last_buy = {}, {}
    for row in sorted(rows, key=lambda r: str(r.get("executed_at") or "")):
        code = str(row.get("code") or "").upper()
        if str(row.get("side") or "") == "buy":
            last_buy[code] = str(row.get("day") or "")
        elif str(row.get("side") or "") == "sell" and last_buy.get(code):
            entry_day[id(row)] = last_buy[code]
    return entry_day


def collect(sleeve: str, *, pause: float = 0.4) -> list[dict]:
    snap = trade_history.snapshot(limit=500)
    if not isinstance(snap, dict) or not snap.get("available"):
        raise RuntimeError("원장 무결성 미확인 — 분석 중단")
    rows = snap.get("trades") or []
    entry_day = _pair(rows)
    sells = [r for r in rows
             if str(r.get("side") or "").lower() == "sell"
             and str(r.get("sleeve") or "A").upper() == sleeve]
    cache: dict[str, list[dict] | None] = {}
    out = []
    for row in sells:
        code = str(row.get("code") or "").upper()
        entry_px, d0 = row.get("entry_price"), entry_day.get(id(row))
        d1 = str(row.get("day") or "")
        rec = {"code": code, "ret": row.get("return_pct"),
               "reason_kind": row.get("reason_kind"), "d0": d0,
               "above": None, "gap_pct": None, "mfe_r": None, "why": ""}
        if not entry_px or not d0:
            rec["why"] = "진입일/진입가 미상"
            out.append(rec); continue
        if code not in cache:
            bars = None
            for sym in _syms(code, str(row.get("market") or "US")):
                bars = _bars(sym)
                if bars:
                    break
                time.sleep(pause)
            cache[code] = bars
            time.sleep(pause)
        bars = cache[code]
        if bars is None:
            rec["why"] = "야후 조회 실패(실패≠부재)"
            out.append(rec); continue
        price, ma = trend_at(bars, d0)
        if price is None:
            rec["why"] = f"진입일({d0}) 이전 봉 없음"
            out.append(rec); continue
        if ma is None:
            rec["why"] = f"200일치 봉 부족 — 판정 보류"
            out.append(rec); continue
        rec["above"] = price >= ma
        rec["gap_pct"] = (price / ma - 1) * 100
        # MFE(R)도 같이 — 위/아래 그룹의 '움직임 크기' 차이를 보려는 것
        window = [b for b in bars if d0 <= b["d"] <= d1]
        stop = _stop_of(code)
        if window and stop and 0 < stop < float(entry_px):
            r_pct = (float(entry_px) - stop) / float(entry_px) * 100
            mfe = (max(b["hi"] for b in window) / float(entry_px) - 1) * 100
            rec["mfe_r"] = mfe / r_pct if r_pct > 0 else None
        out.append(rec)
    return out


def report(records: list[dict], sleeve: str) -> None:
    judged = [r for r in records if r["above"] is not None]
    held = [r for r in records if r["above"] is None]
    print(f"\n{'='*74}")
    print(f" B1 가설 검정 — 슬리브 {sleeve} · 진입일 200일선 위/아래")
    print(f"{'='*74}")
    print(f" {'종목':<8}{'진입일':<12}{'200MA 대비':>11}{'실현':>9}{'MFE':>8}  종료")
    print(" " + "-"*72)
    for r in sorted(judged, key=lambda x: -(x["gap_pct"] or 0)):
        kind = {"stop": "손절", "take_profit": "목표",
                "time_stop": "타임"}.get(r["reason_kind"], r["reason_kind"] or "?")
        mark = "위" if r["above"] else "아래"
        mfe = f"{r['mfe_r']:+.2f}R" if r["mfe_r"] is not None else "—"
        print(f" {r['code']:<8}{r['d0']:<12}{r['gap_pct']:>+9.1f}%{mark:<3}"
              f"{r['ret']:>+8.2f}%{mfe:>8}  {kind}")
    for r in held:
        print(f" {r['code']:<8}{str(r['d0'] or '?'):<12}{'—':>11}"
              f"{(r['ret'] or 0):>+8.2f}%{'—':>8}  ※ {r['why']}")

    print(f"\n{'='*74}")
    if not judged:
        print(" 판정 가능한 건이 없습니다.")
        return
    for label, group in (("200일선 위", [r for r in judged if r["above"]]),
                         ("200일선 아래", [r for r in judged if not r["above"]])):
        if not group:
            print(f" {label}: 0건")
            continue
        rets = [float(r["ret"]) for r in group if r["ret"] is not None]
        wins = [v for v in rets if v > 0]
        mfes = [r["mfe_r"] for r in group if r["mfe_r"] is not None]
        print(f" {label}: {len(group)}건 · 승 {len(wins)}건 "
              f"({len(wins)/len(rets)*100:.0f}%) · 평균 "
              f"{statistics.fmean(rets):+.2f}%"
              + (f" · MFE 중앙 {statistics.median(mfes):+.2f}R" if mfes else ""))
    if held:
        print(f" 판정 보류: {len(held)}건 (표본에 넣지 않음)")
    print(f"{'='*74}")
    print(" ※ 표본이 작아 확정이 아니라 **방향**만 본다. 이 가설은 데이터를 보기")
    print("   전에 등록된 것이라 사후 짜맞추기는 아니지만, 셀이 한 자릿수면")
    print("   우연으로도 이 정도 차이는 난다.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B1(200일선 필터) 가설 검정")
    ap.add_argument("--sleeve", default="B", choices=("A", "B"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        records = collect(args.sleeve)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 2
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=1, default=str))
        return 0
    report(records, args.sleeve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
