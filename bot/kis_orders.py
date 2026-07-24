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
# 시장별 주문/취소 엔드포인트 — 국내(order-cash)와 해외(order)는 경로·바디 구조가
#   완전히 다르다(국내: PDNO 6자리·ORD_DVSN·매수매도는 TR_ID로만 구분 / 해외:
#   OVRS_EXCG_CD·SLL_TYPE). 심볼로 시장을 가른다(kis.market_of_symbol).
_PATHS = {
    "US": {"order":  "/uapi/overseas-stock/v1/trading/order",
           "cancel": "/uapi/overseas-stock/v1/trading/order-rvsecncl"},
    "KR": {"order":  "/uapi/domestic-stock/v1/trading/order-cash",
           "cancel": "/uapi/domestic-stock/v1/trading/order-rvsecncl"},
}

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


def _kr_tick(price: float) -> int:
    """국내 호가단위(원) — 2023-01 KRX 개정표. 정확 밴드는 [대조필요]지만 이 값이
    호가 유효성의 하한. 지정가는 호가단위에 맞아야 거부되지 않는다."""
    p = float(price)
    if p < 2_000:        return 1
    if p < 5_000:        return 5
    if p < 20_000:       return 10
    if p < 50_000:       return 50
    if p < 200_000:      return 100
    if p < 500_000:      return 500
    return 1_000


