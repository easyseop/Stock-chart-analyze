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
    test_secret_never_logged()
    print("\n✅ 토스 어댑터 전부 통과 — 읽기 전용·키 게이트·폴백·시크릿 비노출.")
    # 원상복구(다른 테스트에 영향 없게)
    for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"):
        os.environ.pop(k, None)
