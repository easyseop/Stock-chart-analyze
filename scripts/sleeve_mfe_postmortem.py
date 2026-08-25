#!/usr/bin/env python3
"""슬리브 청산 소급 부검 — MFE/MAE로 "구제 가능했나"를 실측한다(읽기 전용).

원장에는 진입가와 청산가만 있고 **그 사이**가 없다. 그런데 처방이 그 사이에
달려 있다:

    100 → 112 → 93   (+12% 갔다 되돌아옴)   → 본전 래칫이 있었으면 0%에서 탈출
    100 →  98 → 93   (처음부터 쭉 하락)      → 래칫 무관, 진입 타이밍 문제

둘 다 원장엔 똑같이 −7%로 적힌다. 야후 일봉의 고가/저가로 그 사이를 복원한다.

  MFE = (기간 최고가 − 진입가)/진입가   "얼마나 벌 수 있었나"
  MAE = (기간 최저가 − 진입가)/진입가   "얼마나 깊이 팠나"
  R   = (진입가 − 손절가)/진입가        원장 매수 주문 meta의 stop

핵심 판정: 손절로 끝난 거래 중 MFE ≥ 1R이었던 비율. 이 값이 높으면 청산 규칙
(절반익절·본전 래칫)이 처방이고, 낮으면 진입 타이밍이 처방이다.

MAE는 슬리피지도 드러낸다. MAE가 손절선보다 훨씬 깊으면 체결이 늦은 것이고,
손절선 언저리면 손절선 위치 자체가 먼 것이다.

주문 0 · 계좌 조회 0 · 쓰기 0 · 배포 0. 야후 조회만 종목당 1회.
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


def _ohlc(sym: str, rng: str = "1y") -> list[dict] | None:
    """일봉 OHLC. 실패=None(빈 결과와 구분 — 실패≠부재)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym) + f"?range={rng}&interval=1d")
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=_UA), timeout=15) as resp:
            res = json.load(resp)["chart"]["result"][0]
        offset = int((res["meta"] or {}).get("gmtoffset") or 0)
        stamps = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except Exception:
        return None
    out = []
    for i, stamp in enumerate(stamps):
        try:
            hi, lo = q["high"][i], q["low"][i]
            if hi is None or lo is None:
                continue
            day = (datetime.datetime.utcfromtimestamp(int(stamp))
                   + datetime.timedelta(seconds=offset)).date().isoformat()
            hi, lo = float(hi), float(lo)
        except (TypeError, ValueError, IndexError, KeyError,
                OSError, OverflowError):
            continue
        if hi > 0 and lo > 0 and math.isfinite(hi) and math.isfinite(lo):
            out.append({"d": day, "hi": hi, "lo": lo})
    out.sort(key=lambda r: r["d"])
    return out


def _yahoo_symbols(code: str, market: str) -> list[str]:
    """야후 티커 후보. KR 6자리는 .KS 먼저, 없으면 .KQ(scanner/data.py와 동일)."""
    code = str(code).upper()
    if market == "KR" or (code.isdigit() and len(code) == 6):
        return [f"{code}.KS", f"{code}.KQ"]
    return [code]


def _stop_of(code: str) -> float | None:
    """원장 매수 주문 meta에 기록된 손절가(가장 최근 것)."""
    best = None
    for order in ledger.orders_for(code, side="BUY"):
        stop = order.get("stop")
        try:
            stop = float(stop)
        except (TypeError, ValueError):
            continue
        if stop > 0 and (best is None or
                         float(order.get("submitted_at") or 0) >= best[0]):
            best = (float(order.get("submitted_at") or 0), stop)
    return None if best is None else best[1]


def _pair_entries(rows: list[dict]) -> dict[int, str]:
    """매도 행 → 그 종목의 직전 매수일. 같은 종목의 마지막 선행 매수를 쓴다."""
    entry_day: dict[int, str] = {}
    last_buy: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: str(r.get("executed_at") or "")):
        code = str(row.get("code") or "").upper()
        if row.get("side") == "buy":
            last_buy[code] = str(row.get("day") or "")
        elif row.get("side") == "sell" and last_buy.get(code):
            entry_day[id(row)] = last_buy[code]
    return entry_day