def marketable_limit_price(last_price: float, side: str,
                           slippage_bps: int = 30, *,
                           market: str = "US") -> float:
    """연속장 시장가 부재 → 즉시 체결 지향 지정가.
    SELL: 현재가보다 낮게(팔리게), BUY: 높게(사지게). 기본 30bp.
    · US: 2자리 반올림(1달러 미만 4자리 규칙은 [대조필요]).
    · KR: 원단위·호가단위 정렬(BUY는 위 tick, SELL은 아래 tick)로 유효성 확보."""
    px = float(last_price)
    adj = px * (slippage_bps / 10_000.0)
    out = px - adj if side.upper() == "SELL" else px + adj
    if market == "KR":
        tick = _kr_tick(px)
        if side.upper() == "SELL":
            return float(max(tick, (int(out) // tick) * tick))          # 아래로 정렬
        return float(((int(out) + tick - 1) // tick) * tick)            # 위로 정렬
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


def _order_body(market: str, acct: dict, symbol: str, side: str,
                qty: int, price: float, excg: str, key: str,
                order_type: str) -> dict:
    """시장별 주문 바디. 국내(order-cash)와 해외는 필드가 완전히 다르다 — [대조필요].
    order_type: 'limit'(지정가 00) | 'market'(국내 시장가 01·ORD_UNPR=0)."""
    if market == "KR":
        # 국내: 매수/매도는 TR_ID로 구분(바디는 동일)·PDNO 6자리.
        #   시장가(01)는 단가 0. 지정가(00)는 원단위 정수.
        is_mkt = order_type == "market"
        return {**acct, "PDNO": symbol, "ORD_DVSN": "01" if is_mkt else "00",
                "ORD_QTY": str(qty),
                "ORD_UNPR": "0" if is_mkt else str(int(round(float(price))))}
    # 미국(해외): 연속장 시장가 없음 — 항상 지정가(호출부가 마켓터블 지정가 계산).
    return {**acct, "OVRS_EXCG_CD": excg, "PDNO": symbol,
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
            "SLL_TYPE": "00" if side == "SELL" else "",
            "CTAC_TLNO": "", "MGCO_APTM_ODNO": _mgco_tag(key)}


def place_order(key: str, symbol: str, side: str, qty: int, price: float,
                *, excg: str = "NASD", reason: str = "",
                min_interval_s: float = 60.0, market: str | None = None,
                order_type: str = "limit",
                hldg_before: int | None = None,
                order_meta: dict | None = None) -> dict:
    """주문 1건 전송(모의 전용). 반환 {ok, act, key, odno?, orgno?, why?}.

    key: 포지션 정체성 멱등키(호출부 소유, 예 '{pos}#{n}'). 같은 키 재호출 금지 —
         잔여 재주문은 새 키(#n+1)로. side: 'BUY'|'SELL'. qty: whole-share 정수.
    market: None이면 심볼로 자동 판별(국내 6자리 숫자=KR, 그 외=US).
    order_type: 'limit'(기본) | 'market'. **시장가는 국내(KR)만** — 미국주는 연속장
         시장가가 없어 요청 시 차단(fail-closed). 국내 손절 체결 보장용(사용자 정책 2026-07-13).
    hldg_before: 주문 직전 보유수량(호출부가 아는 브로커-진실 값). 원장 meta에 남겨
         ack(접수)→체결 확정의 잔고-delta 대사 기준으로 쓴다(없으면 자동확정 불가).
    """
    side = side.upper()
    market = market or kis.market_of_symbol(symbol)
    ok, why = orders_allowed()
    if not ok:
        return {"ok": False, "act": "blocked", "key": key, "why": why}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "act": "blocked", "key": key, "why": f"side={side}"}
    if order_type not in ("limit", "market"):
        return {"ok": False, "act": "blocked", "key": key,
                "why": f"order_type={order_type}"}
    if order_type == "market" and market != "KR":
        # 미국 해외주식은 연속장 시장가 부재 → 마켓터블 지정가로만(chase가 보완).
        return {"ok": False, "act": "blocked", "key": key,
                "why": "시장가는 국내(KR)만 — 미국주는 마켓터블 지정가 사용"}
    qty = int(qty)
    if qty < 1 or float(price) < 0:
        return {"ok": False, "act": "blocked", "key": key, "why": "qty/price 무효"}
    if order_type == "limit" and float(price) <= 0:
        return {"ok": False, "act": "blocked", "key": key, "why": "지정가 0 이하"}
    # exclude_key=key: 호출부(파수꾼)가 이 키를 이미 선기록했을 수 있으므로 자기
    #   주문을 in-flight로 오인해 스스로를 막지 않게 한다(자기 차단 버그 수정).
    if not ledger.can_submit(symbol, min_interval_s=min_interval_s,
                             exclude_key=key):
        return {"ok": False, "act": "blocked", "key": key,
                "why": "원장 게이트(잠금/in-flight/간격)"}
    if not _LIMITER.acquire("order", timeout=5.0):
        return {"ok": False, "act": "rate_limited", "key": key,
                "why": "order-plane 유량 슬롯 없음(5s)"}

    acct = kis.account()
    tr = kis.tr_id("buy" if side == "BUY" else "sell", market=market)
    body = _order_body(market, acct, symbol, side, qty, price, excg, key, order_type)
    venue = excg if market == "US" else "KRX"

    # 원장 선기록(전송 전 — 크래시 대비) + 합성키(타임아웃 대사 근거)
    meta = {"side": side, "price": float(price), "excg": excg, "market": market,
            "order_type": order_type, "env": kis.ENV}
    allowed_meta = {"pos_key", "sleeve", "fx", "ccy", "stop", "target",
                    "name", "opened", "tactic", "pending", "parent_key",
                    "chase", "ref_price"}
    for mk, mv in (order_meta or {}).items():
        if mk in allowed_meta and mv is not None:
            meta[mk] = mv
    if hldg_before is not None:                   # ack→체결 잔고대사 기준(있을 때만)
        meta["hldg_before"] = int(hldg_before)
    ledger.record_submit(key, symbol, qty, reason, meta=meta)
    ledger.record_synthetic(key, ledger.synthetic_key(
        acct["CANO"], venue, symbol, side, qty, price,
        time.strftime("%H%M%S")))

    for attempt in (0, 1):
        d, http = _post(_PATHS[market]["order"], tr, body)
        act = kis.classify_error((d or {}).get("rt_cd"), (d or {}).get("msg_cd"),
                                 http, is_order=True)
        if act == kis.ACT_OK:
            out = (d or {}).get("output") or {}
            odno = str(out.get("ODNO") or out.get("odno") or "")
            orgno = str(out.get("KRX_FWDG_ORD_ORGNO") or "")
            ledger.bind_broker_order(key, odno, orgno=orgno,
                                     ord_tmd=str(out.get("ORD_TMD") or ""))
            ledger.on_result(key, "ack", 0)
            return {"ok": True, "act": "ack", "key": key, "odno": odno,
                    "orgno": orgno}
        if act == kis.ACT_RETRY and attempt == 0:
            time.sleep(1.2)                       # R2: 짧은 백오프 1회(61초 금지)
            continue
        if act == kis.ACT_RETRY:                  # 재시도도 유량 거부 — 명시 거부
            ledger.on_result(key, "rejected", 0)
            return {"ok": False, "act": "rate_limited", "key": key,
                    "why": "EGW00201 지속 — P0(집행 불능) 대상"}
        if act == kis.ACT_REFRESH and attempt == 0:
            # 401 — 토큰 만료/회전. 강제 재발급 후 1회 재시도(감사 수정: 기존엔
            #   ACT_REFRESH 분기가 없어 UNKNOWN 잠금으로 빠져 손절이 wedge됐다).
            kis._token(force=True)
            continue
        if act == kis.ACT_REFRESH:                # 재발급 후에도 401 = 확정 거부
            ledger.on_result(key, "rejected", 0)  # 401은 접수 전 거부 → 주문 미실행
            return {"ok": False, "act": "auth_fatal", "key": key,
                    "why": "401 지속(토큰 재발급 후에도) — 인증 확인 필요"}
        if act in (kis.ACT_REJECT, kis.ACT_AUTH_FATAL):
            ledger.on_result(key, "rejected", 0)
            mc = (d or {}).get("msg_cd") or http
            m1 = str((d or {}).get("msg1") or "").strip()
            return {"ok": False, "act": act, "key": key,
                    "why": f"{mc}: {m1}" if m1 else str(mc)}
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
                 *, excg: str = "NASD", orgno: str = "",
                 market: str | None = None) -> dict:
    """주문 취소(모의 전용). 취소 응답 유실=원주문 생사 불명 → unknown 잠금(§16).
    key: 취소 자체의 원장 키(원주문 키와 별개, 예 '{orig_key}:cxl').
    orgno: 국내 취소는 KRX_FWDG_ORD_ORGNO(원주문 응답의 orgno) 필수 — place 응답에서 받아 전달."""
    market = market or kis.market_of_symbol(symbol)
    ok, why = orders_allowed()
    if not ok:
        return {"ok": False, "act": "blocked", "key": key, "why": why}
    if not odno:
        return {"ok": False, "act": "blocked", "key": key, "why": "ODNO 없음"}
    if not _LIMITER.acquire("order", timeout=5.0):
        return {"ok": False, "act": "rate_limited", "key": key,
                "why": "order-plane 유량 슬롯 없음(5s)"}
    acct = kis.account()
    if market == "KR":
        body = {**acct, "KRX_FWDG_ORD_ORGNO": str(orgno), "ORGN_ODNO": str(odno),
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"}
    else:
        body = {**acct, "OVRS_EXCG_CD": excg, "PDNO": symbol,
                "ORGN_ODNO": str(odno), "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": "0",
                "ORD_SVR_DVSN_CD": "0"}
    ledger.record_submit(key, symbol, 0, "취소",
                         meta={"side": "CANCEL", "orgn_odno": str(odno),
                               "market": market})
    d, http = _post(_PATHS[market]["cancel"], kis.tr_id("rvsecncl", market=market),
                    body)
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
