"""지표 계산 — 외부 라이브러리 없이 pandas로 직접 구현.

각 분석 함수는 dict 를 반환한다:
  {"score": -2..+2, "reason": "근거 문구", "terms": [용어...], ...상세}
점수가 없는 보조 지표(ADX 국면, ATR 손절)는 별도 형태를 반환.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


# ════════════════════════════════════════════════════════════
# 기본 지표 (Wilder 평활)
# ════════════════════════════════════════════════════════════
def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder 평활(= EMA, alpha=1/period)."""
    return series.ewm(alpha=1 / period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = config.ATR_PERIOD) -> pd.Series:
    return _wilder(true_range(df), period)


def rsi(close: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def adx(df: pd.DataFrame, period: int = config.ADX_PERIOD):
    """표준 ADX(14). (adx, +DI, -DI) 시리즈 반환."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    atr_ = _wilder(true_range(df), period)
    plus_di = 100 * _wilder(plus_dm, period) / atr_.replace(0, np.nan)
    minus_di = 100 * _wilder(minus_dm, period) / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx.fillna(0), period), plus_di, minus_di


# ════════════════════════════════════════════════════════════
# 1-1. 시장 국면 (ADX) — 점수가 아니라 게이트
# ════════════════════════════════════════════════════════════
def regime(df: pd.DataFrame) -> dict:
    adx_s, plus_di, minus_di = adx(df)
    val = float(adx_s.iloc[-1])
    if val >= config.ADX_TREND:
        flag = "추세장"
    elif val < config.ADX_RANGE:
        flag = "횡보장"
    else:
        flag = "전환"
    direction = "상승" if plus_di.iloc[-1] >= minus_di.iloc[-1] else "하락"
    return {"flag": flag, "adx": val, "direction": direction,
            "reason": f"{flag} (ADX {val:.0f})", "terms": ["ADX"]}


# ════════════════════════════════════════════════════════════
# 1-2. 추세 — MA 배열 + 골든/데드크로스 + 다중 시간프레임
# ════════════════════════════════════════════════════════════
def _ma_dict(close: pd.Series, periods) -> dict[int, float]:
    return {p: float(close.rolling(p).mean().iloc[-1]) for p in periods}


def _tf_direction(df: pd.DataFrame, ma_period: int) -> int:
    """한 시간프레임의 추세 방향: 종가>MA & MA 상승 → +1, 반대 → -1, 그 외 0."""
    ma = df["Close"].rolling(ma_period).mean()
    if len(ma.dropna()) < 2:
        return 0
    rising = ma.iloc[-1] > ma.iloc[-2]
    above = df["Close"].iloc[-1] > ma.iloc[-1]
    if above and rising:
        return 1
    if (not above) and (not rising):
        return -1
    return 0


def trend(frames: dict[str, pd.DataFrame]) -> dict:
    d = frames["D"]
    close = d["Close"]
    mas = _ma_dict(close, config.MA_PERIODS["D"])
    p = config.MA_PERIODS["D"]
    vals = [mas[x] for x in p]

    aligned_up = all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))
    aligned_dn = all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))

    # 골든/데드크로스 (20선이 60선 돌파) — 최근 CROSS_LOOKBACK봉 내 '실제 교차'만
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    cross = ""
    if ma60.notna().sum() > config.CROSS_LOOKBACK:  # 가드는 더 긴 ma60 기준
        diff = ma20 - ma60
        win = diff.iloc[-(config.CROSS_LOOKBACK + 1):]
        prev = win.shift(1)
        crossed_up = ((prev <= 0) & (win > 0)).any()   # 음→양 전환이 구간 내 발생
        crossed_dn = ((prev >= 0) & (win < 0)).any()
        if crossed_up:
            cross = "골든크로스"
        elif crossed_dn:
            cross = "데드크로스"

    # 다중 시간프레임 정렬
    dirs = {tf: _tf_direction(frames[tf], config.MTF_TREND_MA[tf])
            for tf in ("M", "W", "D")}
    mtf_sum = sum(dirs.values())

    # 점수 합성: 배열(±2 기반) + 다중TF 보정 → -2..+2 클램프
    base = 2 if aligned_up else (-2 if aligned_dn else 0)
    if base == 0:  # 배열이 애매하면 다중TF 정렬로 방향 결정
        base = 2 if mtf_sum == 3 else (-2 if mtf_sum == -3 else 0)
    bonus = 0
    if cross == "골든크로스":
        bonus += 1
    elif cross == "데드크로스":
        bonus -= 1
    score = int(np.clip(base + bonus, -2, 2))

    arr = "정배열" if aligned_up else ("역배열" if aligned_dn else "혼조")
    mtf_txt = "·".join(
        f"{ {'M':'월','W':'주','D':'일'}[tf] }{'↑' if dirs[tf]>0 else ('↓' if dirs[tf]<0 else '→')}"
        for tf in ("M", "W", "D"))
    reason = f"{arr} / 다중TF {mtf_txt}" + (f" / {cross}" if cross else "")
    terms = ["다중시간프레임"]
    if arr in ("정배열", "역배열"):
        terms.append(arr)
    if cross:
        terms.append(cross)
    return {"score": score, "reason": reason, "terms": terms,
            "ma": mas, "arrangement": arr, "mtf": dirs, "cross": cross}


# ════════════════════════════════════════════════════════════
# 1-3. 모멘텀 — RSI (역추세 신호)
# ════════════════════════════════════════════════════════════
def momentum_rsi(df: pd.DataFrame) -> dict:
    val = float(rsi(df["Close"]).iloc[-1])
    if val <= 30:
        score, note = 2, "과매도(공포) → 반등 기대"
    elif val >= 70:
        score, note = -2, "과매수(탐욕) → 단기조정 경계"
    elif val <= 40:
        score, note = 1, "약세권"
    elif val >= 60:
        score, note = -1, "강세권(과열 주의)"
    else:
        score, note = 0, "중립"
    return {"score": score, "reason": f"RSI {val:.0f} ({note})",
            "terms": ["RSI"], "rsi": val}


# ════════════════════════════════════════════════════════════
# 1-3b. 상대강도(RS) · 신고가 · 시장방향 — 모멘텀/추세추종 계열
# ════════════════════════════════════════════════════════════
def relative_strength(df: pd.DataFrame, bench, lookback: int = config.RS_LOOKBACK) -> dict:
    """지수 대비 상대강도. 같은 기간 종목수익률 − 지수수익률(아웃퍼폼)로 ±2 점수.

    RS 라인(종목/지수 비율)도 반환해 차트에 쓴다. bench 없으면 0점(데이터 없음).
    """
    if bench is None or "Close" not in bench:
        return {"score": 0, "reason": "상대강도: 지수 데이터 없음", "terms": ["상대강도"],
                "rs_line": None, "rel": None}
    # 공통 날짜로 정렬
    b = bench["Close"].reindex(df.index).ffill()
    rs_line = (df["Close"] / b).dropna()
    if len(rs_line) <= lookback:
        return {"score": 0, "reason": "상대강도: 데이터 부족", "terms": ["상대강도"],
                "rs_line": rs_line, "rel": None}
    rel = float(rs_line.iloc[-1] / rs_line.iloc[-lookback] - 1.0)   # 기간 상대수익차
    if rel >= config.RS_STRONG:
        score, note = 2, "지수 대비 강한 아웃퍼폼"
    elif rel >= config.RS_WEAK:
        score, note = 1, "지수 대비 우위"
    elif rel <= -config.RS_STRONG:
        score, note = -2, "지수 대비 심한 언더퍼폼"
    elif rel <= -config.RS_WEAK:
        score, note = -1, "지수 대비 열위"
    else:
        score, note = 0, "지수와 비슷"
    return {"score": score, "rel": rel, "rs_line": rs_line,
            "reason": f"상대강도 {rel*100:+.1f}%/{lookback}일 ({note})",
            "terms": ["상대강도"]}


def new_high(df: pd.DataFrame, lookback: int = config.NEWHIGH_LOOKBACK) -> dict:
    """52주(기본) 신고가 근접도. 고가 갱신권일수록 강한 모멘텀(+2)."""
    seg = df.iloc[-lookback:]
    hi = float(seg["High"].max())
    price = float(df["Close"].iloc[-1])
    pct = (price - hi) / hi if hi > 0 else 0.0     # 보통 ≤0 (고가 대비)
    if price >= hi * (1 - config.NEWHIGH_NEAR):
        score, note = 2, "52주 신고가권(강한 모멘텀)"
    elif pct >= -config.NEWHIGH_MID:
        score, note = 1, "고가권"
    elif pct <= -0.50:
        score, note = -2, "고가 대비 −50%↓ 심한 침체"
    elif pct <= -config.NEWHIGH_FAR:
        score, note = -1, "고가 대비 −25%↓ 침체"
    else:
        score, note = 0, "중간권"
    return {"score": score, "high52": hi, "pct_from_high": pct * 100,
            "reason": f"52주고가 대비 {pct*100:+.1f}% ({note})",
            "terms": ["신고가"]}


def market_trend(bench, ma_period: int = config.MARKET_MA) -> dict:
    """시장(지수) 방향. 지수>MA & MA상승 → +2 등. bench 없으면 0점."""
    if bench is None or "Close" not in bench or len(bench) < ma_period + 2:
        return {"score": 0, "reason": "시장방향: 지수 데이터 없음", "terms": ["시장방향"],
                "direction": "?"}
    close = bench["Close"]
    ma = close.rolling(ma_period).mean()
    above = close.iloc[-1] > ma.iloc[-1]
    rising = ma.iloc[-1] > ma.iloc[-2]
    if above and rising:
        score, note, d = 2, "상승장(지수>MA·MA상승)", "상승"
    elif above:
        score, note, d = 1, "지수 MA 위", "상승"
    elif (not above) and (not rising):
        score, note, d = -2, "하락장(지수<MA·MA하락)", "하락"
    else:
        score, note, d = -1, "지수 MA 아래", "하락"
    return {"score": score, "direction": d,
            "reason": f"시장 {note}", "terms": ["시장방향"]}


# ════════════════════════════════════════════════════════════
# 1-4. 지지/저항 + 매물대(POC) + 박스권/방어선
# ════════════════════════════════════════════════════════════
def poc(df: pd.DataFrame, bins: int = config.POC_BINS,
        lookback: int = config.POC_LOOKBACK) -> float:
    """매물대 POC: 최근 구간 종가를 가격대로 나눠 거래량 합이 최대인 구간 중심."""
    seg = df.iloc[-lookback:]
    lo, hi = float(seg["Low"].min()), float(seg["High"].max())
    if hi <= lo:
        return float(seg["Close"].iloc[-1])
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    # 각 봉의 거래량을 그 봉이 오르내린 [Low, High] 범위의 구간들에 고르게 분산
    # (종가 한 점에 몰아넣지 않음 → 매물대 정의에 충실)
    for low, high, v in zip(seg["Low"].values, seg["High"].values,
                            seg["Volume"].values):
        b0 = int(np.clip(np.digitize(low, edges) - 1, 0, bins - 1))
        b1 = int(np.clip(np.digitize(high, edges) - 1, 0, bins - 1))
        span = b1 - b0 + 1
        vol[b0:b1 + 1] += v / span
    b = int(vol.argmax())
    return float((edges[b] + edges[b + 1]) / 2)


def detect_box(df: pd.DataFrame) -> dict:
    """현재 박스권 탐지. 후보 구간 중 (좁고 + 경계 터치 많은) 구간 선택.

    경계는 '직전까지 형성된' 박스로 산출(현재 봉 제외) → 현 시점의 이탈을 잡는다.
    """
    k = config.BOX_EXCLUDE_LAST
    usable = df.iloc[:-k] if k > 0 else df   # 현재 봉(들) 제외 후
    best = None
    for n in config.BOX_WINDOWS:
        seg = usable.iloc[-n:]               # 남은 데이터의 마지막 n봉
        if len(seg) < 2:                     # 짧은 이력(신규 상장 등) 가드
            continue
        lo, hi = float(seg["Low"].min()), float(seg["High"].max())
        rng = (hi - lo) / lo if lo > 0 else 1.0
        # 경계 터치 수 (±NEAR_PCT 이내 종가 봉)
        touch_lo = (seg["Close"] <= lo * (1 + config.NEAR_PCT)).sum()
        touch_hi = (seg["Close"] >= hi * (1 - config.NEAR_PCT)).sum()
        touches = int(touch_lo + touch_hi)
        cand = {"n": n, "low": lo, "high": hi, "range": rng, "touches": touches}
        # 우선순위: 좁은 박스 우선 → 터치 밀도(터치/봉수, 창 길이 보정) 높은 쪽
        density = touches / len(seg)
        score = (1 if rng < config.BOX_RANGE_MAX else 0, density)
        if best is None or score > best[0]:
            best = (score, cand)
    if best is None:                         # 모든 창이 너무 짧으면 전체로 폴백
        seg = usable if len(usable) >= 2 else df
        lo, hi = float(seg["Low"].min()), float(seg["High"].max())
        return {"n": len(seg), "low": lo, "high": hi,
                "range": (hi - lo) / lo if lo > 0 else 1.0, "touches": 0}
    return best[1]


def support_resistance(df: pd.DataFrame, ma: dict[int, float]) -> dict:
    """박스권·방어선 중심의 지지/저항 분석. position 플래그를 함께 반환
    (거래대금 모듈이 재사용)."""
    price = float(df["Close"].iloc[-1])
    box = detect_box(df)
    box_low, box_high = box["low"], box["high"]
    poc_price = poc(df)

    # 방어선 신뢰도: 박스 하단 ±2% 안에 POC/MA60/MA120/라운드넘버가 겹치는 수
    confl = []
    for label, val in [("POC", poc_price), ("MA60", ma.get(60)), ("MA120", ma.get(120))]:
        if val and abs(val - box_low) / box_low <= config.NEAR_PCT:
            confl.append(label)
    if _is_round_number(box_low):
        confl.append("라운드넘버")
    defense_strength = "강함" if len(confl) >= 2 else ("보통" if confl else "약함")

    near = config.NEAR_PCT
    broke_down = price < box_low * (1 - near)         # 박스 하단 이탈
    broke_up = price > box_high * (1 + near)           # 박스 상단 돌파
    # 근접은 '선 안쪽(미돌파/미이탈)'에서만 — 이미 뚫은 가격을 임박으로 오판하지 않게
    pos_low = box_low * (1 - near) <= price <= box_low * (1 + near)
    pos_high = box_high * (1 - near) <= price <= box_high

    rebound = df["Close"].iloc[-1] > df["Open"].iloc[-1]  # 당일 양봉(반등)

    if broke_down:
        score, pos, note = -2, "박스이탈", "방어선 붕괴 → 추세 훼손"
    elif pos_low and rebound:
        score, pos, note = 2, "바닥권", "방어선 지지 반등 (손절선이 바로 아래)"
    elif broke_up:
        score, pos, note = 2, "저항돌파", "박스 상단 돌파 → 상단이 지지로 전환"
    elif pos_high:
        score, pos, note = -1, "고점권", "박스 상단(매도벽) 임박"
    else:
        score, pos, note = 0, "중단", "박스 중단, 방향 미정"

    # 상단 대비 현재가 위치(+면 상단 위로 돌파, -면 아래)
    pct_vs_high = (price - box_high) / box_high * 100
    return {
        "score": score, "reason": note, "terms": ["박스권", "방어선", "POC"],
        "position": pos, "rebound_candle": rebound,
        "box_low": box_low, "box_high": box_high, "poc": poc_price,
        "defense": box_low, "defense_strength": defense_strength,
        "confluence": confl, "price": price, "pct_vs_high": pct_vs_high,
    }


def _is_round_number(x: float) -> bool:
    """라운드 넘버(심리적 가격대) 근접 여부.

    자릿수 단위(10^n)와 그 절반(5×10^(n-1)) 배수에 ±0.5% 이내일 때만 인정.
    (예: 80,000·75,000은 O, 76,000·82,000은 X) — 과도한 라운드 판정 방지.
    """
    if x <= 0:
        return False
    import math
    mag = 10 ** math.floor(math.log10(x))     # 선두 자릿수 크기
    for step in (mag, mag / 2):
        nearest = round(x / step) * step
        if abs(x - nearest) / x <= 0.005:
            return True
    return False


# ════════════════════════════════════════════════════════════
# 1-5. 거래대금 급증 (위치 × 캔들)
# ════════════════════════════════════════════════════════════
def volume_surge(df: pd.DataFrame, sr: dict) -> dict:
    turnover = df["Close"] * df["Volume"]
    avg = turnover.rolling(config.TURNOVER_AVG_WINDOW).mean().iloc[-1]
    today = float(turnover.iloc[-1])
    mult = today / avg if avg and avg > 0 else 0.0
    surged = mult >= config.TURNOVER_SURGE_MULT

    o, c = df["Open"].iloc[-1], df["Close"].iloc[-1]
    body = abs(c - o) / o if o else 0
    bull = c > o
    big_bear = (not bull) and body >= 0.03   # 장대음봉

    pos = sr["position"]
    # 설계 §1-5 위치×캔들 매트릭스
    if surged and pos in ("저항돌파", "박스이탈") and bull:
        score, note = 2, "거래대금 급증 + 양봉 → 진짜 돌파"
    elif surged and pos == "바닥권" and bull:
        score, note = 1, "바닥권 거래폭발 양봉 → 셀링클라이맥스(항복)"
    elif surged and pos == "고점권" and big_bear:
        score, note = -2, "고점권 장대음봉 폭발 → 물량 출회"
    elif (not surged) and pos in ("저항돌파", "박스이탈"):
        score, note = 0, "거래 동반 없는 돌파 → 가짜 돌파 의심"
    else:
        score, note = 0, "특이 거래 없음"

    return {"score": score, "reason": f"{note} (평균 대비 {mult:.1f}배)",
            "terms": ["거래대금"] + (["셀링클라이맥스"] if "클라이맥스" in note else []),
            "mult": mult, "surged": surged}


# ════════════════════════════════════════════════════════════
# 1-6. ATR 손절 / 비중 (점수 X, 리스크 산출)
# ════════════════════════════════════════════════════════════
def risk_levels(df: pd.DataFrame, entry: float, defense: float, ccy: str,
                prefer_atr: bool = False) -> dict:
    a = float(atr(df).iloc[-1])
    atr_stop = entry - config.ATR_STOP_MULT * a
    defense_stop = defense * (1 - config.DEFENSE_STOP_BUFFER)  # 방어선 약간 아래

    # 손절 선택
    if prefer_atr:
        # 진입가가 '지지선 위'에 잡힌 눌림 매수면, 손절은 그 지지 아래 ATR 여유까지
        # (방어선=진입가라 '방어선 약간 아래'로 잡으면 손절폭이 0에 수렴 → 비정상).
        stop = atr_stop
    elif config.STOP_MODE == "atr":
        stop = atr_stop
    elif config.STOP_MODE == "defense":
        stop = defense_stop
    else:  # nearest: 진입가에 더 가까운(손실폭 작은) 쪽 — 단 진입 아래만 유효
        cands = [s for s in (atr_stop, defense_stop) if s < entry]
        stop = max(cands) if cands else atr_stop
    stop = min(stop, entry * 0.99)  # 안전장치: 손절은 진입보다 최소 1% 아래

    risk_per_share = entry - stop
    target = entry + config.RR_TARGET * risk_per_share
    rr = config.RR_TARGET

    acct = config.ACCOUNT_SIZE.get(ccy, 10000.0)
    budget = acct * config.RISK_PER_TRADE
    shares = int(budget // risk_per_share) if risk_per_share > 0 else 0
    # 1% 리스크 예산으로 1주도 못 사면(손절폭 > 예산) 경고 플래그
    underfunded = risk_per_share > budget

    return {"atr": a, "atr_stop": atr_stop, "defense_stop": defense_stop,
            "stop": stop, "target": target,
            "rr": rr, "risk_per_share": risk_per_share,
            "shares": shares, "underfunded": underfunded,
            "account": acct, "ccy": ccy,
            "terms": ["ATR손절", "손익비"]}
