"""어닝(실적발표) 근접 경고 — 발표일 ±N일 진입은 갭 리스크가 커서 회피 권장.

데이터: yfinance(차트와 별개 소스). 발표일은 자주 안 바뀌므로 디스크에 캐시하고
하루 단위로만 갱신한다(스크리너 빌드는 캐시만 읽어 네트워크 0). yfinance가 없거나
수집 실패하면 전부 None → 경고를 안 띄울 뿐 파이프라인은 절대 안 깨진다.
"""
from __future__ import annotations

import json
import os

import pandas as pd

CACHE_PATH = "earnings_cache.json"
_TTL_HOURS = 20          # 캐시 신선도(하루 1회 갱신이면 충분)
NEAR_DAYS = 3            # 발표 ±3일이면 '임박' 경고


def _load() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def _save(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as fp:
            json.dump(cache, fp)
    except Exception:
        pass


def next_date(code: str) -> str | None:
    """캐시된 다음 실적발표일('YYYY-MM-DD') 또는 None(미수집/없음). 네트워크 0."""
    rec = _load().get(code)
    return rec.get("date") if rec else None


def days_until(code: str, today: pd.Timestamp | None = None) -> int | None:
    """다음 실적발표까지 남은 일수(지났으면 음수). 정보 없으면 None."""
    d = next_date(code)
    if not d:
        return None
    today = (today or pd.Timestamp.now()).normalize()
    try:
        return int((pd.Timestamp(d).normalize() - today).days)
    except Exception:
        return None


def is_near(code: str, today: pd.Timestamp | None = None) -> bool:
    """발표 ±NEAR_DAYS 이내인가(앞으로 다가오는 경우만)."""
    n = days_until(code, today)
    return n is not None and 0 <= n <= NEAR_DAYS


def _fetch_one(code: str):
    """yfinance로 다음 실적발표일 1건 조회. 실패/미설치 시 None(예외 안 던짐)."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(code)
        df = t.get_earnings_dates(limit=8)
        if df is None or len(df) == 0:
            return None
        future = df.index[df.index >= pd.Timestamp.now(tz=df.index.tz)]
        if len(future) == 0:
            return None
        return pd.Timestamp(min(future)).strftime("%Y-%m-%d")
    except Exception:
        return None


def refresh(codes: list[str], force: bool = False) -> int:
    """주어진 종목들의 발표일 캐시를 갱신(best-effort). 갱신 건수 반환.

    미국 티커만 의미 있음(yfinance). TTL 안 지난 건 건너뛴다.
    """
    cache = _load()
    now = pd.Timestamp.now()
    updated = 0
    for code in codes:
        if len(code) == 6 and code[:5].isdigit():
            continue                       # 한국 코드는 yfinance 어닝 신뢰도 낮음 → 생략
        rec = cache.get(code)
        if rec and not force:
            try:
                age = (now - pd.Timestamp(rec.get("fetched"))).total_seconds() / 3600
                if age < _TTL_HOURS:
                    continue
            except Exception:
                pass
        d = _fetch_one(code)
        cache[code] = {"date": d, "fetched": now.strftime("%Y-%m-%d %H:%M:%S")}
        if d:
            updated += 1
    _save(cache)
    return updated
