"""데이터 수집 + 리샘플.

일봉을 FinanceDataReader로 받아 주/월봉으로 리샘플한다.
(년봉은 데이터 부족으로 점수에서 제외 — 설계 결정 v2)
"""
from __future__ import annotations

import pandas as pd

import config

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


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
    import FinanceDataReader as fdr  # 지연 임포트(오프라인 테스트 시 불필요)

    if _is_korean(code):
        # KOSPI(.KS) 먼저, 없으면 KOSDAQ(.KQ) 시도. 야후 소스 강제.
        last_err = None
        for suffix in (".KS", ".KQ"):
            try:
                df = fdr.DataReader(f"YAHOO:{code}{suffix}", start)
            except Exception as e:  # 심볼 불일치 등은 다음 접미사로
                last_err = e
                continue
            if df is not None and len(df):
                df = df[[c for c in OHLCV if c in df.columns]].copy()
                return clean(df)
        if last_err is not None:
            raise last_err
        raise ValueError(f"데이터 없음: {code}")

    df = fdr.DataReader(code, start)
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
