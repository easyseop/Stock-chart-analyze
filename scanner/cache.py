"""로컬 증분 캐시 — 전체 이력은 1회 백필, 이후 '신규 봉만' 추가.

설계(사용자 안):
  - 백필: 종목당 전체 일봉을 1회 수집(레이트리밋 회피 위해 하루 N개씩 나눠도 됨).
  - 증분: 다음부터는 캐시의 마지막 날짜 이후만 받아 append → 데이터·시간 최소.
저장: data_cache/{code}.csv.gz  (gitignored, 종목당 ~30KB)
용량 걱정 없음 / 깃에는 올리지 않음(언제든 폴더 삭제하면 0).
"""
from __future__ import annotations

import os

import pandas as pd

from . import data as datamod

CACHE_DIR = "data_cache"
_OVERLAP_DAYS = 7   # 증분 시 마지막 며칠 겹쳐 받아 수정주가·정정 반영


def _path(code: str) -> str:
    return os.path.join(CACHE_DIR, f"{code}.csv.gz")


def is_cached(code: str) -> bool:
    return os.path.exists(_path(code))


def cached_codes() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return sorted(f[:-7] for f in os.listdir(CACHE_DIR) if f.endswith(".csv.gz"))


def load(code: str) -> pd.DataFrame | None:
    """캐시된 일봉 로드(없으면 None). gzip-CSV 자동 해제."""
    p = _path(code)
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return df


def save(code: str, df: pd.DataFrame) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(_path(code))   # .gz 확장자 → pandas가 자동 압축


def update(code: str) -> pd.DataFrame:
    """캐시 있으면 마지막 날짜 이후만 받아 병합, 없으면 전체 백필. 전체 일봉 반환."""
    cached = load(code)
    if cached is None or len(cached) == 0:
        df = datamod.fetch_daily(code)          # 1회 전체 백필
        save(code, df)
        return df
    last = cached.index[-1]
    start = (last - pd.Timedelta(days=_OVERLAP_DAYS)).strftime("%Y-%m-%d")
    try:
        fresh = datamod.fetch_daily(code, start=start)   # 신규 구간만
    except Exception:
        return cached                            # 수집 실패 시 기존 캐시 유지
    merged = pd.concat([cached, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    save(code, merged)
    return merged


def frames(code: str, refresh: bool = True) -> dict:
    """일/주/월 프레임을 캐시 기반으로 구성.

    refresh=True면 증분 갱신 후 사용, False면 캐시만 로드(오프라인·요청 0).
    """
    if refresh:
        d = update(code)
    else:
        d = load(code)
        if d is None:
            d = update(code)                     # 캐시 없으면 부득이 수집
    return datamod.frames_from_daily(d)


def total_size_mb() -> float:
    if not os.path.isdir(CACHE_DIR):
        return 0.0
    n = sum(os.path.getsize(os.path.join(CACHE_DIR, f))
            for f in os.listdir(CACHE_DIR) if f.endswith(".csv.gz"))
    return n / (1024 * 1024)
