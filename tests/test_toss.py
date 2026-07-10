"""토스 시세 어댑터(Stage 0) 검증 — 읽기 전용·키 게이트·폴백 안전.

  1) 키 없음 → 어댑터 전면 비활성(빈 결과) → 호출부 FDR 폴백(현행 유지)
  2) 응답 파싱 — lastPrice(str)→float, null price 종목은 제외, ts/통화 보존
  3) 401 → 토큰 1회 강제 재발급 후 재시도(만료 자가치유)
  4) client_secret이 로그/예외에 새지 않는지(민감정보 비노출)

실행: python -m tests.test_toss
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reload(cid=None, sec=None):
    for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"):
        os.environ.pop(k, None)
    if cid:
        os.environ["TOSS_CLIENT_ID"] = cid
    if sec:
        os.environ["TOSS_CLIENT_SECRET"] = sec
    import importlib
    from bot import toss
    importlib.reload(toss)
    return toss


def test_disabled_without_keys():
    toss = _reload()
    assert toss.enabled() is False
    assert toss.prices(["AAPL", "005930"]) == {}
    assert toss.price("AAPL") is None
    assert toss.quote_price("AAPL") is None
    print("[PASS] 키 없음 → 비활성·빈 결과(폴백 유지)")


def test_parse_and_drop_null():
    toss = _reload("c_x", "s_x")

    def fake(req, timeout=None):
        if "/oauth2/token" in req.full_url:
            return _Resp(json.dumps({"access_token": "tok1",
                                     "expires_in": 86400}).encode())
        assert req.headers.get("Authorization") == "Bearer tok1"
        return _Resp(json.dumps({"result": [
            {"symbol": "AAPL", "timestamp": "2026-07-08T09:30:00+09:00",
             "lastPrice": "212.34", "currency": "USD"},
            {"symbol": "005930", "timestamp": None,
             "lastPrice": "72000", "currency": "KRW"},
            {"symbol": "BAD", "lastPrice": None, "currency": "USD"},
        ]}).encode())

    with mock.patch("urllib.request.urlopen", fake):
        r = toss.prices(["AAPL", "005930", "BAD"])
    assert r["AAPL"]["price"] == 212.34 and r["AAPL"]["currency"] == "USD"
    assert r["005930"]["price"] == 72000.0 and r["005930"]["ts"] is None
    assert "BAD" not in r                 # null lastPrice 제외
    print("[PASS] 파싱·통화·ts 보존, null 가격 종목 제외")


def test_401_forces_refresh():
    toss = _reload("c_x", "s_x")
    toss._TOK = {"tok": None, "exp": 0.0}
    n = {"p": 0}

    def fake(req, timeout=None):
        if "/oauth2/token" in req.full_url:
            return _Resp(json.dumps({"access_token": "tok", "expires_in": 86400}).encode())
        n["p"] += 1
        if n["p"] == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {}, io.BytesIO(b""))
        return _Resp(json.dumps({"result": [
            {"symbol": "AAPL", "lastPrice": "1.0", "currency": "USD", "timestamp": None}]}).encode())

    with mock.patch("urllib.request.urlopen", fake):
        r = toss.prices(["AAPL"])
    assert r.get("AAPL", {}).get("price") == 1.0
    print("[PASS] 401 → 토큰 1회 강제 재발급 후 재시도 성공")


def test_token_failure_cooldown():
    """토큰 발급이 실패하면 쿨다운 동안 재시도하지 않는다(장애 중 15초 타임아웃
    반복으로 손절 감시 주기가 밀리는 회귀 방지 — 감사 CONFIRMED)."""
    toss = _reload("c_x", "s_x")
    toss._TOK = {"tok": None, "exp": 0.0, "fail_until": 0.0}
    n = {"token_calls": 0}

    def fake(req, timeout=None):
        if "/oauth2/token" in req.full_url:
            n["token_calls"] += 1
            raise urllib.error.URLError("token endpoint down")
        raise AssertionError("쿨다운 중엔 /prices까지 가면 안 됨")

    with mock.patch("urllib.request.urlopen", fake):
        assert toss.quote_price("AAPL") is None      # 1회 시도(실패)
        assert toss.quote_price("MSFT") is None      # 쿨다운 → 시도 안 함
        assert toss.quote_price("005930") is None    # 쿨다운 → 시도 안 함
    assert n["token_calls"] == 1, f"쿨다운이 안 먹음(토큰 {n['token_calls']}회 시도)"
    # 쿨다운 만료 후엔 다시 시도
    toss._TOK["fail_until"] = 0.0
    with mock.patch("urllib.request.urlopen", fake):
        assert toss.quote_price("AAPL") is None
    assert n["token_calls"] == 2, "쿨다운 만료 후 재시도해야 함"
    print("[PASS] 토큰 실패 쿨다운 — 장애 중 재시도 폭주 차단, 만료 후 복구")


def test_classify_error():
    """에러 분류 — 특히 주문 경로에서 '불확실=UNKNOWN'(초과매도 방지) 규칙 검증."""
    toss = _reload()
    C = toss.classify_error
    cases = [
        # (status, is_order, expected)
        (200, False, "ok"), (401, False, "refresh_auth"), (403, False, "auth_fatal"),
        (429, False, "retry"), (404, False, "not_found"),
        (400, False, "reject"), (422, False, "reject"),
        # 조회 5xx = 재시도, 주문 5xx/타임아웃 = UNKNOWN(핵심 안전규칙)
        (500, False, "retry"), (500, True, "unknown"), (0, True, "unknown"),
        (408, True, "unknown"),
        # 409: 조회=거부, 주문=중복접수(멱등)
        (409, False, "reject"), (409, True, "duplicate"),
        # 422 주문=비즈니스 거부(재시도 무의미)
        (422, True, "reject"),
        # 미지 status: 주문이면 안전하게 UNKNOWN, 조회면 재시도
        (418, True, "unknown"), (418, False, "retry"),
    ]
    bad = [(s, o, C(s, is_order=o), e) for s, o, e in cases if C(s, is_order=o) != e]
    assert not bad, f"오분류: {bad}"
    print("[PASS] classify_error — 주문 불확실=UNKNOWN, 409=중복, 422=거부 등 정확")


def test_client_order_id():
    """clientOrderId 매핑 — 36자 이하·규격 문자·결정적(멱등)."""
    import re
    toss = _reload()
    key = "toss:acct-12345678901:AAPL:2026-07-10T22:30:00+09:00:seq42:SELL_STOP:v3"
    cid = toss.client_order_id(key)
    assert len(cid) <= 36, f"36자 초과: {len(cid)}"
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", cid), f"규격 외 문자: {cid}"
    assert cid == toss.client_order_id(key), "결정적이지 않음(멱등 깨짐)"
    assert cid != toss.client_order_id(key + ":x"), "다른 키가 같은 값(충돌)"
    print(f"[PASS] client_order_id — 36자 이하·규격·멱등 ({cid})")


def test_secret_never_logged():
    toss = _reload("c_x", "SUPER_SECRET_VALUE")
    toss._TOK = {"tok": None, "exp": 0.0}
    buf = io.StringIO()

    def fake(req, timeout=None):
        raise urllib.error.URLError("boom")

    with mock.patch("urllib.request.urlopen", fake), \
            mock.patch("sys.stdout", buf):
        toss._token(force=True)
        toss.prices(["AAPL"])
    out = buf.getvalue()
    assert "SUPER_SECRET_VALUE" not in out, "시크릿이 로그에 노출됨!"
    print("[PASS] 실패 로그에 client_secret 미노출")


if __name__ == "__main__":
    test_disabled_without_keys()
    test_parse_and_drop_null()
    test_401_forces_refresh()
    test_token_failure_cooldown()
    test_classify_error()
    test_client_order_id()
    test_secret_never_logged()
    print("\n✅ 토스 어댑터 전부 통과 — 읽기 전용·키 게이트·폴백·시크릿 비노출.")
    # 원상복구(다른 테스트에 영향 없게)
    for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"):
        os.environ.pop(k, None)
