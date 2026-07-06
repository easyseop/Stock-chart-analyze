"""차트 전용 리스크 관리 백테스트 — v2(초안).

이 도구를 '예측'이 아니라 '리스크 관리'로 쓸 때 실제로 계좌를 지키는지를
과거 데이터로 검증한다. 라이브와 동일한 analyze()를 시점마다 재호출해
신호를 재현하므로 룰이 분리되지 않는다(= 보이는 신호 그대로 백테스트).

진입 규칙(차트만):
  - analyze() 가 매수/전환후보 신호를 내고, 하락추세 veto가 아닐 때
청산 규칙:
  - 다음날 시가 체결 → 손절(차트 손절가) 또는 목표(체결가 기준 1:2) 도달 시 청산
  - 같은 날 둘 다 닿으면 보수적으로 '손절 먼저'
  - max_hold 거래일 초과 시 종가 청산(시간 손절)

성과는 모두 'R(리스크 단위)' 로 본다: +1R = 손절폭만큼 이익, -1R = 손절.
계좌 1% 리스크 비중이므로 R 합계에 1%를 곱하면 대략의 계좌 수익률이 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import os

import pandas as pd

import config
from scanner import data
from scanner.analyze import analyze


@dataclass
class Trade:
    code: str
    entry_date: pd.Timestamp
    entry: float
    stop: float
    target: float
    exit_date: pd.Timestamp
    exit: float
    reason: str        # 'stop' | 'target' | 'time'
    r: float           # 손익(R 단위)
    trigger: str       # 진입을 유발한 신호


from scanner import trendlines as _tl
TRANSITION = _tl.TRANSITION_CONFIRMED   # '전환 후보' 트리거 식별값


# ═════════════════════════════════════════════════════════════════
# 현행 추천 게이트 재생 — "지금 배포 중인 기준(gates.classify)이
# 과거에도 이기는 기준이었나"를 그대로 검증 (사용자 요구: 승률 분석)
#   진입: classify()=="now" (저점권·손절폭·과열·전환단계 게이트 전부 포함)
#   체결: 다음날 시가 / 청산: 확정 전략 사다리(1R 절반+본전 → 2R → 타임스탑)
# ═════════════════════════════════════════════════════════════════
from scanner import gates as _gates
from scanner.plan import freshness as _fresh_fn, tactic as _tactic_fn


def _ladder(d, ei: int, fill: float, stop: float,
            max_hold: int = 60) -> tuple[float, int, str]:
    """확정 전략 청산 사다리 → (R, 청산 인덱스, 사유)."""
    n = len(d)
    risk = fill - stop
    one_r, target = fill + risk, fill + 2 * risk
    half = False
    j = ei
    for j in range(ei, min(ei + max_hold, n)):
        lo, hi = float(d["Low"].iloc[j]), float(d["High"].iloc[j])
        cl = float(d["Close"].iloc[j])
        if not half:
            if lo <= stop:
                return -1.0, j, "손절"
            if hi >= one_r:
                half = True                      # 절반 익절 + 손절 본전
                continue
            if j - ei + 1 >= config.TIME_STOP_DAYS:
                return (cl - fill) / risk, j, "타임스탑"
        else:
            if lo <= fill:
                return 0.5, j, "본전스탑"
            if hi >= target:
                return 1.5, j, "목표(2R)"
    cl = float(d["Close"].iloc[j])
    r = (0.5 + 0.5 * (cl - fill) / risk) if half else (cl - fill) / risk
    return r, j, "만기청산"


def _atr_at(d, i: int, period: int = 14) -> float:
    """i번째 봉 기준 단순 ATR(진입 시점 스냅샷용)."""
    hi, lo, cl = d["High"], d["Low"], d["Close"]
    trs = []
    for j in range(max(1, i - period + 1), i + 1):
        pc = float(cl.iloc[j - 1])
        trs.append(max(float(hi.iloc[j]) - float(lo.iloc[j]),
                       abs(float(hi.iloc[j]) - pc),
                       abs(float(lo.iloc[j]) - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _ladder_exit(d, ei: int, fill: float, stop: float, variant: str,
                 max_hold: int = 60, atr_mult: float = 3.0) -> tuple[float, int, str]:
    """청산 방식 A/B/C 비교용 사다리 → (총 R, 청산 인덱스, 사유).

    공통(전략 확정분): 손절 전량 / +1R 절반 익절 / 타임스탑. 잔량 관리만 다름:
      A = 본전 스탑 + 2R 목표(현행 autopaper 코드)
      B = ATR 트레일(max(본전, 최고가−3×ATR)) + 2R 캡(STRATEGY.md 문서안)
      C = ATR 트레일만, 2R 캡 없음(승자를 끝까지 태움)
    보수적 판정: 같은 봉에서 손절/목표 둘 다 닿으면 손절 먼저.
    트레일은 '어제까지의 값'으로 오늘 low를 판정(선반영 방지) 후 갱신.
    """
    n = len(d)
    risk = fill - stop
    one_r, target = fill + risk, fill + 2 * risk
    atr0 = _atr_at(d, ei) if variant in ("B", "C") else 0.0
    half = False
    hw = fill
    trail = fill                       # 절반 익절 후 잔량 스탑(본전에서 시작)
    j = ei
    for j in range(ei, min(ei + max_hold, n)):
        lo_, hi_ = float(d["Low"].iloc[j]), float(d["High"].iloc[j])
        cl_ = float(d["Close"].iloc[j])
        if not half:
            if lo_ <= stop:
                return -1.0, j, "손절"
            if hi_ >= one_r:
                half = True
                hw = hi_
                continue
            if j - ei + 1 >= config.TIME_STOP_DAYS:
                return (cl_ - fill) / risk, j, "타임스탑"
        else:
            if lo_ <= trail:
                return (0.5 + 0.5 * (trail - fill) / risk, j,
                        "본전스탑" if trail <= fill else "트레일 스탑")
            if variant in ("A", "B") and hi_ >= target:
                return 1.5, j, "목표(2R)"
            hw = max(hw, hi_)
            if variant in ("B", "C") and atr0 > 0:
                trail = max(trail, hw - atr_mult * atr0)
    cl_ = float(d["Close"].iloc[j])
    r = (0.5 + 0.5 * (cl_ - fill) / risk) if half else (cl_ - fill) / risk
    return r, j, "만기청산"


def simulate_exit_variants(code: str, frames: dict, meta: dict, bench=None,
                           warmup: int = 520, stride: int = 3,
                           lookback: int = 780, max_hold: int = 60) -> list[dict]:
    """같은 진입 이벤트(gates 'now')에 청산 A/B/C를 나란히 적용 — 공정 비교."""
    d = frames["D"]
    n = len(d)
    i = max(warmup, n - lookback)
    out = []
    while i < n - 3:
        sub = d.iloc[:i + 1]
        bsub = bench.loc[:sub.index[-1]] if bench is not None else None
        try:
            res = analyze(data.frames_from_daily(sub), meta, bench=bsub)
        except Exception:
            i += stride
            continue
        if _gates.classify(res)["group"] != "now":
            i += stride
            continue
        ei = i + 1
        fill = float(d["Open"].iloc[ei])
        stop = float(res["risk"]["stop"])
        if not (fill > stop > 0):
            i += stride
            continue
        ra, xj, rea = _ladder_exit(d, ei, fill, stop, "A", max_hold)
        rb, _, reb = _ladder_exit(d, ei, fill, stop, "B", max_hold)
        rc, _, rec = _ladder_exit(d, ei, fill, stop, "C", max_hold)
        out.append({"code": code, "d": str(d.index[ei].date()),
                    "rA": round(ra, 2), "rB": round(rb, 2), "rC": round(rc, 2),
                    "reasonA": rea, "reasonB": reb, "reasonC": rec})
        i = xj + 1                     # A(현행) 기준으로 다음 탐색(이벤트셋 고정)
    return out


def cli_exit_variants() -> None:
    """청산 방식 A/B/C 백테스트 CLI — 캐시만 사용(네트워크 0).

    python -c "from scanner.backtest import cli_exit_variants; cli_exit_variants()" \
        -- [--stride 3] [--sample 0] [--out ...]
    """
    import argparse
    import json as _json
    import sys
    from scanner import cache as _cache
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--lookback", type=int, default=780)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args([x for x in sys.argv[1:] if x != "--"])
    codes = sorted(_cache.cached_codes())
    if a.sample and len(codes) > a.sample:
        step = len(codes) / a.sample
        codes = [codes[int(i * step)] for i in range(a.sample)]
    ev = []
    for code in codes:
        try:
            f = _cache.frames(code, refresh=False)
        except Exception:
            continue
        if len(f.get("D", [])) < 540:
            continue
        meta = {"code": code, "name": code,
                "ccy": "KRW" if code.isdigit() else "USD"}
        ev += simulate_exit_variants(code, f, meta,
                                     stride=a.stride, lookback=a.lookback)

    def agg(key):
        rs = [x[key] for x in ev]
        n = len(rs)
        w = sum(1 for r in rs if r > 0)
        return {"n": n, "win_rate": round(w / n * 100, 1) if n else 0,
                "avg_r": round(sum(rs) / n, 3) if n else 0,
                "max_r": round(max(rs), 2) if rs else 0,
                "min_r": round(min(rs), 2) if rs else 0}

    res = {"events": len(ev),
           "A_현행(본전+2R)": agg("rA"),
           "B_문서안(트레일+2R캡)": agg("rB"),
           "C_트레일무제한": agg("rC"),
           "raw": ev}
    print("=" * 70)
    print(f"◆ 청산 방식 A/B/C — 같은 진입 {len(ev)}건에 나란히 적용")
    for k in ("A_현행(본전+2R)", "B_문서안(트레일+2R캡)", "C_트레일무제한"):
        s = res[k]
        print(f"  {k:<22} 승률 {s['win_rate']:>5.1f}% · 기대값 {s['avg_r']:+.3f}R"
              f" · 최대 {s['max_r']:+.2f}R / 최소 {s['min_r']:+.2f}R")
    print("=" * 70)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fp:
            _json.dump(res, fp, ensure_ascii=False, indent=1)
        print("저장:", a.out)


def simulate_gates(code: str, frames: dict, meta: dict, bench=None,
                   warmup: int = 520, stride: int = 3,
                   lookback: int = 780, max_hold: int = 60) -> list[dict]:
    """한 종목을 현행 게이트로 워크포워드 — 이벤트별 R·컨텍스트 기록."""
    d = frames["D"]
    n = len(d)
    i = max(warmup, n - lookback)
    out = []
    while i < n - 3:
        sub = d.iloc[:i + 1]
        bsub = bench.loc[:sub.index[-1]] if bench is not None else None
        try:
            res = analyze(data.frames_from_daily(sub), meta, bench=bsub)
        except Exception:
            i += stride
            continue
        if _gates.classify(res)["group"] != "now":
            i += stride
            continue
        ei = i + 1
        fill = float(d["Open"].iloc[ei])
        stop = float(res["risk"]["stop"])
        if not (fill > stop > 0):
            i += stride
            continue
        fresh, _lab = _fresh_fn(res)
        t = _tactic_fn(res) or {}
        r, xj, reason = _ladder(d, ei, fill, stop, max_hold)
        # A/B: 같은 이벤트를 '눌림 지정가'로 기다렸다면? — 15봉 내 지정가 도달 시
        # 체결, 미도달이면 놓침(트레이드 없음). 즉시 체결과의 직접 비교용.
        pb = t.get("pb_price")
        r_pb, pb_filled = None, False
        if pb and stop < pb < fill:
            for j in range(ei, min(ei + config.TIME_STOP_DAYS, n)):
                if float(d["Low"].iloc[j]) <= pb:
                    r_pb, _xj2, _rs2 = _ladder(d, j, float(pb), stop, max_hold)
                    pb_filled = True
                    break
        out.append({"code": code, "d": str(d.index[ei].date()),
                    "r": round(r, 2), "reason": reason,
                    "fresh": bool(fresh), "mode": t.get("mode", "full"),
                    "rp": round(float(res.get("range_pos", 0.5)) * 100),
                    "stop_pct": round((fill - stop) / fill * 100, 1),
                    "has_pb": bool(pb and stop < pb < fill),
                    "pb_filled": pb_filled,
                    "r_pb": round(r_pb, 2) if r_pb is not None else None})
        i = xj + 1                               # 청산 후 재탐색(중복 보유 금지)
    return out


def run_gates(frames_map: dict, metas: dict, bench_map: dict | None = None,
              stride: int = 3, lookback: int = 780) -> dict:
    """현행 게이트 백테스트 — 전체/신선도별/전술별 승률·기대값 집계."""
    bench_map = bench_map or {}
    ev = []
    for code, frames in frames_map.items():
        ev += simulate_gates(code, frames, metas[code],
                             bench=bench_map.get(code),
                             stride=stride, lookback=lookback)

    def agg(rows):
        n = len(rows)
        w = sum(1 for x in rows if x["r"] > 0)
        return {"n": n, "win": w,
                "win_rate": round(w / n * 100, 1) if n else 0,
                "avg_r": round(sum(x["r"] for x in rows) / n, 3) if n else 0}

    by_mode = {m: agg([x for x in ev if x["mode"] == m])
               for m in ("full", "half", "pullback")
               if any(x["mode"] == m for x in ev)}
    # 교차 분석(신선도×전술) — "갓 전환이 나쁜 건가, 즉시 체결이 나쁜 건가"를 분리
    cross = {}
    for fr, flab in ((True, "fresh"), (False, "stale")):
        for m in ("full", "half", "pullback"):
            rows = [x for x in ev if x["fresh"] == fr and x["mode"] == m]
            if rows:
                cross[f"{flab}_{m}"] = agg(rows)
    # A/B: 즉시 체결 vs 눌림 지정가 — 눌림가가 존재하는 '같은 이벤트'끼리 비교
    pbable = [x for x in ev if x.get("has_pb")]
    filled = [x for x in pbable if x["pb_filled"]]
    ab = {}
    if pbable:
        ab = {
            "n": len(pbable),
            "immediate": agg(pbable),                        # A: 즉시(다음날 시가)
            "pb_fill_rate": round(len(filled) / len(pbable) * 100, 1),
            "pb_filled": agg([{"r": x["r_pb"]} for x in filled]),   # B: 체결분만
            # B의 이벤트당 기대값(미체결=0R로 — 놓침의 기회비용 반영)
            "pb_per_event_r": round(sum(x["r_pb"] for x in filled) / len(pbable), 3),
        }
    res = {
        "stocks": len(frames_map), "events": len(ev),
        "stride": stride, "lookback": lookback,
        "all": agg(ev),
        "fresh": agg([x for x in ev if x["fresh"]]),
        "stale": agg([x for x in ev if not x["fresh"]]),
        "by_mode": by_mode,
        "cross": cross,
        "ab": ab,
        "raw": ev,             # 이벤트 원자료 — 추가 분석/재집계용
        "note": ("현행 gates.classify('now') 재생 · 다음날 시가 체결 · "
                 "확정 전략 사다리 청산 · 슬리피지/수수료/시장방향(RS) 미반영"),
    }
    lab = {"full": "⚡즉시", "half": "⚖반반", "pullback": "⏳눌림"}
    print("=" * 66)
    print("◆ 현행 추천 게이트 백테스트 — 지금 기준의 역사적 승률")
    print(f"  종목 {res['stocks']} · 이벤트 {res['events']} · 최근 {lookback}봉")
    print("-" * 66)
    for name, s in [("전체", res["all"]), ("🔥 갓 전환", res["fresh"]),
                    ("↗ 돌파후 진행", res["stale"])] + [
                        (lab[m], v) for m, v in by_mode.items()]:
        if s["n"]:
            print(f"  {name:<14} {s['n']:>4}건 · 승률 {s['win_rate']:>5.1f}% · "
                  f"기대값 {s['avg_r']:+.3f}R")
    if cross:
        print("-" * 66)
        print("  교차(신선도×전술):")
        for k, s in cross.items():
            fl, m = k.split("_", 1)
            print(f"    {'🔥' if fl == 'fresh' else '↗'} {lab[m]:<6} "
                  f"{s['n']:>4}건 · 승률 {s['win_rate']:>5.1f}% · {s['avg_r']:+.3f}R")
    if ab:
        print("-" * 66)
        print(f"  A/B (눌림가 있는 {ab['n']}건 — 같은 이벤트 직접 비교):")
        print(f"    A 즉시 체결   승률 {ab['immediate']['win_rate']:>5.1f}% · "
              f"{ab['immediate']['avg_r']:+.3f}R/이벤트")
        print(f"    B 눌림 지정가 체결률 {ab['pb_fill_rate']}% · 체결분 승률 "
              f"{ab['pb_filled']['win_rate']:>5.1f}%({ab['pb_filled']['avg_r']:+.3f}R) · "
              f"이벤트당 {ab['pb_per_event_r']:+.3f}R(미체결=0)")
    print("=" * 66)
    return res


def cli_gates() -> None:
    """오프라인(캐시만) 현행 게이트 백테스트 CLI.

    python -c "from scanner.backtest import cli_gates; cli_gates()" \
        -- [--stride 3] [--max-stocks 0] [--out api/backtest.json]
    """
    import argparse
    import json as _json
    import sys
    from scanner import cache as _cache
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--lookback", type=int, default=780)
    ap.add_argument("--max-stocks", type=int, default=0)
    ap.add_argument("--sample", type=int, default=0,
                    help="전체에서 고르게 N종목 추출(한국/미국 섞이게)")
    ap.add_argument("--notify", action="store_true",
                    help="결과 요약을 텔레그램으로 발송")
    ap.add_argument("--out", default="")
    a = ap.parse_args([x for x in sys.argv[1:] if x != "--"])
    codes = sorted(_cache.cached_codes())
    if a.sample and len(codes) > a.sample:
        step = len(codes) / a.sample
        codes = [codes[int(i * step)] for i in range(a.sample)]
    if a.max_stocks:
        codes = codes[:a.max_stocks]
    frames_map, metas = {}, {}
    for code in codes:
        try:
            f = _cache.frames(code, refresh=False)   # 캐시만 — 네트워크 0
        except Exception:
            continue
        if len(f.get("D", [])) < 540:
            continue
        frames_map[code] = f
        metas[code] = {"code": code, "name": code,
                       "ccy": "KRW" if code.isdigit() else "USD"}
    res = run_gates(frames_map, metas, stride=a.stride, lookback=a.lookback)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fp:
            _json.dump(res, fp, ensure_ascii=False, indent=1)
        print("저장:", a.out)
    if a.notify:
        try:
            from bot import notify as _n
            s, f, st_ = res["all"], res["fresh"], res["stale"]
            lab = {"full": "⚡즉시", "half": "⚖반반", "pullback": "⏳눌림"}
            bm = " · ".join(f'{lab[m]} {v["win_rate"]}%({v["n"]})'
                            for m, v in res["by_mode"].items())
            _n.send(
                "📜 <b>전략 백테스트</b> — 현행 추천 기준의 역사적 성적\n"
                f'종목 {res["stocks"]} · 이벤트 {s["n"]}건 (최근 {res["lookback"]}봉)\n'
                f'전체: 승률 <b>{s["win_rate"]}%</b> · 기대값 <b>{s["avg_r"]:+.3f}R</b>\n'
                f'🔥 갓 전환: <b>{f["win_rate"]}%</b> ({f["n"]}건, {f["avg_r"]:+.3f}R)\n'
                f'↗ 돌파후 진행: {st_["win_rate"]}% ({st_["n"]}건, {st_["avg_r"]:+.3f}R)\n'
                f'전술별: {bm}\n'
                "※ 다음날 시가 체결 · 확정 청산 사다리 · 수수료/슬리피지 미반영")
        except Exception:
            pass


def trigger_kind(t: "Trade") -> str:
    """진입 신호 분류: 'transition'(전환 후보) | 'normal'(일반 매수)."""
    return "transition" if t.trigger in _tl.TRANSITION_STATES else "normal"


def is_entry(res: dict) -> bool:
    """차트 신호가 '진입'인가(하락추세 veto 제외)."""
    if res["vetoed"]:
        return False
    state = res["trendline"]["state"]
    label = res["verdict_label"]
    return (state in _tl.TRANSITION_STATES
            or label.startswith("매수") or label.startswith("적극 매수"))


def simulate(code: str, frames: dict, meta: dict, bench=None,
             warmup: int = 520, max_hold: int = 60) -> list[Trade]:
    """한 종목 시점별 워크포워드 시뮬레이션."""
    d = frames["D"]
    n = len(d)
    trades: list[Trade] = []
    i = warmup
    while i < n - 1:
        sub = d.iloc[:i + 1]
        bsub = bench.loc[:sub.index[-1]] if bench is not None else None
        try:
            res = analyze(data.frames_from_daily(sub), meta, bench=bsub)
        except Exception:
            i += 1
            continue
        if not is_entry(res):
            i += 1
            continue

        # 다음날 시가 체결
        ei = i + 1
        fill = float(d["Open"].iloc[ei])
        stop = float(res["risk"]["stop"])
        if fill <= stop:        # 갭하락으로 이미 손절 아래 → 무효
            i += 1
            continue
        risk = fill - stop
        target = fill + config.RR_TARGET * risk
        _st = res["trendline"]["state"]
        trigger = _st if _st in _tl.TRANSITION_STATES else res["verdict_label"]

        exit_i, exit_px, reason = _walk_forward(d, ei, fill, stop, max_hold)
        r = (exit_px - fill) / risk
        trades.append(Trade(
            code=code, entry_date=d.index[ei], entry=fill, stop=stop,
            target=target, exit_date=d.index[exit_i], exit=exit_px,
            reason=reason, r=r, trigger=trigger))
        i = exit_i + 1                          # 청산 후 재탐색(중복 진입 금지)
    return trades


def _walk_forward(d, ei: int, fill: float, stop: float,
                  max_hold: int) -> tuple[int, float, str]:
    """진입(ei) 이후 손절/목표/시간 청산까지 진행. (exit_idx, exit_px, reason)."""
    n = len(d)
    risk = fill - stop
    target = fill + config.RR_TARGET * risk
    for j in range(ei, min(ei + max_hold, n)):
        if float(d["Low"].iloc[j]) <= stop:     # 손절 우선(보수적)
            return j, stop, "stop"
        if float(d["High"].iloc[j]) >= target:
            return j, target, "target"
    exit_i = min(ei + max_hold, n) - 1
    return exit_i, float(d["Close"].iloc[exit_i]), "time"


# ─────────────────────────────────────────────────────────────
# 돌파 확인 필터 실험 — 신호를 한 번 수집해 필터별로 비교
# ─────────────────────────────────────────────────────────────
@dataclass
class Signal:
    code: str
    date: "pd.Timestamp"
    kind: str          # transition | normal
    r: float
    reason: str
    vol_mult: float    # 진입봉 거래대금 / 평균
    rsi: float
    dist_pct: float    # 추세선 대비 거리(%) — 돌파폭 근사


def collect_signals(code: str, frames: dict, meta: dict, bench=None,
                    warmup: int = 520, max_hold: int = 60) -> list[Signal]:
    """매 봉 분석해 모든 진입 신호를 '독립 거래'로 수집(중복 보유 허용).

    필터 실험용: 같은 신호 풀에 여러 필터를 적용해 공정 비교한다.
    (메인 simulate의 '한 번에 한 포지션' 규칙과 달리 신호 품질 자체를 본다.)
    """
    d = frames["D"]
    n = len(d)
    sigs: list[Signal] = []
    for i in range(warmup, n - 1):
        sub = d.iloc[:i + 1]
        bsub = bench.loc[:sub.index[-1]] if bench is not None else None
        try:
            res = analyze(data.frames_from_daily(sub), meta, bench=bsub)
        except Exception:
            continue
        if not is_entry(res):
            continue
        ei = i + 1
        fill = float(d["Open"].iloc[ei])
        stop = float(res["risk"]["stop"])
        if fill <= stop:
            continue
        exit_i, exit_px, reason = _walk_forward(d, ei, fill, stop, max_hold)
        r = (exit_px - fill) / (fill - stop)
        kind = ("transition" if res["trendline"]["state"] in _tl.TRANSITION_STATES
                else "normal")
        dist = res["trendline"].get("dist_pct")
        sigs.append(Signal(
            code=code, date=d.index[ei], kind=kind, r=r, reason=reason,
            vol_mult=float(res["volume"].get("mult", 0.0)),
            rsi=float(res["rsi"].get("rsi", 50.0)),
            dist_pct=float(dist) if dist is not None else 0.0))
    return sigs


# 실험할 필터 전략 (이름, 신호 판정 함수). '전환후보' 개선이 목표.
STRATEGIES = [
    ("전환후보 — 필터 없음",
     lambda s: s.kind == "transition"),
    ("전환후보 + 거래량≥1.5배",
     lambda s: s.kind == "transition" and s.vol_mult >= 1.5),
    ("전환후보 + RSI<70 (과열 회피)",
     lambda s: s.kind == "transition" and s.rsi < 70),
    ("전환후보 + RSI 45~65 (눌림)",
     lambda s: s.kind == "transition" and 45 <= s.rsi <= 65),
    ("전환후보 + 돌파폭≥1%",
     lambda s: s.kind == "transition" and s.dist_pct >= 1.0),
    ("전환후보 콤보 (거래량≥1.3 & RSI<70)",
     lambda s: s.kind == "transition" and s.vol_mult >= 1.3 and s.rsi < 70),
    ("(참고) 일반매수 — 필터 없음",
     lambda s: s.kind == "normal"),
    ("(참고) 전체 — 필터 없음",
     lambda s: True),
]


def summarize_signals(sigs: list[Signal]) -> Stats:
    reasons = {}
    for s in sigs:
        reasons[s.reason] = reasons.get(s.reason, 0) + 1
    return _summarize_rs([s.r for s in sigs], reasons)


def experiment(frames_map: dict[str, dict], metas: dict, bench_map: dict | None = None,
               warmup: int = 520, max_hold: int = 60) -> dict:
    """필터별 비교 실험. 결과 dict 반환 + 표 출력.

    주의: 신호를 '독립 거래'로 보므로 같은 종목에서 동시 보유가 생길 수 있다
    (필터 효과 비교가 목적 — 포지션 관리가 아니라 신호 품질을 측정).
    """
    bench_map = bench_map or {}
    sigs: list[Signal] = []
    for code, frames in frames_map.items():
        sigs += collect_signals(code, frames, metas[code],
                                bench=bench_map.get(code),
                                warmup=warmup, max_hold=max_hold)

    rows = [(name, summarize_signals([s for s in sigs if pred(s)]))
            for name, pred in STRATEGIES]

    print("=" * 78)
    print("◆ 돌파 확인 필터 실험 — '전환 후보'의 기대값을 양(+)으로 끌어올릴 수 있나?")
    print("  (신호를 독립 거래로 측정 · R=리스크단위 · 슬리피지/수수료 미반영)")
    print("=" * 78)
    print(f"  {'전략':<34}{'거래':>5}{'승률':>7}{'기대값':>9}{'PF':>7}{'최대DD':>8}")
    print("-" * 78)
    for name, s in rows:
        if s.n == 0:
            print(f"  {name:<34}{'0':>5}{'-':>7}{'-':>9}{'-':>7}{'-':>8}")
            continue
        pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
        mark = " ←양(+)" if s.expectancy > 0 else ""
        print(f"  {name:<34}{s.n:>5}{s.win_rate*100:>6.1f}%"
              f"{s.expectancy:>+8.2f}R{pf:>7}{s.max_dd_r:>7.0f}R{mark}")
    print("=" * 78)
    return {"signals": sigs, "rows": rows}


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    expectancy: float = 0.0       # 거래당 평균 R
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_dd_r: float = 0.0         # R 기준 최대낙폭
    by_reason: dict = field(default_factory=dict)


def _summarize_rs(rs: list[float], reasons: dict | None = None) -> Stats:
    """R 손익 리스트 → 통계."""
    s = Stats(n=len(rs))
    if not rs:
        return s
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    s.wins, s.losses = len(wins), len(losses)
    s.total_r = sum(rs)
    s.expectancy = s.total_r / s.n
    s.win_rate = s.wins / s.n
    s.avg_win = (sum(wins) / len(wins)) if wins else 0.0
    s.avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    gain = sum(wins)
    pain = -sum(losses)
    s.profit_factor = (gain / pain) if pain > 0 else float("inf")

    eq, peak, dd = 0.0, 0.0, 0.0   # R 기준 자산곡선의 최대낙폭
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    s.max_dd_r = dd
    s.by_reason = reasons or {}
    return s


def summarize(trades: list[Trade]) -> Stats:
    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return _summarize_rs([t.r for t in trades], reasons)


def summarize_by_trigger(trades: list[Trade]) -> dict:
    """신호 유형별 통계: {'transition': Stats, 'normal': Stats}."""
    trans = [t for t in trades if trigger_kind(t) == "transition"]
    norm = [t for t in trades if trigger_kind(t) == "normal"]
    return {"transition": summarize(trans), "normal": summarize(norm)}


def equity_curve(trades: list[Trade]) -> tuple[list, list]:
    """진입일순 누적 R 자산곡선. (x=청산일, y=누적R)."""
    ts = sorted(trades, key=lambda t: t.entry_date)
    xs, ys, cum = [], [], 0.0
    for t in ts:
        cum += t.r
        xs.append(t.exit_date)
        ys.append(round(cum, 3))
    return xs, ys


def _fmt_stats(name: str, s: Stats) -> str:
    if s.n == 0:
        return f"[{name}] 거래 없음"
    pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    rr = s.by_reason
    return (
        f"[{name}]\n"
        f"  거래수      : {s.n}건  (승 {s.wins} / 패 {s.losses})\n"
        f"  승률        : {s.win_rate*100:.1f}%\n"
        f"  기대값      : {s.expectancy:+.2f}R / 거래  "
        f"(평균 익 {s.avg_win:+.2f}R · 평균 손 {s.avg_loss:+.2f}R)\n"
        f"  누적손익    : {s.total_r:+.1f}R  "
        f"(≈ 계좌 {s.total_r*config.RISK_PER_TRADE*100:+.1f}%, 1%리스크 가정)\n"
        f"  손익비(PF)  : {pf}\n"
        f"  최대낙폭    : {s.max_dd_r:.1f}R\n"
        f"  청산내역    : 목표 {rr.get('target',0)} · 손절 {rr.get('stop',0)} · 시간 {rr.get('time',0)}")


def run(frames_map: dict[str, dict], metas: dict[str, dict],
        bench_map: dict | None = None,
        warmup: int = 520, max_hold: int = 60) -> dict:
    """종목별 + 전체 통합 백테스트. 결과 dict 반환 + 보고서 출력."""
    bench_map = bench_map or {}
    all_trades: list[Trade] = []
    per_code = {}
    for code, frames in frames_map.items():
        ts = simulate(code, frames, metas[code], bench=bench_map.get(code),
                      warmup=warmup, max_hold=max_hold)
        per_code[code] = ts
        all_trades += ts

    print("=" * 64)
    print("리스크 관리 백테스트 (차트 전용) — R = 리스크 단위(손절폭)")
    print(f"진입: 매수/전환후보 신호 & 하락추세 veto 제외 | 손익비 1:{config.RR_TARGET:.0f} | 최대보유 {max_hold}거래일")
    print("=" * 64)
    for code, ts in per_code.items():
        name = metas[code]["name"]
        print(_fmt_stats(f"{name} {code}", summarize(ts)))
        print("-" * 64)
    total = summarize(all_trades)
    print(_fmt_stats("전체 통합", total))
    print("=" * 64)

    # ── 신호 유형별 (핵심: 전환 후보가 일반 매수보다 나은가?) ──
    by_trig = summarize_by_trigger(all_trades)
    print("◆ 신호 유형별 — '전환 후보'가 일반 매수보다 나은가?")
    print(_fmt_stats("전환 후보 (하락추세선 상향돌파)", by_trig["transition"]))
    print("-" * 64)
    print(_fmt_stats("일반 매수 (매수/적극매수 신호)", by_trig["normal"]))
    print("=" * 64)

    # ── 표본외(Out-of-Sample) 검증: 시간순 70/30 분할 ──
    #    앞 70%(과거)에서 보이던 엣지가 뒤 30%(미래)에서도 유지되나? 과적합 점검.
    oos = oos_split(all_trades)
    print("◆ 표본외(OOS) 검증 — 시간순 70/30 분할(과적합·미래 일반화 점검)")
    print(_fmt_stats("앞 70% (인샘플·과거)", oos["train"]))
    print("-" * 64)
    print(_fmt_stats("뒤 30% (표본외·미래)", oos["test"]))
    if oos["train"].n and oos["test"].n:
        drop = oos["train"].expectancy - oos["test"].expectancy
        verdict = ("표본외에서도 양(+) 유지 → 비교적 견고"
                   if oos["test"].expectancy > 0
                   else "표본외에서 엣지 소멸 → 과적합/생존편향 의심, 신뢰 낮음")
        print(f"  · 기대값 변화: {oos['train'].expectancy:+.2f}R → "
              f"{oos['test'].expectancy:+.2f}R (Δ{-drop:+.2f}R) · {verdict}")
    print("=" * 64)
    print("주의(신뢰도 한계):")
    print("  · 생존편향 — S&P500은 '살아남아 성공한 기업'만 모은 집합이라 과거가 실제보다")
    print("    좋게 나온다. 상장폐지·편출된 종목은 표본에 없다(엣지 과대평가 가능).")
    print("  · 표본·기간이 제한적이고 슬리피지·수수료·세금·어닝갭은 미반영.")
    print("  · 해석은 승률보다 '기대값(R)·최대낙폭'과 위 표본외 결과를 우선으로 본다.")
    return {"per_code": per_code, "all": all_trades, "total": total,
            "by_trigger": by_trig, "oos": oos}


def oos_split(trades: list[Trade], train_frac: float = 0.7) -> dict:
    """진입일 기준 시간순 분할 → 앞부분(인샘플)/뒷부분(표본외) 통계."""
    ts = sorted(trades, key=lambda t: t.entry_date)
    k = int(len(ts) * train_frac)
    return {"train": summarize(ts[:k]), "test": summarize(ts[k:]),
            "cut": ts[k].entry_date if 0 < k < len(ts) else None}