def analyze(sleeve: str, *, pause: float = 0.4) -> list[dict]:
    snap = trade_history.snapshot(limit=500)
    if not isinstance(snap, dict) or not snap.get("available"):
        raise RuntimeError("원장 무결성 미확인 — 분석 중단")
    rows = snap.get("trades") or []
    entry_day = _pair_entries(rows)
    sells = [r for r in rows
             if str(r.get("side") or "").lower() == "sell"
             and str(r.get("sleeve") or "A").upper() == sleeve]

    bars_cache: dict[str, list[dict] | None] = {}
    out = []
    for row in sells:
        code = str(row.get("code") or "").upper()
        entry = row.get("entry_price")
        d0, d1 = entry_day.get(id(row)), str(row.get("day") or "")
        rec = {"code": code, "reason_kind": row.get("reason_kind"),
               "reason": row.get("reason"), "ret": row.get("return_pct"),
               "entry": entry, "d0": d0, "d1": d1,
               "mfe": None, "mae": None, "r_pct": None, "why": ""}
        if not entry or not d0 or not d1 or d0 > d1:
            rec["why"] = "진입일/진입가 미상 — 짝 못 지음"
            out.append(rec); continue
        if code not in bars_cache:
            bars = None
            for sym in _yahoo_symbols(code, str(row.get("market") or "US")):
                bars = _ohlc(sym)
                if bars:
                    break
                time.sleep(pause)
            bars_cache[code] = bars
            time.sleep(pause)
        bars = bars_cache[code]
        if bars is None:
            rec["why"] = "야후 조회 실패(실패≠부재)"
            out.append(rec); continue
        window = [b for b in bars if d0 <= b["d"] <= d1]
        if not window:
            rec["why"] = f"일봉 범위 밖({d0}~{d1}) — 조회 기간 초과 가능"
            out.append(rec); continue
        entry = float(entry)
        rec["mfe"] = (max(b["hi"] for b in window) / entry - 1) * 100
        rec["mae"] = (min(b["lo"] for b in window) / entry - 1) * 100
        rec["bars"] = len(window)
        stop = _stop_of(code)
        if stop and 0 < stop < entry:
            rec["r_pct"] = (entry - stop) / entry * 100
        out.append(rec)
    return out


def report(records: list[dict], sleeve: str) -> None:
    print(f"\n{'='*78}")
    print(f" 슬리브 {sleeve} 소급 부검 — MFE(최대상승) / MAE(최대하락)")
    print(f"{'='*78}")
    print(f" {'종목':<8}{'종료':<12}{'실현':>8}{'MFE':>8}{'MAE':>9}"
          f"{'R':>7}{'MFE/R':>7}  비고")
    print(" " + "-"*76)
    for r in sorted(records, key=lambda x: (x["reason_kind"] or "", x["code"])):
        kind = {"stop": "손절", "take_profit": "목표", "time_stop": "타임",
                "trail": "트레일"}.get(r["reason_kind"], r["reason_kind"] or "?")
        ret = f"{r['ret']:+.2f}%" if r.get("ret") is not None else "  ?  "
        if r["mfe"] is None:
            print(f" {r['code']:<8}{kind:<12}{ret:>8}{'—':>8}{'—':>9}"
                  f"{'—':>7}{'—':>7}  {r['why']}")
            continue
        rr = f"{r['r_pct']:.2f}%" if r["r_pct"] else "—"
        ratio = (f"{r['mfe']/r['r_pct']:.2f}"
                 if r["r_pct"] and r["r_pct"] > 0 else "—")
        flag = ""
        if r["reason_kind"] == "stop" and r["r_pct"] and r["mfe"] >= r["r_pct"]:
            flag = "★ +1R 도달 — 래칫 구제 가능"
        print(f" {r['code']:<8}{kind:<12}{ret:>8}{r['mfe']:>+7.2f}%"
              f"{r['mae']:>+8.2f}%{rr:>7}{ratio:>7}  {flag}")

    stops = [r for r in records if r["reason_kind"] == "stop"
             and r["mfe"] is not None and r["r_pct"]]
    print()
    if not stops:
        print(" ※ 판정 불가 — 손절 건의 MFE/R을 계산하지 못했습니다.")
        return
    saved = [r for r in stops if r["mfe"] >= r["r_pct"]]
    print(f"{'='*78}")
    print(f" 판정: 손절 {len(stops)}건 중 +1R 도달 {len(saved)}건 "
          f"({len(saved)/len(stops)*100:.0f}%)")
    print(f"{'='*78}")
    print(f"   손절 건 MFE 중앙값 : {statistics.median(r['mfe'] for r in stops):+.2f}%")
    print(f"   손절 건 MAE 중앙값 : {statistics.median(r['mae'] for r in stops):+.2f}%")
    print(f"   손절선(R) 중앙값   : {statistics.median(r['r_pct'] for r in stops):.2f}%")
    deeper = [r for r in stops if r["mae"] < -r["r_pct"] * 1.5]
    print(f"   MAE가 손절선의 1.5배보다 깊은 건 : {len(deeper)}건"
          " (슬리피지·체결지연 의심)" if deeper else
          f"   MAE가 손절선의 1.5배보다 깊은 건 : 0건")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MFE/MAE 소급 부검(읽기 전용)")
    ap.add_argument("--sleeve", default="B", choices=("A", "B"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        records = analyze(args.sleeve)
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
