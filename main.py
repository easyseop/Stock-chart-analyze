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
        chart: bool = False, png: bool = False, chart_dir: str = "charts"):
    results = []
    charts = []

    if demo:
        scenarios = [("uptrend", "데모-상승추세"), ("box", "데모-횡보박스"),
                     ("breakdown", "데모-박스이탈"), ("downtrend", "데모-하락추세")]
        jobs = [({"code": f"DEMO_{s.upper()}", "name": name, "ccy": "USD"}, s)
                for s, name in scenarios]
    else:
        jobs = [(s, None) for s in config.STOCKS if s.get("code")]

    for meta, scenario in jobs:
        try:
            frames = _frames_demo(scenario) if demo else _frames_real(meta["code"])
            res = analyze(frames, meta)
            results.append(res)
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
    args = ap.parse_args()
    run(demo=args.demo, csv_path=args.csv, chart=args.chart,
        png=args.png, chart_dir=args.chart_dir)


if __name__ == "__main__":
    main()
