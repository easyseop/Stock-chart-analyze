"""토스증권 Open API 어댑터 — Stage 0: 시세 읽기 전용(주문·계좌 미사용).

설계 원칙(안전 우선):
  1. 표준 라이브러리만 사용(requests 없음 — notify.py와 동일 방침).
  2. 키 미설정이면 전면 비활성 → 호출부가 기존 FDR(야후/Stooq)로 자동 폴백.
     즉 키가 없으면 시스템 동작은 지금과 100% 동일(리스크 0).
  3. 어떤 실패도 예외를 밖으로 던지지 않는다 — 조회 실패는 빈 결과 → 폴백.
  4. 키 값(client_secret)은 절대 로그·예외 메시지에 담지 않는다.
  5. 주문·계좌 엔드포인트는 이 파일에 두지 않는다(Stage 0은 시세만).

토큰 주의: 토스는 "client당 유효 토큰 1개 — 재발급 시 이전 토큰 즉시 무효화".
  따라서 프로세스 내 단일 캐시로만 발급하고 매 호출마다 재발급하지 않는다.
  (서버 상시 운용 시 루프 여러 개가 각자 발급하면 서로 무효화 → 반드시 단일
   token_manager. Stage 0은 배치 단일 프로세스라 모듈 전역 캐시로 충분.)

검증 출처: developers.tossinvest.com — OpenAPI 3.1(openapi.json), 2026-07 확인.
  POST /oauth2/token  (application/x-www-form-urlencoded, grant_type=client_credentials)
      → { access_token, token_type: "Bearer", expires_in }
  GET  /api/v1/prices?symbols=AAPL,005930  (콤마 구분, 최대 200건)
      → { result: [ { symbol, timestamp(ISO8601|null), lastPrice(str), currency } ] }

환경변수(값은 절대 코드/채팅/로그에 두지 말 것 — 시크릿으로만):
  TOSS_CLIENT_ID      발급받은 클라이언트 ID
  TOSS_CLIENT_SECRET  발급받은 클라이언트 시크릿
  TOSS_BASE_URL       (선택) 기본 https://openapi.tossinvest.com
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("TOSS_BASE_URL", "https://openapi.tossinvest.com")
PRICES_MAX = 200            # /api/v1/prices 다건 조회 상한(콤마 구분)
_TOKEN_SKEW = 300           # 만료 이 초 전이면 선제 재발급(시계 오차·왕복 여유)
_TOKEN_FAIL_COOLDOWN = 60   # 토큰 발급 실패 시 이 초간 재시도 안 함(음성 캐시)
_HTTP_TIMEOUT = 15

# 모듈 전역 토큰 캐시 — {"tok": str|None, "exp": epoch초, "fail_until": epoch초}
#   fail_until: 발급이 계속 실패할 때 매 시세 호출마다 15초씩 타임아웃을 무는 것을
#   막는 음성 캐시(장애 중 손절 감시 주기 회귀 방지 — 감사 CONFIRMED).
_TOK: dict = {"tok": None, "exp": 0.0, "fail_until": 0.0}


def enabled() -> bool:
    """키가 둘 다 있으면 True. 없으면 어댑터 비활성(호출부는 FDR 폴백)."""
    return bool(os.environ.get("TOSS_CLIENT_ID")
                and os.environ.get("TOSS_CLIENT_SECRET"))


# ── 에러 대응 분류 ────────────────────────────────────────────────
#   토스는 에러 code를 flat string으로 주고 "unknown code 허용" 설계라, 1차 축은
#   HTTP status, 2차는 알려진 code. 결정적 포인트: **같은 status가 조회(GET)와
#   주문(POST)에서 다른 의미**를 갖는다.
#     · 조회 500 = 그냥 재시도/폴백(무해)   · 주문 500 = 결과 불명(UNKNOWN, 위험)
#     · 조회 409 = 클라 오류               · 주문 409 = 중복 접수(이미 들어감, 멱등)
#   타임아웃(HTTP status 없음)은 status=0으로 넘긴다 → 주문이면 UNKNOWN.
ACT_OK = "ok"
ACT_RETRY = "retry"            # 일시 오류·429 → 지수 백오프 후 재시도
ACT_REFRESH = "refresh_auth"   # 401 → 토큰 재발급 후 1회 재시도
ACT_AUTH_FATAL = "auth_fatal"  # 403/자격증명 오류 → 재시도 무의미, P0
ACT_UNKNOWN = "unknown"        # 주문 결과 불명 → 재주문 절대 금지, 원장 잠금·대사
ACT_DUPLICATE = "duplicate"    # 이미 접수됨(멱등 충돌) → 재전송 말고 대사
ACT_REJECT = "reject"          # 비즈니스/클라 오류(422·400) → 재시도 무의미(종료)
ACT_NOT_FOUND = "not_found"    # 404 — 종목/주문/계좌 없음


def classify_error(status: int, code: str = "", *, is_order: bool = False) -> str:
    """(HTTP status, 에러 code) → 대응 액션 문자열. is_order면 주문 경로 규칙 적용.

    주문 경로의 핵심 안전 규칙: 결과가 조금이라도 불확실하면(5xx·타임아웃·미지의
    status) REJECT가 아니라 **UNKNOWN**으로 분류한다 → 원장이 종목을 잠그고 실체결
    대사 후 잔여만 재주문 → 초과매도 원천 차단. (감사·설계 원칙과 일치.)"""
    if status in (200, 201, 204):
        return ACT_OK
    if status == 401:
        return ACT_REFRESH
    if status == 403:
        return ACT_AUTH_FATAL
    if status == 429:
        return ACT_RETRY
    if status in (408, 0, 500, 502, 503, 504):      # 0=네트워크 타임아웃(status 없음)
        return ACT_UNKNOWN if is_order else ACT_RETRY
    if status == 409:                                # 중복 요청
        return ACT_DUPLICATE if is_order else ACT_REJECT
    if status == 404:
        return ACT_NOT_FOUND
    if status in (400, 422):                         # 잘못된 요청 / 비즈니스 규칙 위반
        return ACT_REJECT
    # 미지의 status — 주문이면 안전하게 UNKNOWN(초과매도 방지), 조회면 재시도.
    return ACT_UNKNOWN if is_order else ACT_RETRY


def _fetch_token() -> str | None:
    cid = os.environ.get("TOSS_CLIENT_ID")
    sec = os.environ.get("TOSS_CLIENT_SECRET")
    if not (cid and sec):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid, "client_secret": sec,
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + "/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            d = json.load(resp)
        tok = d.get("access_token")
        exp = float(d.get("expires_in", 3600))
        if tok:
            _TOK["tok"] = tok
            _TOK["exp"] = time.time() + exp
            _TOK["fail_until"] = 0.0        # 복구 즉시 정상화
            return tok
    except Exception as e:
        # 키 값은 절대 남기지 않는다 — 예외 타입만.
        _TOK["fail_until"] = time.time() + _TOKEN_FAIL_COOLDOWN
        print(f"[toss] 토큰 발급 실패({type(e).__name__}) — FDR 폴백")
    return None


def _token(force: bool = False) -> str | None:
    if not enabled():
        return None
    if not force and _TOK["tok"] and time.time() < _TOK["exp"] - _TOKEN_SKEW:
        return _TOK["tok"]
    # 최근 발급 실패 쿨다운 중이면 재시도 안 하고 즉시 폴백(장애 중 주기 회귀 방지).
    #   force(401 재발급)는 예외 — 진짜 만료는 즉시 갱신 필요.
    if not force and time.time() < _TOK.get("fail_until", 0.0):
        return None
    return _fetch_token()


def _get(path: str, params: dict) -> dict | None:
    """인증 GET — 401이면 토큰 1회 강제 갱신 후 재시도. 실패는 None."""
    tok = _token()
    if not tok:
        return None
    url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    for attempt in (0, 1):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {tok}",
                          "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:      # 만료/무효 → 1회 재발급
                tok = _token(force=True)
                if tok:
                    continue
            print(f"[toss] GET {path} 실패(HTTP {e.code}) — FDR 폴백")
            return None
        except Exception as e:
            print(f"[toss] GET {path} 실패({type(e).__name__}) — FDR 폴백")
            return None
    return None


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def prices(symbols: list[str]) -> dict[str, dict]:
    """다건 현재가 조회(읽기 전용). 반환: {symbol: {price, ts, currency}}.

    - price: float(원/달러 원값), ts: ISO8601 문자열 또는 None(체결 미발생),
      currency: 'KRW'/'USD'.
    - 비활성/실패/누락 종목은 결과에서 빠진다(→ 호출부가 그 종목만 FDR 폴백).
    """
    syms = [s for s in dict.fromkeys(symbols) if s]   # 중복·빈값 제거, 순서 보존
    if not syms or not enabled():
        return {}
    out: dict[str, dict] = {}
    for group in _chunks(syms, PRICES_MAX):
        d = _get("/api/v1/prices", {"symbols": ",".join(group)})
        if not d:
            continue
        for row in (d.get("result") or []):
            sym = row.get("symbol")
            lp = row.get("lastPrice")
            if not sym or lp in (None, ""):
                continue
            try:
                px = float(lp)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            out[sym] = {"price": px, "ts": row.get("timestamp"),
                        "currency": row.get("currency")}
    return out


def price(symbol: str) -> dict | None:
    """단건 현재가. 실패/비활성이면 None."""
    return prices([symbol]).get(symbol)


def quote_price(symbol: str) -> float | None:
    """단건 현재가 float만 — advisor/sentinel 폴백 체인의 1순위 소스용.
    토스가 값을 주면 float, 아니면 None(호출부가 FDR로 폴백)."""
    p = price(symbol)
    return p["price"] if p else None


def _selftest(symbols: list[str]) -> None:
    """`python -m bot.toss --selftest AAPL,005930` — 키 검증용(값 미출력)."""
    if not enabled():
        print("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정 — 어댑터 비활성 상태.")
        print("시크릿을 설정하면 이 명령으로 시세 조회를 확인할 수 있습니다.")
        return
    print(f"키 감지됨. base={BASE_URL}")
    tok = _token()
    print("토큰 발급:", "성공" if tok else "실패")
    if not tok:
        return
    res = prices(symbols)
    if not res:
        print("시세 조회 결과 없음(심볼/시장/장 상태 확인).")
        return
    for sym, q in res.items():
        cur = q.get("currency") or ""
        print(f"  {sym}: {q['price']} {cur}  ts={q.get('ts')}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="토스 시세 어댑터 자가진단")
    ap.add_argument("--selftest", metavar="SYMBOLS",
                    default="AAPL,005930",
                    help="콤마 구분 심볼(기본 AAPL,005930)")
    args = ap.parse_args()
    _selftest([s.strip() for s in args.selftest.split(",") if s.strip()])


if __name__ == "__main__":
    main()
