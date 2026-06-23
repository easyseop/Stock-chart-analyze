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
        backtest: bool = False, use_cache: bool = False,
        universe_scan: int = 0, universe_path: str = "universe.csv",
        screener: bool = False, screener_dir: str = "public",
        scan_cached: bool = False):
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
    elif scan_cached:
        # 캐시된 모든 종목을 스크리너 대상으로(즉석조회로 추가된 것까지 자동 포함)
        from scanner import cache, universe
        umap = {s["code"]: s for s in universe.load(universe_path) if s.get("code")}
        jobs = []
        for code in cache.cached_codes():
            m = umap.get(code) or {
                "code": code, "name": code,
                "ccy": "KRW" if (len(code) == 6 and code[:5].isdigit()) else "USD"}
            jobs.append((m, None))
    elif universe_scan:
        from scanner import universe
        uni = [s for s in universe.load(universe_path) if s.get("code")][:universe_scan]
        if use_cache:
            # 캐시된 종목만 렌더(빌드 중 대량 재수집 방지 — 수집은 --backfill/--update가 담당)
            from scanner import cache
            uni = [s for s in uni if cache.is_cached(s["code"])]
        jobs = [(s, None) for s in uni]
    else:
        jobs = [(s, None) for s in config.STOCKS if s.get("code")]

    for meta, scenario in jobs:
        try:
            if demo:
                frames = _frames_demo(scenario)
            elif use_cache:
                from scanner import cache
                # 대량 모드(universe_scan/scan_cached)는 캐시만 읽음(네트워크 0). 워치리스트는 증분 갱신.
                frames = cache.frames(meta["code"],
                                      refresh=not (universe_scan or scan_cached))
            else:
                frames = _frames_real(meta["code"])
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

    if screener:
        if results:
            from scanner import screener as scrmod
            out = scrmod.build(results, frames_map, out_dir=screener_dir, metas=metas)
            print(f"스크리너 저장: {out}/index.html ({len(results)}종목) "
                  f"+ {out}/stocks/*.html")
        else:
            print("분석 결과가 없어 스크리너를 생성하지 않음", file=sys.stderr)

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
    ap.add_argument("--cache", action="store_true",
                    help="로컬 증분 캐시(data_cache/) 사용 — 신규 봉만 받아 빠름")
    ap.add_argument("--backfill", type=int, metavar="N", default=0,
                    help="아직 캐시 없는 종목을 최대 N개까지 전체이력 백필(하루 N개씩 분산용)")
    ap.add_argument("--update", action="store_true",
                    help="캐시된 모든 종목을 증분 갱신(신규 봉만)")
    ap.add_argument("--universe", metavar="PATH", default="universe.csv",
                    help="유니버스 파일(code,name,ccy) — 백필/갱신 대상")
    ap.add_argument("--build-universe", type=int, metavar="KOSPI_TOP", nargs="?",
                    const=200, default=None,
                    help="S&P500 + KOSPI 시총상위 N으로 유니버스 자동 구성(기본 200)")
    ap.add_argument("--build-universe-max", action="store_true",
                    help="최대 유니버스: S&P500 + KOSPI 전체 + KOSDAQ 전체(~3,300종목)")
    ap.add_argument("--build-universe-us", action="store_true",
                    help="미국 전용 유니버스(S&P500). 자동 수집은 미장만, 한국주는 수시 조회")
    ap.add_argument("--build-universe-us-all", action="store_true",
                    help="미국 보통주 전체(NASDAQ+NYSE, ETF/잡주 제외 ~5천). 백필 후 --prune illiquid:N으로 거래대금 상위만 유지")
    ap.add_argument("--ticker", metavar="SYM",
                    help="티커/코드 즉석 조회(수시) — 바로 받아 카드+상세HTML 생성")
    ap.add_argument("--ccy", default=None, help="--ticker 통화(USD/KRW). 미지정 시 자동")
    ap.add_argument("--universe-scan", type=int, metavar="N", default=0,
                    help="분석/백테스트 대상을 워치리스트 대신 유니버스 앞 N종목으로(대량 검증)")
    ap.add_argument("--screener", action="store_true",
                    help="대량 종목 스크리너(가벼운 표 index + 종목별 상세 페이지) 생성")
    ap.add_argument("--screener-dir", default="public", help="스크리너 출력 폴더")
    ap.add_argument("--scan-cached", action="store_true",
                    help="캐시된 모든 종목을 스크리너 대상으로(즉석조회 추가분 포함)")
    ap.add_argument("--add", metavar="SYM",
                    help="티커/코드를 캐시에 추가·갱신(즉석조회를 스크리너에 영구 반영)")
    ap.add_argument("--prune", metavar="SPEC",
                    help="캐시 정리: 'korean'=한국 6자리 전부 / 'CODE[,CODE]'=지정 종목")
    ap.add_argument("--update-hourly", type=int, metavar="N", default=0,
                    help="전환후보 상위 N종목의 시간봉(60분) 캐시 갱신(best-effort, yfinance)")
    ap.add_argument("--update-earnings", action="store_true",
                    help="캐시 종목의 다음 실적발표일 갱신(best-effort, yfinance, 하루 1회)")
    args = ap.parse_args()

    if args.prune:
        _prune(args.prune)
        return

    if args.update_hourly:
        _update_hourly(args.update_hourly)
        return

    if args.update_earnings:
        from scanner import cache, earnings
        n = earnings.refresh(cache.cached_codes())
        print(f"어닝 캐시 갱신: {n}종목 발표일 확보 / 캐시 {len(cache.cached_codes())}종목")
        return

    if args.add:
        from scanner import cache
        code = args.add.strip().upper()
        try:
            d = cache.update(code)
            print(f"캐시 추가/갱신: {code} ({len(d)}행) · 총 {len(cache.cached_codes())}종목")
        except Exception as e:
            print(f"[{code}] 캐시 추가 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return

    if args.ticker:
        _lookup(args.ticker, args.ccy)
        return

    if args.build_universe_us_all:
        from scanner import universe
        rows = universe.build_us_all(args.universe)
        print(f"미국 보통주 유니버스: {len(rows)}종목 → {args.universe} "
              f"(백필 후 --prune 'illiquid:2000'으로 거래대금 상위만 유지)")
        return

    if args.build_universe_us or args.build_universe_max or args.build_universe is not None:
        from scanner import universe
        if args.build_universe_us:
            rows = universe.build(args.universe)                      # 미국 전용
        elif args.build_universe_max:
            rows = universe.build(args.universe, kospi_top=0, kosdaq_top=0)
        else:
            rows = universe.build(args.universe, kospi_top=args.build_universe)
        us = sum(1 for r in rows if r["ccy"] == "USD")
        kr = sum(1 for r in rows if r["ccy"] == "KRW")
        print(f"유니버스 구성: {len(rows)}종목 (미국 {us} · 한국 {kr}) → {args.universe}")
        return

    if args.backfill or args.update:
        _manage_cache(args.backfill, args.update, args.universe)
        return

    run(demo=args.demo, csv_path=args.csv, chart=args.chart,
        png=args.png, chart_dir=args.chart_dir,
        dashboard=args.dashboard, dashboard_path=args.dashboard_path,
        backtest=args.backtest, use_cache=args.cache,
        universe_scan=args.universe_scan, universe_path=args.universe,
        screener=args.screener, screener_dir=args.screener_dir,
        scan_cached=args.scan_cached)


def _prune(spec: str):
    """캐시 정리: 'korean' / 'illiquid:N'(거래대금 상위 N만 유지) / 'CODE,CODE'."""
    from scanner import cache
    spec = spec.strip()
    if spec.lower().startswith("illiquid:"):
        _prune_illiquid(int(spec.split(":", 1)[1]))
        return
    cached = cache.cached_codes()
    if spec.lower() == "korean":
        targets = [c for c in cached if len(c) == 6 and c.isdigit()]
    else:
        want = {s.strip().upper() for s in spec.split(",") if s.strip()}
        targets = [c for c in cached if c in want]
    if not targets:
        print(f"정리 대상 없음(spec={spec!r}). 캐시 {len(cached)}종목 유지.")
        return
    n = sum(1 for c in targets if cache.remove(c))
    print(f"캐시 정리: {n}종목 삭제 ({', '.join(targets)}) · "
          f"남은 {len(cache.cached_codes())}종목")


def _prune_illiquid(keep: int):
    """거래대금(가격×거래량, 최근 60일 평균) 상위 keep개만 남기고 캐시·유니버스 정리.

    백필 완료 후 1회 실행 권장(미수집 종목이 많으면 그만큼만 평가됨).
    """
    import pandas as pd
    from scanner import cache, universe
    cached = cache.cached_codes()
    scored = []
    for code in cached:
        d = cache.load(code)
        if d is None or len(d) < 20:
            scored.append((0.0, code))
            continue
        tail = d.iloc[-60:]
        turnover = float((tail["Close"] * tail["Volume"]).mean())
        scored.append((turnover, code))
    scored.sort(reverse=True)
    keep_codes = {c for _t, c in scored[:keep]}
    drop = [c for _t, c in scored[keep:]]
    removed = sum(1 for c in drop if cache.remove(c))
    # 유니버스도 상위 keep으로 재작성 → 이후 백필이 잡주를 다시 안 받음
    umap = {s["code"]: s for s in universe.load() if s.get("code")}
    new_rows = [umap.get(c) or {"code": c, "name": c, "ccy": "USD"}
                for _t, c in scored[:keep]]
    universe.save(new_rows)
    print(f"거래대금 상위 {keep}종목 유지 · {removed}종목 캐시 삭제 · "
          f"유니버스 {len(new_rows)}종목으로 재작성 (남은 캐시 {len(cache.cached_codes())})")


def _update_hourly(n: int):
    """전환후보 상위 N종목의 시간봉 캐시를 갱신(best-effort).

    캐시된 일봉으로 분석(네트워크 0)해 전환단계·점수 상위를 고르고, 그 종목만
    yfinance에서 60분봉을 받는다. yfinance 없거나 실패해도 조용히 건너뛴다.
    """
    from scanner import cache, universe, intraday
    from scanner.analyze import analyze
    umap = {s["code"]: s for s in universe.load() if s.get("code")}
    ranked = []
    for code in cache.cached_codes():
        if len(code) == 6 and code[:5].isdigit():
            continue                              # 미국주만(yfinance 시간봉)
        try:
            frames = cache.frames(code, refresh=False)
            meta = umap.get(code) or {"code": code, "name": code, "ccy": "USD"}
            res = analyze(frames, meta)           # bench 없음 OK(RS/시장 0점 처리)
            ranked.append((res.get("transition_stage", 0), res["norm"], code))
        except Exception:
            continue
    ranked.sort(reverse=True)
    targets = [c for _s, _n, c in ranked[:n]]
    ok = 0
    for code in targets:
        if intraday.update(code) is not None:
            ok += 1
    print(f"시간봉 갱신 시도 {len(targets)}종목 · 성공 {ok} "
          f"(캐시 {len(intraday.cached_codes())}종목)")


def _lookup(ticker: str, ccy: str | None):
    """티커/코드 즉석 조회: 실데이터 받아 카드 출력 + 상세 차트 HTML 1장 생성."""
    from scanner import data, card, screener
    from scanner.analyze import analyze
    code = ticker.strip().upper()
    if not ccy:
        ccy = "KRW" if (len(code) == 6 and code[:5].isdigit()) else "USD"
    meta = {"code": code, "name": code, "ccy": ccy}
    try:
        frames = data.build_frames(code)
    except Exception as e:
        print(f"[{code}] 데이터 수집 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return
    bench = data.fetch_benchmark(ccy)
    res = analyze(frames, meta, bench=bench)
    print(card.render(res))
    # 시간봉(미국주만, best-effort) — 있으면 상세 차트에 '시간봉' 탭 추가
    if ccy != "KRW":
        from scanner import intraday
        h = intraday.update(code)
        if h is not None and len(h) >= config.MA_PERIODS["H"][-1] + 5:
            frames = {**frames, "H": h}
    out = f"lookup_{code}.html"
    with open(out, "w", encoding="utf-8") as fp:
        fp.write(screener._detail(res, frames))
    print(f"\n상세 차트(시간/일/주/월·토글): {out}")


def _manage_cache(backfill_n: int, do_update: bool, universe_path: str):
    """캐시 백필/증분 갱신 (분석 없이 데이터만). 대상 = 유니버스."""
    from scanner import cache, universe
    uni = universe.load(universe_path)
    if backfill_n:
        todo = [s for s in uni
                if s.get("code") and not cache.is_cached(s["code"])][:backfill_n]
        print(f"백필 대상 {len(todo)}종목(전체이력 1회 수집)…")
        for s in todo:
            try:
                d = cache.update(s["code"])
                print(f"  ✓ {s['code']:8} {len(d)}행")
            except Exception as e:
                print(f"  ✗ {s['code']:8} 실패: {type(e).__name__}: {e}",
                      file=sys.stderr)
    if do_update:
        codes = cache.cached_codes()
        print(f"증분 갱신 {len(codes)}종목(신규 봉만)…")
        for code in codes:
            try:
                cache.update(code)
            except Exception as e:
                print(f"  ✗ {code} 실패: {e}", file=sys.stderr)
    print(f"캐시 현황: {len(cache.cached_codes())}종목 · "
          f"{cache.total_size_mb():.2f} MB ({cache.CACHE_DIR}/)")


if __name__ == "__main__":
    main()
