#!/usr/bin/env python3
"""토스 Open API 읽기 전용 진단 프로브 — 실계좌 검증(V1·V3 읽기)용.

주문·조건주문·취소 **절대 없음**. 토큰·시세·계좌·보유·매수여력·매도가능수량만 조회.
시크릿은 어떤 경우에도 출력하지 않는다. 표준 라이브러리만.

사용(로컬, 집 컴퓨터에서):
  export TOSS_CLIENT_ID='발급받은_아이디'
  export TOSS_CLIENT_SECRET='발급받은_시크릿'
  python scripts/toss_probe.py                     # 토큰+계좌+시세 전체 점검
  python scripts/toss_probe.py --symbol 005930     # 특정 종목 시세/매도가능수량까지
  python scripts/toss_probe.py --account 1         # accountSeq 지정(계좌 여러 개일 때)

성공 기준: 토큰 발급 OK + accounts에 accountSeq 나옴 + holdings/buying-power 200.
실패하면: 상태코드·requestId·error.code를 (시크릿 없이) 출력 → 그대로 공유하면 진단 가능.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TOSS_BASE_URL", "https://openapi.tossinvest.com")
TIMEOUT = 20


def _mask(s: str | None) -> str:
    if not s:
        return "(없음)"
    s = str(s)
    return s[:2] + "…" + s[-4:] if len(s) > 6 else "…"


def get_token() -> str | None:
    cid = os.environ.get("TOSS_CLIENT_ID")
    sec = os.environ.get("TOSS_CLIENT_SECRET")
    if not (cid and sec):
        print("✗ TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 없습니다.")
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(
        BASE + "/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        tok = d.get("access_token")
        print(f"✓ 토큰 발급 성공 (만료 {d.get('expires_in')}초, 타입 {d.get('token_type')})")
        return tok
    except urllib.error.HTTPError as e:
        _show_err("POST /oauth2/token", e)
    except Exception as e:
        print(f"✗ 토큰 발급 오류: {type(e).__name__}: {e}")
    return None


def _show_err(what: str, e: urllib.error.HTTPError) -> None:
    body = ""
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:
        pass
    rid = e.headers.get("X-Request-Id") if e.headers else None
    code = ""
    try:
        code = (json.loads(body).get("error") or {}).get("code", "")
    except Exception:
        pass
    print(f"✗ {what} → HTTP {e.code}"
          + (f" · code={code}" if code else "")
          + (f" · requestId={rid}" if rid else ""))
    # 본문은 시크릿을 담지 않으므로 앞부분만 노출(진단용)
    if body:
        print("   " + body[:200].replace("\n", " "))


def get(token: str, path: str, params: dict | None = None,
        account: str | None = None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if account is not None:
        headers["X-Tossinvest-Account"] = str(account)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        _show_err(f"GET {path}", e)
        return None, e.code
    except Exception as e:
        print(f"✗ GET {path} 오류: {type(e).__name__}: {e}")
        return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description="토스 API 읽기전용 진단(주문 없음)")
    ap.add_argument("--symbol", default="AAPL", help="시세/매도가능수량 조회 종목")
    ap.add_argument("--account", default=None, help="accountSeq 명시(계좌 여러 개일 때)")
    args = ap.parse_args()

    print(f"base = {BASE}")
    tok = get_token()
    if not tok:
        return 1

    # 1) 시세 (계좌 헤더 불필요)
    print("\n[시세] GET /api/v1/prices")
    d, _ = get(tok, "/api/v1/prices", {"symbols": f"{args.symbol},005930"})
    for row in (d or {}).get("result", []) if d else []:
        print(f"   {row.get('symbol')}: {row.get('lastPrice')} {row.get('currency')}"
              f"  ts={row.get('timestamp')}")

    # 2) 계좌 목록 → accountSeq
    print("\n[계좌] GET /api/v1/accounts")
    d, _ = get(tok, "/api/v1/accounts")
    accounts = (d or {}).get("result") if d else None
    if isinstance(accounts, dict):          # 단일/래핑 형태 방어
        accounts = accounts.get("items") or [accounts]
    seq = args.account
    if accounts:
        for a in accounts:
            print(f"   accountSeq={a.get('accountSeq')} · "
                  f"번호…{_mask(a.get('accountNo'))} · type={a.get('accountType')}")
        if seq is None:
            if len(accounts) == 1:
                seq = accounts[0].get("accountSeq")
                print(f"   → 계좌 1개, accountSeq={seq} 사용")
            else:
                print("   → 계좌가 여러 개입니다. --account <seq>로 지정하세요(자동선택 안 함).")
    if seq is None:
        print("\naccountSeq를 못 정해 계좌 상세 조회는 건너뜁니다.")
        return 0

    # 3) 계좌 상세 (헤더 필요) — 전부 읽기 전용
    for label, path, params in [
        ("보유(holdings)", "/api/v1/holdings", None),
        ("매수여력(buying-power)", "/api/v1/buying-power", None),
        ("매도가능수량(sellable-quantity)", "/api/v1/sellable-quantity",
         {"symbol": args.symbol}),
    ]:
        print(f"\n[{label}] GET {path}  (X-Tossinvest-Account={seq})")
        d, _ = get(tok, path, params, account=seq)
        if d is not None:
            print("   " + json.dumps(d.get("result", d), ensure_ascii=False)[:400])

    print("\n완료 — 위 결과(시크릿 없음)를 공유하면 다음 단계를 정확히 안내합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
