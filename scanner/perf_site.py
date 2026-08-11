"""공개 사이트용 성과 스냅샷 굽기 — 앱 '성과·지수 비교' 탭의 데이터 소스.

문제(2026-08-12 실측): 앱의 성과 탭은 `../api/performance.json`을 읽는데 이는
Oracle 개인 서버(portfolio-web) 전용 API라 GitHub Pages에는 404였다. 또 구식
perf.html은 ntfy를 직접 읽는데 보관 12시간이라 오후에 열면 그래프가 비었다.

해결: 빌드(15분 주기)가 서버 발행(ntfy `alpha-dash`, 퍼센트 전용) 최신 스냅샷을
긁어 `bot.alpha.dashboard_snapshot(st=…)`로 앱 스키마로 변환해
`public/api/performance.json`으로 굽는다. ntfy가 비어 있으면(주말·보관 만료)
data_cache에 보존해 둔 마지막 스냅샷으로 굽는다 — 사이트에서는 만료가 없다.

원칙: 페이로드는 원래부터 무시크릿(퍼센트만). 실패는 전부 무해(파일 미갱신).
실행: python -m scanner.perf_site --out public
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import urllib.request

CACHE_PATH = os.path.join("data_cache", "perf_snapshot.json")
FETCH_TIMEOUT_S = 15


def _topic() -> str:
    return os.environ.get("NTFY_ALPHA_TOPIC", "stock-alpha-c81f4e2b9d")


def fetch_latest() -> dict | None:
    """ntfy에서 최신 alpha-dash 메시지 1건 → {payload, published_at}. 실패 None."""
    url = f"https://ntfy.sh/{_topic()}/json?poll=1"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            lines = resp.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    latest = None
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (msg.get("event") == "message" and msg.get("title") == "alpha-dash"
                and msg.get("message")):
            latest = msg
    if latest is None:
        return None
    try:
        payload = json.loads(latest["message"])
        published = float(latest.get("time") or 0)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("days"):
        return None
    return {"payload": payload, "published_at": published}


def _load_cache() -> dict | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fp:
            raw = json.load(fp)
        if (isinstance(raw, dict) and isinstance(raw.get("payload"), dict)
                and raw["payload"].get("days")):
            return raw
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _save_cache(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(entry, fp, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def build(out_dir: str = "public") -> bool:
    """api/performance.json을 굽는다. 소스가 전혀 없으면 False(파일 미생성)."""
    entry = fetch_latest()
    if entry is not None:
        _save_cache(entry)
        source = "ntfy"
    else:
        entry = _load_cache()
        source = "cache"
    if entry is None:
        print("perf_site: 스냅샷 소스 없음(ntfy 비어있음·캐시 없음) — 건너뜀")
        return False

    from bot import alpha
    st = dict(entry["payload"])
    published = float(entry.get("published_at") or 0)
    kst = datetime.timezone(datetime.timedelta(hours=9))
    if published > 0:
        st.setdefault("updated_at", datetime.datetime.fromtimestamp(
            published, kst).isoformat())
    snapshot = alpha.dashboard_snapshot(st=st)
    snapshot.update({
        "environment": "mock",
        "read_only": True,
        "source": f"actions-{source}",   # 개인서버 실시간이 아닌 빌드 시점 사본
    })
    api_dir = os.path.join(out_dir, "api")
    os.makedirs(api_dir, exist_ok=True)
    out_path = os.path.join(api_dir, "performance.json")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(snapshot, fp, ensure_ascii=False, separators=(",", ":"))
    age_min = (max(0.0, (datetime.datetime.now(kst).timestamp() - published))
               / 60 if published > 0 else -1)
    print(f"perf_site: {out_path} 생성 (source={source}, "
          f"스냅샷 나이 {age_min:.0f}분, days={len(st.get('days') or [])})")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="공개 사이트 성과 스냅샷 굽기")
    ap.add_argument("--out", default="public", help="사이트 출력 디렉터리")
    args = ap.parse_args(argv)
    build(args.out)
    return 0        # best-effort — 실패해도 빌드를 깨지 않는다


if __name__ == "__main__":
    raise SystemExit(main())
