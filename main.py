"""심리 신호 스캐너 v1 — CLI 진입점.

사용:
  python main.py                # config.STOCKS 전체 스캔 (실데이터)
  python main.py --csv out.csv  # 결과를 CSV로도 저장
  python main.py --demo         # 합성 데이터로 파이프라인 데모(네트워크 불필요)
"""
from __future__ import annotations

import argparse
import csv
import sys

import config
from scanner import data, card
from scanner.analyze import analyze


def _frames_real(code):
    return data.build_frames(code)


def _frames_demo(scenario):
    from tests.sample_data import make
    return data.frames_from_daily(make(scenario))


def run(demo: bool = False, csv_path: str | None = None,
        chart: bool = False, png: bool = False, chart_dir: str = "charts",
        dashboard: bool = False, dashboard_path: str = "dashboard.html",
        backtest: bool = False):
    results = []
    charts = []
    frames_map = {}
    metas = {}
    bench_map = {}

    if demo:
        scenarios = [("uptrend", "데모-상승추세"), ("box", "데모-횡보박스"),
                     ("breakdown", "데모-박스이탈"), ("downtrend", "데모-하락추세"),
                     ("reversal", "데모-추세전환")]
        jobs = [({"code": f"DEMO_{s.upper()}", "name": name, "ccy": "USD"}, s)
                for s, name in scenarios]
    else:
        jobs = [(s, None) for s in config.STOCKS if s.get("code")]

    for meta, scenario in jobs:
        try:
            frames = _frames_demo(scenario) if demo else _frames_real(meta["code"])
            bench = None if demo else data.fetch_benchmark(meta.get("ccy", "USD"))
            res = analyze(frames, meta, bench=bench)
            results.append(res)
            frames_map[res["code"]] = frames
            bench_map[res["code"]] = bench
            metas[res["code"]] = meta
            print(card.render(res))
            print()
            if chart:
                from scanner import chart as chartmod
                path = chartmod.save(res, frames, out_dir=chart_dir, png=png)
                charts.append(path)
        except Exception as e:
            print(f"[{meta.get('name','?')} {meta.get('code','?')}] "
                  f"분석 실패: {type(e).__name__}: {e}\n", file=sys.stderr)

    if csv_path:
        if results:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as fp:
                w = csv.DictWriter(fp, fieldnames=card.CSV_FIELDS)
                w.writeheader()
                for r in results:
                    w.writerow(card.to_row(r))
            print(f"CSV 저장: {csv_path} ({len(results)}종목)")
        else:
            print(f"분석 결과가 없어 CSV를 생성하지 않음: {csv_path}", file=sys.stderr)

    if charts:
        print(f"차트 저장: {chart_dir}/ ({len(charts)}개)")

    bt_result = exp_result = None
    if backtest:
        if frames_map:
            from scanner import backtest as bt
            bt_result = bt.run(frames_map, metas, bench_map=bench_map)
            exp_result = bt.experiment(frames_map, metas, bench_map=bench_map)
        else:
            print("데이터가 없어 백테스트를 실행하지 않음", file=sys.stderr)

    if dashboard:
        if results:
            from scanner import dashboard as dashmod
            path = dashmod.build(results, frames_map, out_path=dashboard_path,
                                 backtest=bt_result, metas=metas,
                                 experiment=exp_result)
            print(f"대시보드 저장: {path} ({len(results)}종목"
                  f"{', 백테스트 포함' if bt_result else ''})")
        else:
            print("분석 결과가 없어 대시보드를 생성하지 않음", file=sys.stderr)

    return results


def main():
    ap = argparse.ArgumentParser(description="심리 신호 스캐너 v1")
    ap.add_argument("--demo", action="store_true",
                    help="합성 데이터로 데모(네트워크 불필요)")
    ap.add_argument("--csv", metavar="PATH", help="결과 CSV 저장 경로")
    ap.add_argument("--chart", action="store_true",
                    help="종목별 차트(HTML) 생성 — 박스권·방어선·진입/손절 선")
    ap.add_argument("--png", action="store_true",
                    help="차트를 PNG로도 저장(kaleido+chrome 필요)")
    ap.add_argument("--chart-dir", default="charts", help="차트 저장 폴더")
    ap.add_argument("--dashboard", action="store_true",
                    help="5종목 단일 페이지 대시보드(HTML) 생성 — 토글·전환후보 분류")
    ap.add_argument("--dashboard-path", default="dashboard.html",
                    help="대시보드 저장 경로")
    ap.add_argument("--backtest", action="store_true",
                    help="차트 전용 리스크 관리 백테스트(R 단위 기대값·최대낙폭)")
    args = ap.parse_args()
    run(demo=args.demo, csv_path=args.csv, chart=args.chart,
        png=args.png, chart_dir=args.chart_dir,
        dashboard=args.dashboard, dashboard_path=args.dashboard_path,
        backtest=args.backtest)


if __name__ == "__main__":
    main()
