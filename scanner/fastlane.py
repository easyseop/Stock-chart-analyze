"""빠른 차선(fast lane) — 시세→매매→피드만 ~3분에 갱신 (L2 분리, RELIABILITY A1).

왜: 벌크 빌드는 [5,400종목 분석 → 자동매매 → 5,400페이지 렌더 → 배포]가 한
줄이라, 렌더·배포가 밀리면 매매까지 밀린다(실측 F4: 재렌더 3연속 → 가격 빌드
33분+ 지연). 이 모듈은 매매에 필요한 최소 집합(보유 + 대기주문 + 최근 후보)만
증분 수집→분석→게이트→자동매매 1스텝을 수행하고 렌더·배포는 건너뛴다.

원칙:
  1) 분석·게이트·매매는 벌크와 **같은 함수**를 재사용한다(로직 분기 금지 —
     차선이 달라도 같은 데이터에는 같은 결정). 여기서 조건을 추가하지 말 것.
  2) 후보 발굴(L1)은 벌크의 몫 — fast는 마지막 풀스캔의 신호 피드에서 후보를
     받아 '유지·집행'만 한다. 새 후보는 다음 벌크 스캔에서 들어온다.
  3) 계좌 정합성은 autopaper의 매매 분산 락 + state 스냅샷 최신성 채택(F8)이
     보장 — fast/bulk가 겹쳐도 이중 매매·체결 유실이 없다.

실행: python main.py --fast   (daily.yml fast 잡 / CF Worker가 발사)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

FAST_MAX = 80          # 한 사이클 최대 심볼 수(보유+대기 우선, 후보는 잔여 슬롯)
FEED_RAW = ("https://raw.githubusercontent.com/{repo}/state/"
            "feed/signals.latest.json")


def _feed_signals() -> list[dict]:
    """후보 소스(우선순위): ① 로컬 public/api/signals.json(캐시 복원분)
    ② CI: state 브랜치 git show ③ HTTPS raw(로컬 스모크용). 실패는 빈 목록."""
    p = os.path.join("public", "api", "signals.json")
    try:
        with open(p, encoding="utf-8") as fp:
            return json.load(fp).get("signals") or []
    except Exception:
        pass
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            import subprocess
            subprocess.run(["git", "fetch", "-q", "origin", "state"],
                           timeout=30, check=True, capture_output=True)
            out = subprocess.run(
                ["git", "show", "FETCH_HEAD:feed/signals.latest.json"],
                timeout=10, check=True, capture_output=True)
            return json.loads(out.stdout).get("signals") or []
        except Exception:
            pass
    try:
        repo = os.environ.get("GITHUB_REPOSITORY",
                              "easyseop/Stock-chart-analyze")
        with urllib.request.urlopen(FEED_RAW.format(repo=repo),
                                    timeout=15) as r:
            return json.loads(r.read()).get("signals") or []
    except Exception:
        return []


def _targets() -> list[str]:
    """이번 사이클 심볼 목록 — 보유·대기(무조건) + 신호 피드 후보(잔여 슬롯)."""
    from scanner import autopaper as ap
    st = ap._load()                        # 읽기 전용(락·저장 없음)
    codes: list[str] = []
    for c in list(st.get("pos", {})) + list(st.get("pending", {})):
        if c not in codes:
            codes.append(c)
    for s in _feed_signals():
        c = s.get("code")
        if c and c not in codes:
            codes.append(c)
        if len(codes) >= FAST_MAX:
            break
    return codes


def run(out_dir: str = "public") -> dict | None:
    from scanner import cache, data, universe, gates
    from scanner import screener as scr
    from scanner.analyze import analyze

    codes = _targets()
    if not codes:
        print("[fast] 대상 없음(보유·대기·후보 0) — 종료")
        return None
    umap = {s["code"]: s for s in universe.load() if s.get("code")}
    bench = {}
    for ccy in ("USD", "KRW"):
        try:
            bench[ccy] = data.fetch_benchmark(ccy)
        except Exception:
            bench[ccy] = None              # 벤치 실패 = RS/시장 0점 처리(분석과 동일)

    results, fetched, failed = [], 0, []
    for code in codes:
        meta = umap.get(code) or {
            "code": code, "name": code,
            "ccy": "KRW" if (len(code) == 6 and code[:5].isdigit()) else "USD"}
        try:
            cache.update(code)             # 증분 수집(실패 시 기존 캐시 유지)
            fetched += 1
        except Exception:
            failed.append(code)
        try:
            frames = cache.frames(code, refresh=False)
            results.append(analyze(frames, meta,
                                   bench=bench.get(meta.get("ccy", "USD"))))
        except Exception as e:
            print(f"[fast] {code} 분석 실패(건너뜀): {type(e).__name__}: {e}",
                  file=sys.stderr)
    if not results:
        print("[fast] 분석 결과 0 — 종료(매매 없음)")
        return None

    # 벌크와 동일 경로: 큐레이션 → 불변식 감사 → 자동매매 1스텝.
    #   audit 실패는 예외 전파 → 잡 실패(텔레그램) — 나쁜 신호로 매매하지 않는다.
    picks = scr._paper_picks(results)
    gates.audit(results, picks)
    from scanner import autopaper
    auto = autopaper.update(results, picks, out_dir)

    # 신호 피드 갱신(부분·신선) — advisor·다음 fast·파수꾼의 소비용.
    #   전 종목 피드는 벌크가 쓴다. fast는 '자기가 본 종목'만 신선한 가격으로.
    try:
        payload = json.loads(scr._signals_json(results))
        payload["lane"] = "fast"
        os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
        with open(os.path.join(out_dir, "api", "signals.json"), "w",
                  encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
    except Exception as e:
        print(f"[fast] 신호 피드 기록 실패(무시): {e}", file=sys.stderr)

    print(f"[fast] 완료 — 심볼 {len(codes)} (수집 {fetched}"
          f"{' · 실패 ' + ','.join(failed[:5]) if failed else ''}) · "
          f"보유 {len(auto.get('positions', []))} · "
          f"대기 {len(auto.get('pending', []))}"
          + (" · 매매생략(락)" if auto.get("lock_skipped") else ""))
    return auto
