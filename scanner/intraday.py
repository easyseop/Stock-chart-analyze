"""시간봉(60분) 인트라데이 데이터 — 상세 차트의 '시간봉' 탭용.

일봉(fdr)과 별개로 yfinance에서 60분봉을 받아 data_cache_h/ 에 캐시한다.
정적 사이트라 '실시간'은 아니고 인트라데이 워크플로 실행 주기(몇 시간)로 갱신된다.
yfinance 미설치/수집 실패 시 전부 None → '시간봉' 탭이 안 뜰 뿐, 일봉 흐름엔 영향 없음.

분봉(1·5분)은 의도적으로 미지원: 배포 주기상 항상 몇 시간 stale라 오해를 부르고,
점수·전환 로직이 일봉 스윙 기준으로 보정돼 있어 분봉엔 맞지 않는다(증권사 앱 권장).
"""
from __future__ import annotations

import os

import pandas as pd

CACHE_DIR = "data_cache_h"
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _path(code: str) -> str:
    return os.path.join(CACHE_DIR, f"{code}.csv.gz")


def is_cached(code: str) -> bool:
    return os.path.exists(_path(code))


def cached_codes() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return sorted(f[:-7] for f in os.listdir(CACHE_DIR) if f.endswith(".csv.gz"))


def load(code: str) -> pd.DataFrame | None:
    p = _path(code)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index)      # 인덱스 datetime 보장
        return df
    except Exception:
        return None


def _fetch(code: str) -> pd.DataFrame | None:
    """yfinance 60분봉 수집(약 6개월, yfinance 유효 period). 실패/미설치 시 None."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        df = yf.download(code, period="6mo", interval="60m",
                         progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return None
        if isinstance(df.columns, pd.MultiIndex):     # 단일 티커도 멀티컬럼일 때
            df.columns = df.columns.get_level_values(0)
        df = df[[c for c in OHLCV if c in df.columns]].dropna(subset=["Close"])
        df = df[df["Close"] > 0]
        df["Volume"] = df["Volume"].fillna(0)
        return df if len(df) else None
    except Exception:
        return None


def update(code: str) -> pd.DataFrame | None:
    """시간봉 캐시 갱신(best-effort). 성공 시 DataFrame, 실패 시 기존 캐시/None."""
    fresh = _fetch(code)
    if fresh is None or len(fresh) == 0:
        return load(code)
    os.makedirs(CACHE_DIR, exist_ok=True)
    fresh.to_csv(_path(code))
    return fresh


def frame(code: str) -> pd.DataFrame | None:
    """캐시된 시간봉(네트워크 0). 없으면 None."""
    return load(code)
