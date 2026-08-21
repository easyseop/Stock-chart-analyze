#!/usr/bin/env python3
"""저장된 일별 기록의 **지수 등락률**을 야후 일봉으로 소급 정정한다.

왜(2026-08-21 실측): `_yahoo_quote()`가 쓰던 야후 meta의
`regularMarketPreviousClose`는 코스피·코스닥·나스닥 전부 None이라 폴백인
`chartPreviousClose`가 상시 경로였다. 그런데 그 값은 직전 *거래일* 종가를
보장하지 않는다(같은 시각 코스닥이 08-20 종가 840.89 대신 08-19의 824.46을
반환). 기준선은 세션 시작 때 한 번 고정되므로 아침에 하루 밀린 값을 잡으면
그날 지수가 통째로 오염된다 — 코스피 실제 +0.88%가 +7.12%로 발행됐다.

`capture_stats()`가 이 값을 **분모로** 쓰기 때문에(상승일 캡처·지수 이긴 날)
오염된 하루는 누적 통계 전체를 끌고 간다. 야후 일봉이라는 독립적 정답지가
있으므로 검증 가능한 정정이 가능하다.

원칙:
  · 주문 없음 · 계좌 조회 없음 — 상태 파일 1개만 읽고 쓴다.
  · 기본은 미리보기(dry-run). 실제 저장은 --apply 필요.
  · 저장 전 상태 파일을 타임스탬프 백업으로 남긴다.
  · **계좌 수익률(acct/a/b)은 절대 건드리지 않는다** — 지수 자리만 정정한다.
  · `basis == "first_sample"` 행은 건너뛴다. 리베이스 첫날은 계좌 TWR과 같은
    첫 관측값을 지수 기준으로 쓰도록 **의도된** 설계라, 전일종가 기준으로
    바꾸면 그날만 사과와 오렌지를 비교하게 된다.
  · 정정 내역은 감사 원장(alpha_index_repair.jsonl)에 append 한다.

사용:
  python scripts/alpha_repair_index.py                 # 미리보기(전체)
  python scripts/alpha_repair_index.py --mkt KR        # 시장 한정
  python scripts/alpha_repair_index.py --apply         # 실제 저장
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import alpha, settings  # noqa: E402

TOLERANCE_PP = 0.01          # 이 이하 차이는 반올림 잡음 — 정정하지 않는다
REPAIR_LEDGER = os.environ.get(
    "ALPHA_INDEX_REPAIR_PATH",
    os.path.join(os.path.dirname(alpha.STATE_PATH), "alpha_index_repair.jsonl"))


def _bars_by_symbol(symbols: list[str], rng: str) -> dict[str, list] | None:
    """심볼별 일봉. 하나라도 실패하면 None — 부분 정정은 하지 않는다."""
    out: dict[str, list] = {}
    for sym in symbols:
        bars = alpha.yahoo_daily_bars(sym, rng=rng)
        if bars is None:
            print(f"✗ {sym} 일봉 조회 실패 — 정정 중단(실패≠부재)")
            return None
        if len(bars) < 2:
            print(f"✗ {sym} 일봉이 {len(bars)}개뿐 — 정정 중단")
            return None
        out[sym] = bars
    return out


def true_daily_pct(bars: list, date: str) -> float | None:
    """`date` 세션의 전일종가 대비 등락률(%). 그날 봉이 없으면 None."""
    index = {d: i for i, (d, _c) in enumerate(bars)}
    i = index.get(date)
    if i is None or i == 0:
        return None
    prev_close = bars[i - 1][1]
    if prev_close <= 0:
        return None
    return (bars[i][1] / prev_close - 1) * 100.0


def plan_row(row: dict, bars_by_sym: dict[str, list]) -> dict | None:
    """행 1개의 정정안. 정정할 게 없으면 None."""
    mkt = str(row.get("mkt") or "")
    date = str(row.get("d") or "")
    if mkt not in alpha.IDX:
        return None
    if str(row.get("basis") or "first_sample") != "previous_close":
        return {"skip": "basis=first_sample(의도된 세션기준)", "d": date, "mkt": mkt}

    truth: dict[str, float | None] = {}
    for sym, name in alpha.IDX[mkt]:
        pct = true_daily_pct(bars_by_sym[sym], date)
        truth[name] = alpha._sane_idx_pct(pct, name=name)
    if all(v is None for v in truth.values()):
        return {"skip": "일봉에 해당 거래일 없음(범위 밖·휴장)", "d": date, "mkt": mkt}

    primary = alpha.IDX[mkt][0][1]
    changes: list[dict] = []

    def _cmp(field: str, key: str | None, old, new):
        if new is None and old is None:
            return
        if old is not None and new is not None and abs(float(old) - new) <= TOLERANCE_PP:
            return
        # 두 종류를 구분해서 보고한다. '정정'은 이미 발행된 **틀린 숫자**를
        #   바꾸는 것이고(누적 통계 오염), '보강'은 비어 있던 자리를 채우는
        #   것이다(오염 아님). 섞어 세면 오염된 날 수가 부풀려 보인다.
        changes.append({"field": field, "key": key, "old": old,
                        "new": None if new is None else round(new, 4),
                        "kind": "보강" if old is None else "정정"})

    _cmp("idx", None, row.get("idx"), truth.get(primary))
    for name, value in truth.items():
        _cmp("indices", name, (row.get("indices") or {}).get(name), value)
        _cmp("daily_indices", name,
             (row.get("daily_indices") or {}).get(name), value)
    if not changes:
        return None
    return {"d": date, "mkt": mkt, "changes": changes, "truth": truth,
            "primary": primary,
            "corrupted": any(c["kind"] == "정정" for c in changes)}


def apply_plan(row: dict, plan: dict) -> None:
    primary = plan["primary"]
    truth = plan["truth"]
    rnd = lambda v: None if v is None else round(v, 4)
    row["idx"] = rnd(truth.get(primary))
    row["indices"] = {name: rnd(value) for name, value in truth.items()}
    row["daily_indices"] = {name: rnd(value) for name, value in truth.items()}
    row["index_repaired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


def _ledger_append(record: dict) -> None:
    try:
        with open(REPAIR_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(REPAIR_LEDGER, 0o600)
    except Exception as exc:                    # 감사 기록 실패가 정정을 막지 않는다
        print(f"  (감사 원장 기록 실패: {type(exc).__name__}: {exc})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일별 지수 등락률 소급 정정")
    ap.add_argument("--mkt", choices=("US", "KR"), help="시장 한정(기본 전체)")
    ap.add_argument("--range", default="2y", help="야후 일봉 조회 범위(기본 2y)")
    ap.add_argument("--apply", action="store_true", help="실제 저장(기본 미리보기)")
    ap.add_argument("--force", action="store_true",
                    help="장중에도 강행(권장하지 않음)")
    args = ap.parse_args(argv)

    # 장중 금지. alpha._save()는 원자적이지만 **락이 없다** — 파수꾼이 5분마다
    #   같은 파일에 read-modify-write 하므로, 장중에 이 스크립트가 끼어들면
    #   마지막 저장이 이기면서 그 사이 세션 표본이 통째로 날아갈 수 있다.
    if args.apply and not args.force:
        live = [ccy for ccy in ("KRW", "USD") if settings.market_open(ccy)]
        if live:
            print(f"✗ 장중({'·'.join(live)})에는 정정하지 않습니다 — "
                  "alpha 상태 파일에 락이 없어 파수꾼 저장과 충돌합니다.\n"
                  "  장 마감 후 다시 실행하세요(정말 필요하면 --force).")
            return 3

    st = alpha._load()
    fingerprint = st.get("updated_at")
    days = st.get("days") or []
    targets = [r for r in days
               if not args.mkt or str(r.get("mkt")) == args.mkt]
    if not targets:
        print("정정 대상 행이 없습니다 — 변경 없음")
        return 1

    symbols = sorted({sym for mkt in ("US", "KR") for sym, _n in alpha.IDX[mkt]})
    bars = _bars_by_symbol(symbols, args.range)
    if bars is None:
        return 2

    plans: list[tuple[dict, dict]] = []
    skipped: list[dict] = []
    for row in targets:
        plan = plan_row(row, bars)
        if plan is None:
            continue
        if plan.get("skip"):
            skipped.append(plan)
            continue
        plans.append((row, plan))

    corrupted = [p for _r, p in plans if p["corrupted"]]
    backfill = [p for _r, p in plans if not p["corrupted"]]
    print(f"검사 {len(targets)}행 · ★오염 정정 {len(corrupted)}행 · "
          f"결측 보강 {len(backfill)}행 · 건너뜀 {len(skipped)}행 · "
          f"이상 없음 {len(targets) - len(plans) - len(skipped)}행\n")
    for plan in skipped:
        print(f"  건너뜀 {plan['mkt']} {plan['d']} — {plan['skip']}")
    if skipped:
        print()
    for label, group in (("★ 오염 정정(누적 통계에 영향)", corrupted),
                         ("결측 보강(오염 아님)", backfill)):
        if not group:
            continue
        print(f"{label}")
        for plan in group:
            print(f"  {plan['mkt']} {plan['d']}")
            for c in plan["changes"]:
                key = f"[{c['key']}]" if c["key"] else ""
                print(f"      {c['kind']} {c['field']}{key}: "
                      f"{c['old']} → {c['new']}")
        print()
    if not plans:
        print("정정할 항목이 없습니다.")
        return 0
    if not args.apply:
        print("\n미리보기입니다. 실제로 저장하려면 --apply 를 붙이세요.")
        return 0

    # 낙관적 동시성 — 읽은 뒤 누가 썼으면 덮어쓰지 않는다.
    if alpha._load().get("updated_at") != fingerprint:
        print("✗ 읽은 뒤 상태 파일이 바뀌었습니다(다른 프로세스가 저장). "
              "변경 없이 중단 — 다시 실행하세요.")
        return 4

    path = alpha.STATE_PATH
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for row, plan in plans:
        _ledger_append({"ev": "index_repair", "at": stamp, "d": plan["d"],
                        "mkt": plan["mkt"], "changes": plan["changes"]})
        apply_plan(row, plan)
    alpha._save(st)
    print(f"\n✅ {len(plans)}행 정정 완료 · 백업 {backup}")
    print(f"   감사 원장 {REPAIR_LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
