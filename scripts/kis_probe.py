#!/usr/bin/env python3
"""KIS(한국투자증권) Open API 읽기 전용 진단 프로브 — 모의/실전 계좌 검증용.

주문·정정·취소 **절대 없음**. 토큰 발급 + 해외잔고 + 미체결내역 + (가능하면) 시세만 조회.
appsecret·토큰은 어떤 경우에도 출력하지 않는다. 표준 라이브러리만.

사용(로컬, 집 컴퓨터에서 — 모의 먼저):
  export KIS_ENV=mock                          # mock(모의) / live(실전). 기본 mock
  export KIS_MOCK_APPKEY='발급받은_앱키'
  export KIS_MOCK_APPSECRET='발급받은_앱시크릿'
  export KIS_MOCK_CANO='모의계좌_앞8자리'
  export KIS_MOCK_ACNT_PRDT_CD='01'            # 주식 상품코드(보통 01)
  python scripts/kis_probe.py                   # 토큰+잔고+미체결 점검
  python scripts/kis_probe.py --symbol AAPL --excg NASD   # 특정 종목/거래소

실전은 KIS_ENV=live + KIS_LIVE_* 환경변수(같은 이름 규칙). 실전은 조회만 해도 실계좌라
**주문은 이 스크립트에 아예 없음** — 안심하고 실행 가능.

성공 기준: 토큰 발급 OK(rt 없이 access_token 수신) + 해외잔고 rt_cd=0.
실패하면: HTTP status·rt_cd·msg_cd·msg1을 (시크릿 없이) 출력 → 그대로 공유하면 진단 가능.

주의(조사 반영):
  - 토큰 발급은 **1분당 1회** 제한 → 반복 실행 시 텀을 둘 것(발급 실패 시 잠시 후 재시도).
  - **모의는 매수가능금액조회·예약주문조회·분봉·10호가 미지원** → 이 프로브는 그걸 안 부른다.
  - TR_ID는 (실전 T… / 모의 V…) 규칙. **미국 매도 주문만 비대칭**이나 이 프로브는 주문이
    없어 무관. 조회 TR은 접두 치환으로 안전.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ── 환경(모의/실전) ────────────────────────────────────────────────
ENV = os.environ.get("KIS_ENV", "mock").lower()
IS_MOCK = ENV != "live"
BASE = ("https://openapivts.koreainvestment.com:29443" if IS_MOCK
        else "https://openapi.koreainvestment.com:9443")
PFX = "MOCK" if IS_MOCK else "LIVE"
TIMEOUT = 20


def _tr(real_tr: str) -> str:
    """조회 TR을 환경에 맞게. 모의는 앞글자 T→V(조회 TR은 이 규칙이 안전)."""
    return ("V" + real_tr[1:]) if IS_MOCK else real_tr


def _env(name: str) -> str | None:
    return os.environ.get(f"KIS_{PFX}_{name}")


def _mask(s: str | None) -> str:
    if not s:
        return "(없음)"
    s = str(s)
    return s[:2] + "…" + s[-2:] if len(s) > 4 else "…"


def get_token(appkey: str, appsecret: str) -> str | None:
    body = json.dumps({
        "grant_type": "client_credentials",
        "appkey": appkey, "appsecret": appsecret}).encode()
    req = urllib.request.Request(
        BASE + "/oauth2/tokenP", data=body,
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        tok = d.get("access_token")
        if tok:
            print(f"✓ 토큰 발급 성공 (만료 {d.get('expires_in')}초, "
                  f"만료일시 {d.get('access_token_token_expired')})")
            return tok
        # 200인데 토큰이 없으면 본문에 에러가 실려 있음(시크릿 없음)
        print(f"✗ 토큰 발급 실패 본문: {json.dumps(d, ensure_ascii=False)[:200]}")
    except urllib.error.HTTPError as e:
        _show_http_err("POST /oauth2/tokenP", e)
    except Exception as e:
        print(f"✗ 토큰 발급 오류: {type(e).__name__}: {e}")
    return None


def _show_http_err(what: str, e: urllib.error.HTTPError) -> None:
    body = ""
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:
        pass
    print(f"✗ {what} → HTTP {e.code}")
    if body:
        # 본문은 appkey/secret을 담지 않음(요청 헤더에만 있음) → 앞부분만 진단 노출
        print("   " + body[:300].replace("\n", " "))


def get(token: str, appkey: str, appsecret: str, path: str,
        tr_id: str, params: dict, retries: int = 2) -> dict | None:
    """조회 GET. 레이트리밋(EGW00201, HTTP 500)이면 잠시 후 재시도."""
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": appkey, "appsecret": appsecret,
        "tr_id": tr_id, "custtype": "P",
    }
    for attempt in range(retries + 1):
        d = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            # KIS는 레이트리밋을 HTTP 500 + 본문 rt_cd로 준다 → 본문 파싱
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
                d = json.loads(body)
            except Exception:
                _show_http_err(f"GET {path} (tr_id={tr_id})", e)
                return None
        except Exception as e:
            print(f"✗ GET {path} 오류: {type(e).__name__}: {e}")
            return None

        if d and d.get("msg_cd") == "EGW00201" and attempt < retries:
            print(f"   … 초당 유량초과(EGW00201) — 1.2초 후 재시도 "
                  f"({attempt + 1}/{retries})")
            time.sleep(1.2)
            continue

        rt = d.get("rt_cd") if d else None
        tag = "✓" if rt == "0" else "✗"
        print(f"   {tag} tr_id={tr_id} rt_cd={rt} "
              f"msg_cd={(d or {}).get('msg_cd')} msg={(d or {}).get('msg1','').strip()}")
        return d
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="KIS API 읽기전용 진단(주문 없음)")
    ap.add_argument("--symbol", default="AAPL", help="조회 종목(티커)")
    ap.add_argument("--excg", default="NASD", help="거래소(NASD/NYSE/AMEX)")
    args = ap.parse_args()

    print(f"env = {ENV}  ({'모의' if IS_MOCK else '실전'})")
    print(f"base = {BASE}\n")

    appkey = _env("APPKEY")
    appsecret = _env("APPSECRET")
    cano = _env("CANO")
    prdt = _env("ACNT_PRDT_CD") or "01"
    if not (appkey and appsecret):
        print(f"✗ KIS_{PFX}_APPKEY / KIS_{PFX}_APPSECRET 환경변수가 없습니다.")
        return 1
    print(f"appkey={_mask(appkey)}  cano={_mask(cano)}  prdt={prdt}\n")

    tok = get_token(appkey, appsecret)
    if not tok:
        print("\n※ 토큰 발급은 1분당 1회 제한 — 방금 실패했다면 잠시 후 다시.")
        return 1

    if not cano:
        print(f"\n✗ KIS_{PFX}_CANO(계좌 앞 8자리)가 없어 계좌 조회는 건너뜁니다.")
        return 0

    acct = {"CANO": cano, "ACNT_PRDT_CD": prdt}

    # 1) 해외잔고 (모의/실전 모두 지원) — output1=보유, output2=요약
    print("\n[해외잔고] GET /uapi/overseas-stock/v1/trading/inquire-balance")
    d = get(tok, appkey, appsecret,
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            _tr("TTTS3012R"),
            {**acct, "OVRS_EXCG_CD": args.excg, "TR_CRCY_CD": "USD",
             "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
    if d and d.get("rt_cd") == "0":
        holdings = d.get("output1") or []
        print(f"   보유 {len(holdings)}종목:")
        for h in holdings[:10]:
            print(f"     {h.get('ovrs_pdno')} {h.get('ovrs_item_name','')}"
                  f" 수량={h.get('ovrs_cblc_qty')} 주문가능={h.get('ord_psbl_qty')}"
                  f" 평단={h.get('pchs_avg_pric')} 평가손익={h.get('frcr_evlu_pfls_amt')}")

    # 2) 해외 미체결내역 (모의/실전 모두 지원) — UNKNOWN 대사 채널 A
    time.sleep(0.7)   # 모의 초당 2건 제한 회피(잔고 호출과 간격)
    print("\n[해외미체결] GET /uapi/overseas-stock/v1/trading/inquire-nccs")
    d = get(tok, appkey, appsecret,
            "/uapi/overseas-stock/v1/trading/inquire-nccs",
            _tr("TTTS3018R"),
            {**acct, "OVRS_EXCG_CD": args.excg, "SORT_SQN": "DS",
             "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
    if d and d.get("rt_cd") == "0":
        oo = d.get("output") or []
        print(f"   미체결 {len(oo)}건" + (":" if oo else ""))
        for o in oo[:10]:
            print(f"     ODNO={o.get('odno')} {o.get('pdno')}"
                  f" {o.get('sll_buy_dvsn_cd_name','')} 주문={o.get('ft_ord_qty')}"
                  f" 미체결={o.get('nccs_qty')}")

    print("\n완료 — 위 결과(시크릿 없음)를 공유하면 다음 단계를 정확히 안내합니다.")
    print("※ 매수가능금액·예약주문조회·분봉·10호가는 모의 미지원이라 이 프로브는 부르지 않음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
