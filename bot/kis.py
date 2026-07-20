"""한국투자증권(KIS) Open API 어댑터 — Stage 0/1: 읽기 전용(주문 없음).

⚠️ 이 파일에는 **매수/매도 주문 경로가 없다**(Stage 0 안전). 토큰·TR매핑·에러분류·
   계좌/잔고/미체결/체결내역 조회만. 주문은 Stage 1.5에서 별도 모듈로 추가한다.
   (주문 생성 엔드포인트가 이 파일에 없어야 정상 —
   `tests/test_kis.py: test_no_order_path_present`가 grep 가드로 강제.)

설계 원칙(토스 어댑터와 동일 철학 + KIS 특성):
  1. 표준 라이브러리만(requests 없음).
  2. 키 미설정이면 전면 비활성(enabled()=False) → 호출부 폴백.
  3. 어떤 실패도 예외를 밖으로 던지지 않는다 — 조회 실패는 None/빈 결과.
  4. appsecret·토큰은 절대 로그·예외 메시지에 담지 않는다.

KIS 특성(토스와 다른 점 — docs/kis/KIS_API_READINESS.md 근거):
  · 토큰: POST /oauth2/tokenP (JSON body). 24h 유효, 6h 내 동일 토큰 반환,
    **발급 1분당 1회 제한** → 단일 캐시 필수. 다중 프로세스 대비 **파일 락(flock)**
    기반 호스트-단일 캐시(Codex 리뷰 R1). 실전/모의 키·URL 완전 분리.
  · TR_ID: (env × 시장 × 액션) **명시 매핑 테이블**. 접두 T→V 기계치환 금지 —
    **미국 매도만 비대칭**(실전 TTTT1006U ↔ 모의 VTTT1001U). 표에 없는 조합=RuntimeError.
  · 에러: 대부분 HTTP 200 + 본문 `rt_cd`. **레이트리밋만 HTTP 500 + EGW00201**.
    → 1차 분기는 rt_cd/msg_cd, HTTP status는 폴백(§15 에러표).

환경변수(값은 절대 코드/채팅/로그에 두지 말 것):
  KIS_ENV                 mock(기본) / live
  KIS_MOCK_APPKEY / _APPSECRET / _CANO / _ACNT_PRDT_CD   (모의)
  KIS_LIVE_APPKEY / _APPSECRET / _CANO / _ACNT_PRDT_CD   (실전)
  KIS_TOKEN_CACHE         (선택) 토큰 캐시 파일 경로
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ── 환경(모의/실전) ────────────────────────────────────────────────
ENV = os.environ.get("KIS_ENV", "mock").lower()
IS_MOCK = ENV != "live"
_ENVPFX = "MOCK" if IS_MOCK else "LIVE"
BASE_URL = os.environ.get(
    "KIS_BASE_URL",
    "https://openapivts.koreainvestment.com:29443" if IS_MOCK
    else "https://openapi.koreainvestment.com:9443")

_TOKEN_SKEW = 600            # 만료 이 초 전이면 선제 재발급
_TOKEN_FAIL_COOLDOWN = 65    # 발급 실패 시 재시도 금지(1분1회 제한 대비 여유)
_HTTP_TIMEOUT = 15

# 모듈 전역 토큰 캐시(빠른 경로) + 프로세스 내 락. 다중 프로세스는 파일 락으로 직렬화.
_TOK: dict = {"tok": None, "exp": 0.0, "fail_until": 0.0, "keyhash": None}
_LOCAL_LOCK = threading.Lock()


def _cred() -> tuple[str | None, str | None]:
    return (os.environ.get(f"KIS_{_ENVPFX}_APPKEY"),
            os.environ.get(f"KIS_{_ENVPFX}_APPSECRET"))


def account() -> dict | None:
    """CANO/ACNT_PRDT_CD(요청 파라미터용). 없으면 None."""
    cano = os.environ.get(f"KIS_{_ENVPFX}_CANO")
    if not cano:
        return None
    return {"CANO": cano,
            "ACNT_PRDT_CD": os.environ.get(f"KIS_{_ENVPFX}_ACNT_PRDT_CD", "01")}


def enabled() -> bool:
    """appkey/appsecret 둘 다 있으면 True."""
    k, s = _cred()
    return bool(k and s)


def _keyhash() -> str:
    """appkey를 시크릿 노출 없이 식별(캐시가 다른 키/환경 토큰을 쓰지 않도록)."""
    k, _ = _cred()
    return hashlib.sha256(f"{ENV}:{k}".encode()).hexdigest()[:12] if k else ""


# ── TR_ID 명시 매핑 (env × 시장 × 액션) — 접두 치환 금지 ──────────────
#   ★ 미국 매도만 비대칭: 실전 TTTT1006U ↔ 모의 VTTT1001U(숫자까지 다름).
#   조회 TR은 T→V 접두 규칙이 성립하지만, 실수 방지 위해 전부 명시한다.
#   모의 실측 확정(2026-07-10): balance VTTS3012R · nccs VTTS3018R · ccnl VTTS3035R
#   모두 rt_cd=0. 주문 TR(buy/sell/rvsecncl)은 Stage 1.5 실주문으로 최종 확인.
_TR: dict[tuple[str, str, str], str] = {
    # ── 미국(해외주식) ─────────────────────────────────────────────
    # 조회(읽기 전용) — Stage 0에서 사용
    ("live", "US", "balance"): "TTTS3012R", ("mock", "US", "balance"): "VTTS3012R",
    ("live", "US", "nccs"):    "TTTS3018R", ("mock", "US", "nccs"):    "VTTS3018R",  # 모의 실측 확정

    ("live", "US", "ccnl"):    "TTTS3035R", ("mock", "US", "ccnl"):    "VTTS3035R",
    ("live", "US", "psamount"): "TTTS3007R", ("mock", "US", "psamount"): "VTTS3007R",
    # 체결기준현재잔고 — 외화예수금(현금) 조회. 모의 psamount 미지원 대체용 [대조필요]
    ("live", "US", "present"): "CTRP6504R", ("mock", "US", "present"): "VTRP6504R",
    # 주문(참조용 상수 — 이 파일은 주문을 호출하지 않는다. Stage 1.5 모듈이 사용).
    ("live", "US", "buy"):  "TTTT1002U", ("mock", "US", "buy"):  "VTTT1002U",
    ("live", "US", "sell"): "TTTT1006U", ("mock", "US", "sell"): "VTTT1001U",  # ★비대칭
    ("live", "US", "rvsecncl"): "TTTT1004U", ("mock", "US", "rvsecncl"): "VTTT1004U",

    # ── 한국(국내주식) ─────────────────────────────────────────────
    #   국내는 US-매도 같은 비대칭 없음(T→V 접두 규칙 성립) — 그래도 실수 방지
    #   위해 전부 명시. 아래 TR_ID·필드는 모의 왕복 실측 전까지 [대조필요].
    #   매수/매도는 같은 order-cash 엔드포인트, TR_ID로만 구분(해외와 다른 구조).
    ("live", "KR", "buy"):  "TTTC0802U", ("mock", "KR", "buy"):  "VTTC0802U",  # 현금매수
    ("live", "KR", "sell"): "TTTC0801U", ("mock", "KR", "sell"): "VTTC0801U",  # 현금매도
    ("live", "KR", "rvsecncl"): "TTTC0803U", ("mock", "KR", "rvsecncl"): "VTTC0803U",
    ("live", "KR", "balance"):  "TTTC8434R", ("mock", "KR", "balance"):  "VTTC8434R",
    ("live", "KR", "nccs"):     "TTTC8036R", ("mock", "KR", "nccs"):     "VTTC8036R",  # 정정취소가능주문조회
    ("live", "KR", "ccnl"):     "TTTC8001R", ("mock", "KR", "ccnl"):     "VTTC8001R",  # 일별주문체결조회
    ("live", "KR", "psbl"):     "TTTC8908R", ("mock", "KR", "psbl"):     "VTTC8908R",  # 매수가능조회
}


def market_of_symbol(symbol: str) -> str:
    """심볼 → 시장('KR'|'US'). 라우팅 단일 소스 — kis_orders/sentinel이 이걸로
    주문 경로를 가른다.

    KRX 종목코드는 6자리이고 **앞 5자리가 숫자**(6번째 자리는 보통 숫자지만
    신형우선주·제N종 등은 문자 K/L… 가능, 예 '00088K'). 미국 티커는 알파벳이라
    5자리 연속 숫자로 시작하지 않는다 → 앞5자리 숫자+길이6이면 KR.
    (ccy를 아는 호출부는 market_of_ccy로 넘기는 게 더 견고 — 이건 심볼만 있을 때 폴백.)"""
    s = str(symbol).strip().upper()
    return "KR" if (len(s) == 6 and s[:5].isdigit()) else "US"


def market_of_ccy(ccy: str) -> str:
    """통화 → 시장. KRW=KR, 그 외(USD)=US. (신호의 ccy 기준 라우팅용.)"""
    return "KR" if str(ccy).upper() == "KRW" else "US"


def tr_id(action: str, *, market: str = "US", env: str | None = None) -> str:
    """(env, market, action) → TR_ID. 표에 없으면 RuntimeError(접두 치환 금지)."""
    e = (env or ENV).lower()
    try:
        return _TR[(e, market, action)]
    except KeyError:
        raise RuntimeError(
            f"TR_ID 미정의: env={e} market={market} action={action} "
            f"— 접두 치환 금지, _TR 테이블에 명시 추가 필요")


# ── 에러 분류 (rt_cd/msg_cd 1차, HTTP status 폴백) — §15 에러표 ──────
ACT_OK = "ok"
ACT_RETRY = "retry"            # 일시 오류·EGW00201(레이트리밋) → 백오프 후 재시도
ACT_REFRESH = "refresh_auth"   # 401 → 토큰 재발급 후 1회 재시도
ACT_AUTH_FATAL = "auth_fatal"  # 403/자격증명 → 재시도 무의미, P0
ACT_UNKNOWN = "unknown"        # 주문 결과 불명 → 재주문 금지, 원장 잠금·대사
ACT_REJECT = "reject"          # 서버가 응답한 확정 실패(비즈니스/TR오류) → 재시도 무의미


def classify_error(rt_cd: str | None, msg_cd: str = "",
                   http_status: int = 200, *, is_order: bool = False) -> str:
    """KIS 응답 → 대응 액션. 1차 rt_cd/msg_cd, 2차 http_status.

    핵심(§15): ① HTTP 200이어도 rt_cd≠0이면 실패. ② 레이트리밋은 HTTP 500+EGW00201
    → 재시도(주문 경로는 호출부가 61초 대기 대신 짧은 백오프+P0, Codex R2). ③ 타임아웃·
    EGW00201 아닌 5xx는 주문이면 UNKNOWN(초과매도 방지)."""
    # 네트워크 타임아웃/무응답(HTTP status 없음 → 0)
    if http_status == 0:
        return ACT_UNKNOWN if is_order else ACT_RETRY
    # 레이트리밋 — HTTP 500 + 본문 EGW00201 (본문 먼저 봐야 함)
    if msg_cd == "EGW00201":
        return ACT_RETRY
    # TR 라우팅/설정 오류(첫 글자 오류 등) — 재시도 무의미(코드 버그·P0 대상)
    if msg_cd == "EGW00356":
        return ACT_REJECT
    if msg_cd == "EGW00202":                     # GW 라우팅 일시 오류
        return ACT_UNKNOWN if is_order else ACT_RETRY
    if http_status == 401:
        return ACT_REFRESH
    if http_status == 403:
        return ACT_AUTH_FATAL
    if rt_cd == "0" and http_status in (200, 201, 204):
        return ACT_OK
    # EGW00201 아닌 5xx = 서버 오류 → 주문 결과 불명
    if http_status >= 500:
        return ACT_UNKNOWN if is_order else ACT_RETRY
    # HTTP 200인데 rt_cd≠0 → 서버가 확정 응답한 실패(수신 O, 접수 X) → 재주문 아님
    if rt_cd not in (None, "", "0"):
        return ACT_REJECT
    # 미지 상황 — 주문이면 안전하게 UNKNOWN
    return ACT_UNKNOWN if is_order else ACT_RETRY


# ── 토큰 (파일 락 기반 호스트-단일 캐시, Codex R1) ────────────────────
def _cache_path() -> str:
    return os.environ.get(
        "KIS_TOKEN_CACHE",
        os.path.join(tempfile.gettempdir(), f".kis_token_{ENV}.json"))


def _read_cache() -> str | None:
    """파일 캐시에서 유효 토큰을 읽는다(환경·키 일치 + 만료 여유). 실패 None."""
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("env") != ENV or d.get("keyhash") != _keyhash():
        return None                              # 다른 환경/키 토큰은 안 씀
    if float(d.get("expires_at", 0)) > time.time() + _TOKEN_SKEW:
        _TOK.update(tok=d.get("access_token"), exp=float(d["expires_at"]),
                    keyhash=d.get("keyhash"))
        return _TOK["tok"]
    return None


def _write_cache(tok: str, exp: float) -> None:
    """원자적 쓰기(temp→rename). 시크릿은 안 담고 keyhash만."""
    path = _cache_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": tok, "expires_at": exp,
                       "issued_at": time.time(), "env": ENV,
                       "keyhash": _keyhash()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _flock(f, exclusive: bool):
    """파일 락(있으면). fcntl 없는 OS(윈도우)면 조용히 무락(모듈 락으로 대체)."""
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except Exception:
        pass


def _fetch_token() -> str | None:
    k, s = _cred()
    if not (k and s):
        return None
    body = json.dumps({"grant_type": "client_credentials",
                       "appkey": k, "appsecret": s}).encode("utf-8")
    req = urllib.request.Request(BASE_URL + "/oauth2/tokenP", data=body,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            d = json.load(resp)
        tok = d.get("access_token")
        exp = time.time() + float(d.get("expires_in", 86400))
        if tok:
            _TOK.update(tok=tok, exp=exp, fail_until=0.0, keyhash=_keyhash())
            _write_cache(tok, exp)
            return tok
        # 200인데 토큰 없음 = 발급 실패 → 쿨다운(본문에 시크릿 없음, msg만 노출)
        _TOK["fail_until"] = time.time() + _TOKEN_FAIL_COOLDOWN
        print(f"[kis] 토큰 발급 실패(msg_cd={d.get('msg_cd')})")
    except Exception as e:
        _TOK["fail_until"] = time.time() + _TOKEN_FAIL_COOLDOWN
        print(f"[kis] 토큰 발급 오류({type(e).__name__}) — 쿨다운 {_TOKEN_FAIL_COOLDOWN}s")
    return None


def _token(force: bool = False) -> str | None:
    """유효 토큰. 순서: 모듈캐시 → 파일캐시 → (락)재확인 → 발급.
    발급은 1분1회 제한이라 실패 쿨다운을 두고, 파일 락으로 다중프로세스 동시발급을 막는다."""
    if not enabled():
        return None
    now = time.time()
    if not force and _TOK["tok"] and _TOK.get("keyhash") == _keyhash() \
            and now < _TOK["exp"] - _TOKEN_SKEW:
        return _TOK["tok"]
    if not force and now < _TOK.get("fail_until", 0.0):
        return None
    with _LOCAL_LOCK:
        if not force:
            t = _read_cache()                    # 다른 프로세스가 이미 발급했을 수도
            if t:
                return t
        # 파일 락으로 발급 직렬화(호스트-단일). 락 파일은 캐시 경로 재사용.
        lock_path = _cache_path() + ".lock"
        try:
            lf = open(lock_path, "a+")
        except Exception:
            lf = None
        try:
            if lf:
                _flock(lf, exclusive=True)
                if not force:
                    t = _read_cache()            # 락 잡은 사이 갱신됐는지 재확인
                    if t:
                        return t
            return _fetch_token()
        finally:
            if lf:
                try:
                    lf.close()
                except OSError:
                    pass


# ── 인증 GET (읽기 전용) ─────────────────────────────────────────────
#   유량: 프로세스 전역 초당 버킷(모의 2/s·실전 20/s, order 예약슬롯) — 사전 억제.
from bot import kis_ratelimit as _rl

_LIMITER = _rl.for_env(IS_MOCK)


def _get(path: str, tr: str, params: dict) -> dict | None:
    """인증 GET. 401→토큰 1회 강제 재발급, EGW00201→짧은 백오프 1회 재시도. 실패 None."""
    tok = _token()
    if not tok:
        return None
    if not _LIMITER.acquire("data", timeout=10.0):   # 사전 억제(EGW00201 예방)
        print(f"[kis] GET {path} 유량 대기 초과 — 건너뜀")
        return None
    k, s = _cred()
    url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        headers = {"content-type": "application/json; charset=utf-8",
                   "authorization": f"Bearer {tok}",
                   "appkey": k, "appsecret": s, "tr_id": tr, "custtype": "P"}
        d, http = None, 200
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                d = json.load(resp)
        except urllib.error.HTTPError as e:
            http = e.code
            try:
                d = json.loads(e.read().decode("utf-8", "ignore"))
            except Exception:
                d = None
        except Exception as ex:
            print(f"[kis] GET {path} 실패({type(ex).__name__})")
            return None
        act = classify_error((d or {}).get("rt_cd"), (d or {}).get("msg_cd"),
                             http, is_order=False)
        if act == ACT_OK:
            return d
        if act == ACT_REFRESH and attempt == 0:
            tok = _token(force=True) or tok
            continue
        if act == ACT_RETRY and attempt < 2:
            time.sleep(1.2)                      # 조회 경로 짧은 백오프(모의 2/s)
            continue
        m1 = str((d or {}).get("msg1") or "").strip()
        print(f"[kis] GET {path} → {act} "
              f"(rt_cd={(d or {}).get('rt_cd')} msg_cd={(d or {}).get('msg_cd')}"
              f"{' · ' + m1 if m1 else ''})")           # msg1 노출(모의 미지원 판별용)
        return None
    return None


def overseas_balance(excg: str = "NASD", currency: str = "USD") -> dict | None:
    """해외잔고(읽기). output1=보유, output2=요약. 매도가능=output1.ord_psbl_qty."""
    acct = account()
    if not acct or not enabled():
        return None
    return _get("/uapi/overseas-stock/v1/trading/inquire-balance",
                tr_id("balance"),
                {**acct, "OVRS_EXCG_CD": excg, "TR_CRCY_CD": currency,
                 "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})


def open_orders(excg: str = "NASD") -> dict | None:
    """해외 미체결내역(inquire-nccs) — UNKNOWN 대사 채널 A."""
    acct = account()
    if not acct or not enabled():
        return None
    return _get("/uapi/overseas-stock/v1/trading/inquire-nccs",
                tr_id("nccs"),
                {**acct, "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
                 "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})


def present_balance() -> dict | None:
    """해외 체결기준현재잔고(inquire-present-balance) — **외화예수금(현금)** 조회.

    모의는 psamount 미지원(실측 확정)이라, 미국주 매수여력의 '하향 클램프' 입력으로
    이 응답의 USD 현금을 쓴다. TR: live CTRP6504R / mock VTRP6504R.
    [대조필요] 모의 지원·output 필드명은 서버 실측으로 확정 — 실패 시 None(fail-closed).
    """
    acct = account()
    if not acct or not enabled():
        return None
    return _get("/uapi/overseas-stock/v1/trading/inquire-present-balance",
                tr_id("present"),
                {**acct, "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                 "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"})


def _seed_native(ccy: str) -> float | None:
    """SEED(원)를 해당 통화 단위로 — feasibility 폴백값. USD는 fx로 환산.
    SEED 미설정(0)이면 None(그땐 사이징 자체가 0이라 매수 없음)."""
    try:
        from bot import envelope, settings
        seed = envelope.seed_krw()
    except Exception:
        return None
    if seed <= 0:
        return None
    if ccy == "USD":
        try:
            fx = float(settings.FX_USDKRW)
        except Exception:
            fx = 1400.0
        return seed / max(fx, 1e-9)               # 원 시드를 달러로(비바인딩 크기)
    return seed


_USD_CASH_KEYS = ("frcr_drwg_psbl_amt_1", "frcr_dncl_amt_2", "frcr_dncl_amt")
_KRW_CASH_KEYS = ("prvs_rcdl_excc_amt", "nxdy_excc_amt", "dnca_tot_amt")


def _mock_cash(balance_fn, ccy: str, cash_keys: tuple[str, ...]) -> float:
    """모의 잔고 output2에서 해당 통화 현금(양수 최대값). 조회실패/없음=0.0."""
    d = balance_fn()
    best = 0.0
    if d and d.get("rt_cd") == "0":
        out2 = d.get("output2") or []
        rows = out2 if isinstance(out2, list) else [out2]
        for row in rows:
            c = str(row.get("crcy_cd") or "").upper()
            if ccy == "USD" and c not in ("", "USD"):
                continue                          # 통화 행 구분(빈 값=단일행 방어)
            for k in cash_keys:                    # 우선순위 순 — 첫 유효 양수 채택
                try:                               # (max는 여러 잔고필드 중 과대치를
                    v = float(row.get(k))          #  집어 매수여력을 부풀림 — 감사 수정)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    return v
    return best


def _fx() -> float:
    try:
        from bot import settings
        return float(settings.FX_USDKRW)
    except Exception:
        return 1400.0


def buying_power(symbol: str, price: float, excg: str = "NASD") -> float | None:
    """매수여력(USD) — envelope의 feasibility(하향 클램프) 입력.

    `inquire-psamount`의 `ord_psbl_frcr_amt`(외화예수금 기준 주문가능, USD).
    통합증거금 값(echm_af_*·frcr_ord_psbl_amt1)은 **안 쓴다**(cash-only, REFLECTION
    R6 — 원화 환전까지 끌어쓰는 값은 과대 주문 위험). live·mock 동일 소스.
    · mock: psamount **실측 지원 확인**(2026-07-20, rt_cd=0). 이 값을 그대로 씀.
      ① KIS_MOCK_BUYING_POWER override가 있으면 우선(테스트/비상).
      ② psamount 실패 시 국내 원화예수금/fx로 폴백(자동환전 반영, 자가치유),
         그것도 없으면 SEED 폴백(모의에서 사이징이 0으로 막히는 것 방지).
    · live: psamount 실패=None(fail-closed, 폴백 없음).
    """
    if IS_MOCK:
        v = os.environ.get("KIS_MOCK_BUYING_POWER")
        if v is not None:
            try:
                return float(v)
            except ValueError:
                return None
    acct = account()
    if not acct or not enabled():
        return None
    d = _get("/uapi/overseas-stock/v1/trading/inquire-psamount",
             tr_id("psamount"),
             {**acct, "OVRS_EXCG_CD": excg,
              "OVRS_ORD_UNPR": f"{float(price):.2f}", "ITEM_CD": symbol})
    if d and d.get("rt_cd") == "0":
        out = d.get("output") or {}
        try:
            bp = float(out.get("ord_psbl_frcr_amt") or 0)   # 외화예수금 기준(cash-only)
            if bp > 0:
                return bp
        except (TypeError, ValueError):
            pass
    if not IS_MOCK:
        return None                               # live: fail-closed
    # mock 폴백 — psamount가 0/실패여도 사이징이 막히지 않게(원화예수금/fx → SEED)
    krw = _mock_cash(domestic_balance, "KRW", _KRW_CASH_KEYS)
    if krw > 0:
        return krw / max(_fx(), 1e-9)
    return _seed_native("USD")


def fills(excg: str = "NASD", start: str = "", end: str = "") -> dict | None:
    """해외 주문체결내역(inquire-ccnl) — UNKNOWN 대사 채널 B.
    ODNO 검색 불가 → 기간+계좌 조회 후 클라이언트 필터. start/end=YYYYMMDD."""
    acct = account()
    if not acct or not enabled():
        return None
    end = end or time.strftime("%Y%m%d")
    start = start or time.strftime("%Y%m%d", time.localtime(time.time() - 7 * 86400))
    return _get("/uapi/overseas-stock/v1/trading/inquire-ccnl",
                tr_id("ccnl"),
                {**acct, "PDNO": "", "ORD_STRT_DT": start, "ORD_END_DT": end,
                 "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                 "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
                 # 빈 값이라도 필수(누락 시 OPSQ2001 INPUT_FIELD_NAME ORD_DT)
                 "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                 "CTX_AREA_NK200": "", "CTX_AREA_FK200": ""})


# ── 국내(KR) 조회 — 해외와 대칭. 필드·output 키는 모의 왕복 실측 전까지 [대조필요] ──
def domestic_balance() -> dict | None:
    """국내잔고(inquire-balance). output1=보유(hldg_qty·ord_psbl_qty), output2=요약."""
    acct = account()
    if not acct or not enabled():
        return None
    return _get("/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id("balance", market="KR"),
                {**acct, "AFHR_FLPR_YN": "N", "OFL_YN": "",
                 "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                 "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})


def domestic_open_orders() -> dict | None:
    """국내 정정취소가능주문조회(inquire-psbl-rvsecncl) — UNKNOWN 대사 채널 A(국내)."""
    acct = account()
    if not acct or not enabled():
        return None
    return _get("/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
                tr_id("nccs", market="KR"),
                {**acct, "INQR_DVSN_1": "0", "INQR_DVSN_2": "0",
                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})


def domestic_fills(start: str = "", end: str = "") -> dict | None:
    """국내 일별주문체결조회(inquire-daily-ccld) — UNKNOWN 대사 채널 B(국내).
    3개월 이내는 TTTC8001R/VTTC8001R. start/end=YYYYMMDD(기본 최근 7일)."""
    acct = account()
    if not acct or not enabled():
        return None
    end = end or time.strftime("%Y%m%d")
    start = start or time.strftime("%Y%m%d", time.localtime(time.time() - 7 * 86400))
    return _get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id("ccnl", market="KR"),
                {**acct, "INQR_STRT_DT": start, "INQR_END_DT": end,
                 "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
                 "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                 "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})


def domestic_buying_power(symbol: str, price: float) -> float | None:
    """국내 매수여력(원) — inquire-psbl-order. envelope feasibility 입력.
    · live: `ORD_DVSN=00`(지정가)·현금 기준 `nrcvb_buy_amt`(미수 없는 매수가능금액).
    · mock: ① KIS_MOCK_BUYING_POWER(명시 override) → ② 국내잔고 output2의
      주문가능현금(D+2, VTTC8434R)이 양수면 그 값 → ③ 없으면 SEED 폴백(비바인딩).
      실제 원화 현금을 클램프로 쓰므로 계좌 초과 주문 없음."""
    if IS_MOCK:
        v = os.environ.get("KIS_MOCK_BUYING_POWER")
        if v is not None:
            try:
                return float(v)
            except ValueError:
                return None
        krw = _mock_cash(domestic_balance, "KRW", _KRW_CASH_KEYS)
        return krw if krw > 0 else _seed_native("KRW")
    acct = account()
    if not acct or not enabled():
        return None
    d = _get("/uapi/domestic-stock/v1/trading/inquire-psbl-order",
             tr_id("psbl", market="KR"),
             {**acct, "PDNO": symbol, "ORD_UNPR": str(int(price)),
              "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N",
              "OVRS_ICLD_YN": "N"})
    if not d or d.get("rt_cd") != "0":
        return None
    out = d.get("output") or {}
    try:
        return float(out.get("nrcvb_buy_amt") or 0) or None
    except (TypeError, ValueError):
        return None


def buying_power_of(symbol: str, price: float, *, market: str = "US",
                    excg: str = "NASD") -> float | None:
    """시장 라우팅 매수여력 — KR은 원(KRW), US는 달러(USD) 기준. 미확인=None(차단)."""
    if market == "KR":
        return domestic_buying_power(symbol, price)
    return buying_power(symbol, price, excg=excg)


def price_limits(symbol: str) -> tuple[float, float] | None:
    """국내 (하한가, 상한가) — 한국주 일일 ±가격제한. 모의 왕복의 '안 체결 지정가'
    산출용(미국식 현재가×50%는 하한가 아래라 거부됨 — 2026-07-13 실측 40270000).
    output: stck_llam(하한가)·stck_mxpr(상한가). [대조필요] 필드명 실측 확정."""
    if not enabled():
        return None
    d = _get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
    out = (d or {}).get("output") or {}
    try:
        lo = float(out.get("stck_llam") or 0)
        hi = float(out.get("stck_mxpr") or 0)
        return (lo, hi) if lo > 0 and hi > 0 else None
    except (TypeError, ValueError):
        return None


_US_EXCG_CACHE: dict[str, str] = {}   # symbol → NASD/NYSE/AMEX (프로세스 캐시)


def us_excg_of(symbol: str) -> str:
    """미국 종목의 상장 거래소(NASD/NYSE/AMEX) 판별 — 시세가 잡히는 거래소.

    주문 OVRS_EXCG_CD 정확도용(감사 수정): 코드가 US 주문을 전부 NASD로 보내면
    NYSE/AMEX 종목이 live에서 거부돼 손절/매수가 실패한다(모의는 관대). 시세
    조회로 실제 거래소를 찾아 캐시. 판별 실패 시 NASD(기존 동작).
    """
    s = str(symbol).upper()
    if s in _US_EXCG_CACHE:
        return _US_EXCG_CACHE[s]
    if not enabled():
        return "NASD"
    m = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
    for excd in ("NAS", "NYS", "AMS"):
        d = _get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                 {"AUTH": "", "EXCD": excd, "SYMB": s})
        try:
            px = float(((d or {}).get("output") or {}).get("last") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            _US_EXCG_CACHE[s] = m[excd]
            return m[excd]
    return "NASD"


def holdings(market: str = "US", excg: str = "NASD") -> dict | None:
    """브로커 실보유 {symbol.upper(): qty} — 브로커-진실 대조용(매수루프·파수꾼).
    조회 실패/불완전(연속조회 남음)=None(신뢰 불가 → 호출부 보수 처리).
    국내 output1: pdno·hldg_qty / 해외 output1: ovrs_pdno·ovrs_cblc_qty."""
    d = domestic_balance() if market == "KR" else overseas_balance(excg=excg)
    if not d or d.get("rt_cd") != "0":
        return None
    # 다중페이지 미완 → 신뢰 불가. 국내=NK100, 해외=NK200 (감사 수정: 해외 키 누락 시
    #   여러 페이지 보유가 조용히 잘려 '미보유'로 오판 → 중복매수/보호 누락 위험).
    if str(d.get("ctx_area_nk100") or d.get("CTX_AREA_NK100")
           or d.get("ctx_area_nk200") or d.get("CTX_AREA_NK200") or "").strip():
        return None
    out: dict[str, int] = {}
    for r in (d.get("output1") or []):
        sym = str(r.get("pdno") or r.get("ovrs_pdno") or "").upper()
        if not sym:
            continue
        try:
            q = int(float(r.get("hldg_qty") or r.get("ovrs_cblc_qty") or 0))
        except (TypeError, ValueError):
            return None
        if q:
            out[sym] = out.get(sym, 0) + q
    return out


def last_price(symbol: str, *, market: str = "US", excg: str = "NASD") -> float | None:
    """현재가 조회(주문가 산출용). KR=국내시세(FHKST01010100), US=해외시세."""
    if not enabled():
        return None
    if market == "KR":
        d = _get("/uapi/domestic-stock/v1/quotations/inquire-price",
                 "FHKST01010100",
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        try:
            return float(((d or {}).get("output") or {}).get("stck_prpr") or 0) or None
        except (TypeError, ValueError):
            return None
    # 미국: 신호에 거래소 정보가 없어 excg가 기본값(NASD)이면 NAS→NYS→AMS 순차 시도
    #   (종목이 NYSE/AMEX면 NAS로 조회 시 시세 없음 → '조회 실패'가 났던 원인).
    m = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
    excds = ["NAS", "NYS", "AMS"] if excg == "NASD" else [m.get(excg, "NAS")]
    for excd in excds:
        d = _get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                 {"AUTH": "", "EXCD": excd, "SYMB": symbol})
        try:
            px = float(((d or {}).get("output") or {}).get("last") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            return px
    return None


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def positions_detail(market: str = "US", excg: str = "NASD") -> list[dict] | None:
    """보유 종목 상세(텔레그램 조회용) — 브로커 잔고 스냅샷의 정규화 리스트.

    조회 실패/다중페이지 미완 = None(신뢰 불가 → 호출부 보수 처리).
    정규화 필드: code·name·qty·avg(평단)·cur(현재가 스냅샷)·eval_amt(평가금액)·
      buy_amt(매입금액)·pl_amt(평가손익)·pl_rt(평가손익률%)·ccy·market.
    현재가는 잔고 응답 기준(준실시간) — 단일 종목 상세는 last_price로 재조회 권장.
    KR/US는 output1 필드명이 다르고 일부 [대조필요] → 후보 키 순차 파싱(방어적).
    스냅샷 손익 필드가 비면 avg·cur·qty로 보정.
    """
    d = domestic_balance() if market == "KR" else overseas_balance(excg=excg)
    if not d or d.get("rt_cd") != "0":
        return None
    if str(d.get("ctx_area_nk100") or d.get("ctx_area_nk200")
           or d.get("CTX_AREA_NK100") or d.get("CTX_AREA_NK200") or "").strip():
        return None                               # 다중페이지 미완 → 신뢰 불가
    ccy = "KRW" if market == "KR" else "USD"

    def pick(r: dict, *keys: str):
        for k in keys:
            if r.get(k) not in (None, ""):
                return r.get(k)
        return None

    rows: list[dict] = []
    for r in (d.get("output1") or []):
        code = str(pick(r, "pdno", "ovrs_pdno") or "").upper()
        if not code:
            continue
        qty = int(_f(pick(r, "hldg_qty", "ovrs_cblc_qty")))
        if qty <= 0:                              # 잔량 0(정리된 종목) 제외
            continue
        avg = _f(pick(r, "pchs_avg_pric", "pchs_avg_unpr", "avg_unpr"))
        cur = _f(pick(r, "prpr", "now_pric2", "ovrs_now_pric1", "ovrs_now_pric"))
        eval_amt = _f(pick(r, "evlu_amt", "ovrs_stck_evlu_amt", "frcr_evlu_amt2"))
        buy_amt = _f(pick(r, "pchs_amt", "frcr_pchs_amt1", "frcr_pchs_amt"))
        pl_amt = _f(pick(r, "evlu_pfls_amt", "frcr_evlu_pfls_amt",
                         "ovrs_evlu_pfls_amt"))
        pl_rt = _f(pick(r, "evlu_pfls_rt", "evlu_pfls_rt1"))
        if eval_amt <= 0 and cur > 0:             # 스냅샷 결손 → 보정
            eval_amt = cur * qty
        if buy_amt <= 0 and avg > 0:
            buy_amt = avg * qty
        if pl_amt == 0 and cur > 0 and avg > 0:
            pl_amt = (cur - avg) * qty
        if pl_rt == 0 and avg > 0 and cur > 0:
            pl_rt = (cur / avg - 1) * 100
        rows.append({
            "code": code,
            "name": str(pick(r, "prdt_name", "ovrs_item_name") or code),
            "qty": qty, "avg": avg, "cur": cur, "eval_amt": eval_amt,
            "buy_amt": buy_amt, "pl_amt": pl_amt, "pl_rt": pl_rt,
            "ccy": ccy, "market": market})
    return rows
