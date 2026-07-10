"""KIS 주문 primitive (Stage 1.5 · 모의 전용) — 생성/취소 + 원장 결속.

⚠️ 안전 게이트(전부 통과해야 실제 전송):
  1. **live 하드블록** — KIS_ENV=live면 무조건 거부(Stage 2 게이트 전 실전 주문 불가).
     이 모듈은 모의(vps) 도메인에서만 전송한다.
  2. `KIS_ORDERS_ENABLED=1` 명시돼야 전송(기본 미설정=전송 불가).
  3. `ledger.can_submit(symbol)` — UNKNOWN 잠금·동일종목 in-flight·60초 간격(R3).
  4. 이 모듈은 **어디서도 자동 호출되지 않는다** — autopaper/sentinel 미연결.
     Stage 1.5 왕복 검증 스크립트/테스트만 명시적으로 호출한다.

흐름(초과매도 방지 — 설계 O2·§16):
  게이트 → 유량(order-plane, 짧은 대기) → **원장 선기록(record_submit)** → POST →
    · rt_cd=0: ODNO/ORGNO/ORD_TMD **결속(bind)** + ack
    · EGW00201: 1.2s 백오프 1회 재시도 → 실패 시 rejected(rate_limited) — 서버가
      명시 거부했으므로 UNKNOWN 아님. 호출부는 P0(손절 집행 불능 경보).
    · 확정 거부(rt_cd≠0): rejected
    · 타임아웃/5xx/파싱불가: **unknown → 종목 잠금** → kis_reconcile로만 해소
  취소도 동일 — 취소 응답 유실 시 원주문 생사 불명이므로 unknown 잠금(§16).

가격 규칙: 연속장 시장가 없음 → **마켓터블 지정가**(marketable_limit_price).
수량: whole-share 정수만(Stage 2 첫 주 제한과 일치).
MGCO_APTM_ODNO: 항상 우리 태그(숫자 10자리)를 채운다 — 에코 여부는 실측(리뷰 A5),
  멱등 근거로는 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from bot import kis, kis_ratelimit, ledger

_HTTP_TIMEOUT = 15
_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
_CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"

# 주문 전용 리미터 공유(kis._LIMITER와 같은 인스턴스 — 총 유량 일관)
_LIMITER: kis_ratelimit.SecondBucket = kis._LIMITER


def orders_allowed() -> tuple[bool, str]:
    """전송 허용 여부와 사유. live는 어떤 플래그로도 안 열린다(Stage 2 전)."""
    if not kis.IS_MOCK:
        return False, "live 주문 하드블록(Stage 2 게이트 전) — 모의 전용"
    if os.environ.get("KIS_ORDERS_ENABLED") != "1":
        return False, "KIS_ORDERS_ENABLED != 1 (명시 필요)"
    if not kis.enabled():
        return False, "appkey/appsecret 미설정"
    if not kis.account():
        return False, "CANO 미설정"
    return True, "ok"


def marketable_limit_price(last_price: float, side: str,
                           slippage_bps: int = 30) -> float:
    """연속장 시장가 부재 → 즉시 체결 지향 지정가.
    SELL: 현재가보다 낮게(팔리게), BUY: 높게(사지게). 기본 30bp, 2자리 반올림.
    ※ US 1달러 미만 소수점 4자리 규칙은 [대조필요] — Stage 1.5 실측."""
    px = float(last_price)
    adj = px * (slippage_bps / 10_000.0)
    out = px - adj if side.upper() == "SELL" else px + adj
    return round(max(0.01, out), 2)


def _mgco_tag(key: str) -> str:
    """MGCO_APTM_ODNO용 우리 태그 — 숫자 10자리(형식 제약 [대조필요]라 보수적)."""
    return str(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16) % 10 ** 10
               ).zfill(10)


def _post(path: str, tr: str, body: dict) -> tuple[dict | None, int]:
    """인증 POST. (파싱된 본문|None, http_status). 네트워크 실패는 (None, 0)."""
    tok = kis._token()
    if not tok:
        return None, 0
    k, s = kis._cred()
    headers = {"content-type": "application/json; charset=utf-8",
               "authorization": f"Bearer {tok}",
               "appkey": k, "appsecret": s, "tr_id": tr, "custtype": "P"}
    req = urllib.request.Request(kis.BASE_URL + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.load(resp), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore")), e.code
        except Exception:
            return None, e.code
    except Exception:
        return None, 0                            # 타임아웃/네트워크 — UNKNOWN 경로


def place_order(key: str, symbol: str, side: str, qty: int, price: float,
                *, excg: str = "NASD", reason: str = "",
                min_interval_s: float = 60.0) -> dict:
    """주문 1건 전송(모의 전용). 반환 {ok, act, key, odno?, why?}.

    key: 포지션 정체성 멱등키(호출부 소유, 예 '{pos}#{n}'). 같은 키 재호출 금지 —
         잔여 재주문은 새 키(#n+1)로. side: 'BUY'|'SELL'. qty: whole-share 정수.
    """
    side = side.upper()
    ok, why = orders_allowed()
    if not ok:
        return {"ok": False, "act": "blocked", "key": key, "why": why}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "act": "blocked", "key": key, "why": f"side={side}"}
    qty = int(qty)
    if qty < 1 or float(price) <= 0:
        return {"ok": False, "act": "blocked", "key": key, "why": "qty/price 무효"}
    if not ledger.can_submit(symbol, min_interval_s=min_interval_s):
        return {"ok": False, "act": "blocked", "key": key,
                "why": "원장 게이트(잠금/in-flight/간격)"}
    if not _LIMITER.acquire("order", timeout=5.0):
        return {"ok": False, "act": "rate_limited", "key": key,
                "why": "order-plane 유량 슬롯 없음(5s)"}

    acct = kis.account()
    tr = kis.tr_id("buy" if side == "BUY" else "sell", market="US")
    body = {**acct, "OVRS_EXCG_CD": excg, "PDNO": symbol,
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
            "SLL_TYPE": "00" if side == "SELL" else "",
            "CTAC_TLNO": "", "MGCO_APTM_ODNO": _mgco_tag(key)}

    # 원장 선기록(전송 전 — 크래시 대비) + 합성키(타임아웃 대사 근거)
    ledger.record_submit(key, symbol, qty, reason,
                         meta={"side": side, "price": float(price),
                               "excg": excg, "env": kis.ENV})
    ledger.record_synthetic(key, ledger.synthetic_key(
        acct["CANO"], excg, symbol, side, qty, price,
        time.strftime("%H%M%S")))

    for attempt in (0, 1):
        d, http = _post(_ORDER_PATH, tr, body)
        act = kis.classify_error((d or {}).get("rt_cd"), (d or {}).get("msg_cd"),
                                 http, is_order=True)
        if act == kis.ACT_OK:
            out = (d or {}).get("output") or {}
            odno = str(out.get("ODNO") or out.get("odno") or "")
            ledger.bind_broker_order(key, odno,
                                     orgno=str(out.get("KRX_FWDG_ORD_ORGNO") or ""),
                                     ord_tmd=str(out.get("ORD_TMD") or ""))
            ledger.on_result(key, "ack", 0)
            return {"ok": True, "act": "ack", "key": key, "odno": odno}
        if act == kis.ACT_RETRY and attempt == 0:
            time.sleep(1.2)                       # R2: 짧은 백오프 1회(61초 금지)
            continue
        if act == kis.ACT_RETRY:                  # 재시도도 유량 거부 — 명시 거부
            ledger.on_result(key, "rejected", 0)
            return {"ok": False, "act": "rate_limited", "key": key,
                    "why": "EGW00201 지속 — P0(집행 불능) 대상"}
        if act in (kis.ACT_REJECT, kis.ACT_AUTH_FATAL):
            ledger.on_result(key, "rejected", 0)
            return {"ok": False, "act": act, "key": key,
                    "why": str((d or {}).get("msg_cd") or http)}
        # UNKNOWN — 종목 잠금, kis_reconcile로만 해소(재주문 절대 금지)
        ledger.on_result(key, "unknown", 0)
        return {"ok": False, "act": "unknown", "key": key,
                "why": f"http={http} — 잠금·대사 필요"}
    ledger.on_result(key, "unknown", 0)
    return {"ok": False, "act": "unknown", "key": key, "why": "재시도 소진"}


def place_sell(key: str, symbol: str, qty: int, price: float, **kw) -> dict:
    """매도 전용 래퍼 — 파수꾼은 이것만 import(매수 경로 차단 원칙 유지)."""
    return place_order(key, symbol, "SELL", qty, price, **kw)


def place_buy(key: str, symbol: str, qty: int, price: float, **kw) -> dict:
    """매수 래퍼 — Stage 2 매수 실행기(X1) 전용. 파수꾼에서 import 금지."""
    return place_order(key, symbol, "BUY", qty, price, **kw)


def cancel_order(key: str, symbol: str, odno: str, qty: int,
                 *, excg: str = "NASD") -> dict:
    """주문 취소(모의 전용). 취소 응답 유실=원주문 생사 불명 → unknown 잠금(§16).
    key: 취소 자체의 원장 키(원주문 키와 별개, 예 '{orig_key}:cxl')."""
    ok, why = orders_allowed()
    if not ok:
        return {"ok": False, "act": "blocked", "key": key, "why": why}
    if not odno:
        return {"ok": False, "act": "blocked", "key": key, "why": "ODNO 없음"}
    if not _LIMITER.acquire("order", timeout=5.0):
        return {"ok": False, "act": "rate_limited", "key": key,
                "why": "order-plane 유량 슬롯 없음(5s)"}
    acct = kis.account()
    body = {**acct, "OVRS_EXCG_CD": excg, "PDNO": symbol,
            "ORGN_ODNO": str(odno), "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0"}
    ledger.record_submit(key, symbol, 0, "취소",
                         meta={"side": "CANCEL", "orgn_odno": str(odno)})
    d, http = _post(_CANCEL_PATH, kis.tr_id("rvsecncl", market="US"), body)
    act = kis.classify_error((d or {}).get("rt_cd"), (d or {}).get("msg_cd"),
                             http, is_order=True)
    if act == kis.ACT_OK:
        out = (d or {}).get("output") or {}
        ledger.bind_broker_order(key, str(out.get("ODNO") or ""),
                                 ord_tmd=str(out.get("ORD_TMD") or ""))
        ledger.on_result(key, "filled", 0)        # 취소 접수 완료(주문 아님)
        return {"ok": True, "act": "canceled", "key": key}
    if act in (kis.ACT_REJECT, kis.ACT_AUTH_FATAL, kis.ACT_RETRY):
        ledger.on_result(key, "rejected", 0)
        return {"ok": False, "act": act, "key": key,
                "why": str((d or {}).get("msg_cd") or http)}
    # UNKNOWN — 원주문이 살았는지 모름 → 종목 잠금 후 nccs 재조회로만 판단
    ledger.on_result(key, "unknown", 0)
    return {"ok": False, "act": "unknown", "key": key,
            "why": f"취소 응답 유실(http={http}) — 원주문 재조회 필요"}
