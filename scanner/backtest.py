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
    print("주의: 표본이 적어 통계적 신뢰구간이 넓다(참고용). 슬리피지·수수료·세금 미반영.")
    print("     해석은 승률보다 '기대값(R)·최대낙폭'을 우선으로 본다.")
    return {"per_code": per_code, "all": all_trades, "total": total,
            "by_trigger": by_trig}
