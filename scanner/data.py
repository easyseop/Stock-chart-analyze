"""데이터 수집 + 리샘플.

일봉을 FinanceDataReader로 받아 주/월봉으로 리샘플한다.
(년봉은 데이터 부족으로 점수에서 제외 — 설계 결정 v2)
"""
from __future__ import annotations

import socket
import time

import pandas as pd

import config

# 네트워크 행(hang) 방지: 모든 소켓 작업에 전역 타임아웃(대량 수집 시 멈춤 차단)
socket.setdefaulttimeout(30)

OHLCV = ["Open", "High", "Low", "Close", "Volume"]

_last_req = [0.0]   # 마지막 요청 시각(throttle용)


def _read(sym: str, start: str):
    """야후 읽기 + 안전장치: 요청 throttle + 429/네트워크 오류 지수 백오프 재시도.

    심볼 없음(빈 결과)은 재시도하지 않고 그대로 반환(낭비 방지).
    """
    import FinanceDataReader as fdr
    last_err = None
    for attempt in range(config.MAX_RETRIES + 1):
        dt = time.time() - _last_req[0]          # 요청 간 최소 간격 확보
        if dt < config.REQUEST_DELAY:
            time.sleep(config.REQUEST_DELAY - dt)
        _last_req[0] = time.time()
        try:
            return fdr.DataReader(sym, start)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            transient = ("429" in msg or "too many" in msg or "timed out" in msg
                         or "timeout" in msg or "connection" in msg
                         or "temporarily" in msg)
            if transient and attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    raise last_err


def _is_korean(code: str) -> bool:
    """KRX 종목코드 판별(6자리 숫자, 끝자리 영문 우선주 포함: 005935)."""
    c = code.strip()
    return len(c) == 6 and c[:5].isdigit()


def fetch_daily(code: str, start: str = config.FETCH_START) -> pd.DataFrame:
    """일봉 OHLCV 수집. 네트워크/심볼 오류는 호출측에서 처리하도록 예외 전파.

    한국(KRX) 종목은 야후 파이낸스 티커(`.KS`/`.KQ`)로 받는다.
    기본/KRX 소스는 실행 환경의 egress 정책(naver 차단)·KRX 안티봇(LOGOUT)으로
    막히는 경우가 있어, 야후 경유가 가장 안정적이다(미국주와 동일 소스).
    """
    if _is_korean(code):
        # KOSPI(.KS) 먼저, 없으면 KOSDAQ(.KQ) 시도. 야후 소스 강제.
        last_err = None
        for suffix in (".KS", ".KQ"):
            try:
                df = _read(f"YAHOO:{code}{suffix}", start)
            except Exception as e:  # 심볼 불일치 등은 다음 접미사로
                last_err = e
                continue
            if df is not None and len(df):
                df = df[[c for c in OHLCV if c in df.columns]].copy()
                return clean(df)
        if last_err is not None:
            raise last_err
        raise ValueError(f"데이터 없음: {code}")

    df = _read(code, start)
    if df is None or len(df) == 0:
        raise ValueError(f"데이터 없음: {code}")
    df = df[[c for c in OHLCV if c in df.columns]].copy()
    return clean(df)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """결측·거래정지(거래량 0/NaN) 정리."""
    df = df.dropna(subset=["Close"])
    df = df[df["Close"] > 0]
    df["Volume"] = df["Volume"].fillna(0)
    return df


def resample(df_daily: pd.DataFrame, rule: str,
             drop_unclosed: bool = config.DROP_UNCLOSED_BAR) -> pd.DataFrame:
    """일봉 → 주/월봉 집계 (OHLC 표준 집계, Volume 합산).

    rule: 'W'(주, 금요일 마감) / 'M'(월말).
    drop_unclosed=True면 마지막 미완성 봉을 제거한다.
    """
    code = {"W": "W-FRI", "M": "ME"}.get(rule, rule)
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    out = df_daily.resample(code).agg(agg).dropna(subset=["Close"])

    if drop_unclosed and len(out) > 1:
        # 마지막 구간의 라벨(그 주 금요일/그 달 말일)이 '오늘'보다 미래면
        # 아직 끝나지 않은 구간 → 미완성 봉이므로 제거.
        # (마지막 거래일과 비교하면 월말·금요일이 휴장일 때 완성된 봉도 잘못 버림)
        period_end = out.index[-1]
        if period_end > pd.Timestamp.now().normalize():
            out = out.iloc[:-1]
    return out


def frames_from_daily(d: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """일봉 → 일/주/월 3개 시간프레임 데이터셋. (실데이터·데모 공용)"""
    return {"D": d, "W": resample(d, "W"), "M": resample(d, "M")}


def build_frames(code: str) -> dict[str, pd.DataFrame]:
    """일/주/월 3개 시간프레임 데이터셋 생성(실데이터 수집)."""
    return frames_from_daily(fetch_daily(code))


_BENCH_CACHE: dict[str, pd.DataFrame] = {}


def fetch_benchmark(ccy: str, start: str = config.FETCH_START):
    """통화별 비교 지수(일봉) 수집 + 캐시. 실패 시 None(상대강도/시장방향 생략)."""
    sym = config.BENCHMARKS.get(ccy)
    if not sym:
        return None
    if sym in _BENCH_CACHE:
        return _BENCH_CACHE[sym]
    try:
        df = _read(f"YAHOO:{sym}", start)
        df = df[[c for c in OHLCV if c in df.columns]].copy()
        df = clean(df)
    except Exception:
        df = None
    _BENCH_CACHE[sym] = df
    return df
