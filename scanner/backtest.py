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


def is_entry(res: dict) -> bool:
    """차트 신호가 '진입'인가(하락추세 veto 제외)."""
    if res["vetoed"]:
        return False
    state = res["trendline"]["state"]
    label = res["verdict_label"]
    return (state == "하락추세선 상향돌파"
            or label.startswith("매수") or label.startswith("적극 매수"))


def simulate(code: str, frames: dict, meta: dict,
             warmup: int = 520, max_hold: int = 60) -> list[Trade]:
    """한 종목 시점별 워크포워드 시뮬레이션."""
    d = frames["D"]
    n = len(d)
    trades: list[Trade] = []
    i = warmup
    while i < n - 1:
        sub = d.iloc[:i + 1]
        try:
            res = analyze(data.frames_from_daily(sub), meta)
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
        trigger = res["trendline"]["state"] if res["trendline"]["state"] == "하락추세선 상향돌파" else res["verdict_label"]

        # 보유 구간 워크포워드
        exit_i, exit_px, reason = None, None, None
        for j in range(ei, min(ei + max_hold, n)):
            lo = float(d["Low"].iloc[j])
            hi = float(d["High"].iloc[j])
            if lo <= stop:                      # 손절 우선(보수적)
                exit_i, exit_px, reason = j, stop, "stop"
                break
            if hi >= target:
                exit_i, exit_px, reason = j, target, "target"
                break
        if exit_i is None:                      # 시간 손절(종가)
            exit_i = min(ei + max_hold, n) - 1
            exit_px = float(d["Close"].iloc[exit_i])
            reason = "time"

        r = (exit_px - fill) / risk
        trades.append(Trade(
            code=code, entry_date=d.index[ei], entry=fill, stop=stop,
            target=target, exit_date=d.index[exit_i], exit=exit_px,
            reason=reason, r=r, trigger=trigger))
        i = exit_i + 1                          # 청산 후 재탐색(중복 진입 금지)
    return trades


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


def summarize(trades: list[Trade]) -> Stats:
    s = Stats(n=len(trades))
    if not trades:
        return s
    rs = [t.r for t in trades]
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

    # R 기준 자산곡선의 최대낙폭
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    s.max_dd_r = dd

    reasons = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    s.by_reason = reasons
    return s


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
        warmup: int = 520, max_hold: int = 60) -> dict:
    """종목별 + 전체 통합 백테스트. 결과 dict 반환 + 보고서 출력."""
    all_trades: list[Trade] = []
    per_code = {}
    for code, frames in frames_map.items():
        ts = simulate(code, frames, metas[code], warmup=warmup, max_hold=max_hold)
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
    print("주의: 표본이 적어 통계적 신뢰구간이 넓다(참고용). 슬리피지·수수료·세금 미반영.")
    print("     해석은 승률보다 '기대값(R)·최대낙폭'을 우선으로 본다.")
    return {"per_code": per_code, "all": all_trades, "total": total}
