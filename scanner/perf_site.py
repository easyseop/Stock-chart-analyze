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


DAYS_RETENTION_PER_MARKET = 400      # 서버(alpha.DAYS_RETENTION)와 정합


def _merge_days(old_rows: list, new_rows: list) -> list:
    """일별 행 누적 병합 — 발행은 최근 30일만 싣지만 사이트는 계속 쌓는다.

    같은 (날짜, 시장) 행은 최신 발행이 이긴다(미확정→확정 소급 반영).
    시장별 400행 초과분은 오래된 것부터 버린다(서버 보존 창과 동일).
    """
    by_key: dict[tuple, dict] = {}
    for row in list(old_rows or []) + list(new_rows or []):
        if not isinstance(row, dict):
            continue
        key = (str(row.get("d") or ""), str(row.get("mkt") or ""))
        if key[0] and key[1]:
            by_key[key] = row                     # 나중(=신규 발행) 행이 덮음
    merged = sorted(by_key.values(),
                    key=lambda r: (str(r.get("d")), str(r.get("mkt"))))
    per_market: dict[str, list] = {}
    for row in merged:
        per_market.setdefault(str(row.get("mkt")), []).append(row)
    keep = []
    for rows in per_market.values():
        keep.extend(rows[-DAYS_RETENTION_PER_MARKET:])
    return sorted(keep, key=lambda r: (str(r.get("d")), str(r.get("mkt"))))


def build(out_dir: str = "public") -> bool:
    """api/performance.json을 굽는다. 소스가 전혀 없으면 False(파일 미생성)."""
    cached = _load_cache()
    fresh = fetch_latest()
    if fresh is not None:
        payload = dict(fresh["payload"])
        payload["days"] = _merge_days(
            (cached or {}).get("payload", {}).get("days"), payload.get("days"))
        entry = {"payload": payload, "published_at": fresh["published_at"]}
        _save_cache(entry)
        source = "ntfy"
    else:
        entry = cached
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


TRADES_CACHE_PATH = os.path.join("data_cache", "trade_stats.json")


def _fetch_topic_latest(topic: str, title: str) -> dict | None:
    """ntfy 토픽의 최신 지정 title 메시지 payload. 실패·부재는 None."""
    try:
        with urllib.request.urlopen(
                f"https://ntfy.sh/{topic}/json?poll=1",
                timeout=FETCH_TIMEOUT_S) as resp:
            lines = resp.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    latest = None
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (msg.get("event") == "message" and msg.get("title") == title
                and msg.get("message")):
            latest = msg
    if latest is None:
        return None
    try:
        payload = json.loads(latest["message"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("total") else None


def build_trade_stats(out_dir: str = "public") -> bool:
    """공개 승률 요약(api/trades_summary.json)을 굽는다.

    서버가 발행한 payload는 이미 금액·수량·종목이 없다. 그래도 소비 측에서
    한 번 더 화이트리스트로 걸러 실수로 민감 필드가 실려도 사이트로 나가지
    않게 한다(경계 방어). ntfy가 비면 캐시 폴백 — 사이트 사본은 만료 없음.
    """
    from bot import settings
    topic = os.environ.get("NTFY_TRADE_STATS_TOPIC",
                           getattr(settings, "TRADE_STATS_TOPIC", ""))
    payload = _fetch_topic_latest(topic, "trade-stats") if topic else None
    source = "ntfy"
    if payload is None:
        try:
            with open(TRADES_CACHE_PATH, encoding="utf-8") as fp:
                payload = json.load(fp)
            source = "cache"
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            payload = None
    if not isinstance(payload, dict) or not payload.get("total"):
        print("perf_site: 거래 성적 소스 없음 — 건너뜀")
        return False

    bucket_keys = {"closed", "decided", "wins", "losses", "win_rate",
                   "avg_return_pct", "median_return_pct",
                   "avg_win_pct", "avg_loss_pct"}

    def clean(bucket) -> dict:
        return ({k: v for k, v in bucket.items() if k in bucket_keys}
                if isinstance(bucket, dict) else {})

    safe = {
        "version": 1,
        "generated_at": payload.get("generated_at"),
        "partial": bool(payload.get("partial")),
        "source": f"actions-{source}",
        "total": clean(payload.get("total")),
        "by_sleeve": {k: clean(v) for k, v in
                      (payload.get("by_sleeve") or {}).items() if k in ("A", "B")},
        "by_month": {str(k)[:7]: clean(v) for k, v in
                     (payload.get("by_month") or {}).items()},
        "note": "확정 매도 체결 기준 · 금액·수량·종목 비공개",
    }
    try:
        os.makedirs(os.path.dirname(TRADES_CACHE_PATH) or ".", exist_ok=True)
        with open(TRADES_CACHE_PATH, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
    except OSError:
        pass
    api_dir = os.path.join(out_dir, "api")
    os.makedirs(api_dir, exist_ok=True)
    with open(os.path.join(api_dir, "trades_summary.json"), "w",
              encoding="utf-8") as fp:
        json.dump(safe, fp, ensure_ascii=False, separators=(",", ":"))
    total = safe["total"]
    print(f"perf_site: api/trades_summary.json 생성 (source={source}, "
          f"{total.get('wins')}승 {total.get('losses')}패 · "
          f"승률 {total.get('win_rate')})")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="공개 사이트 성과 스냅샷 굽기")
    ap.add_argument("--out", default="public", help="사이트 출력 디렉터리")
    args = ap.parse_args(argv)
    build(args.out)
    build_trade_stats(args.out)
    return 0        # best-effort — 실패해도 빌드를 깨지 않는다


if __name__ == "__main__":
    raise SystemExit(main())
