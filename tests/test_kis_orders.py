"""KIS 주문 primitive 검증(모킹 — 실제 전송 없음) — 게이트·원장 결속·UNKNOWN.

  1) 게이트: live 하드블록 / KIS_ORDERS_ENABLED 미설정 차단
  2) 성공: rt_cd=0 → ODNO/ORGNO/ORD_TMD 원장 결속 + ack(in-flight)
  3) EGW00201: 1회 백오프 재시도 → 성공 / 지속 시 rejected(rate_limited, P0 대상)
  4) 타임아웃: unknown → 종목 잠금(재주문 차단) — can_submit도 거부
  5) 확정 거부(rt_cd≠0): rejected — 잠금 없음
  6) 원장 게이트: in-flight 중 재주문 차단(동일종목 1건)
  7) 취소: 성공 / 응답유실 unknown → 잠금
  8) marketable_limit_price 방향·반올림

실행: python -m tests.test_kis_orders
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup(tmp, orders_enabled="1", env="mock"):
    """환경 초기화 + 모듈 리로드(원장 경로 격리·토큰 캐시 무효화)."""
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]
    os.environ["KIS_ENV"] = env
    pfx = "MOCK" if env != "live" else "LIVE"
    os.environ[f"KIS_{pfx}_APPKEY"] = "k_test"
    os.environ[f"KIS_{pfx}_APPSECRET"] = "s_test"
    os.environ[f"KIS_{pfx}_CANO"] = "50001234"
    os.environ["KIS_TOKEN_CACHE"] = os.path.join(tmp, "tok.json")
    if orders_enabled:
        os.environ["KIS_ORDERS_ENABLED"] = orders_enabled
    import bot.kis as kis
    import bot.ledger as L
    import bot.kis_orders as KO
    importlib.reload(kis)
    importlib.reload(L)
    importlib.reload(KO)
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    return kis, L, KO


def _fake_urlopen(script):
    """script: 호출 순서대로 응답을 내는 리스트. 'token'은 자동 처리.
    각 항목: dict(정상 JSON) | ('http', code, body_dict) | 'timeout'"""
    state = {"i": 0}

    def fake(req, timeout=None):
        url = req.full_url
        if "/oauth2/tokenP" in url:
            return _Resp(json.dumps({"access_token": "tok",
                                     "expires_in": 86400}).encode())
        step = script[state["i"]]
        state["i"] += 1
        if step == "timeout":
            raise TimeoutError("simulated")
        if isinstance(step, tuple) and step[0] == "http":
            _, code, body = step
            raise urllib.error.HTTPError(
                url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return _Resp(json.dumps(step).encode())
    return fake


_OK_ORDER = {"rt_cd": "0", "msg_cd": "APBK0013",
             "output": {"ODNO": "0001569157", "KRX_FWDG_ORD_ORGNO": "06010",
                        "ORD_TMD": "142233"}}
_RATE = ("http", 500, {"rt_cd": "1", "msg_cd": "EGW00201",
                       "msg1": "초당 거래건수를 초과하였습니다."})
_REJ = {"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "주문가능수량 초과"}


def test_gates():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp, orders_enabled=None)      # 플래그 없음
        r = KO.place_sell("g1#1", "AAPL", 1, 100.0)
        assert r["act"] == "blocked" and "KIS_ORDERS_ENABLED" in r["why"]
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp, orders_enabled="1", env="live")  # live 하드블록
        r = KO.place_sell("g2#1", "AAPL", 1, 100.0)
        assert r["act"] == "blocked" and "하드블록" in r["why"]
    print("[PASS] 게이트: 플래그 미설정·live 하드블록 차단")


def test_success_binds_odno():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch("urllib.request.urlopen", _fake_urlopen([_OK_ORDER])):
            r = KO.place_sell("p1#1", "AAPL", 3, 99.50, reason="손절")
        assert r["ok"] and r["act"] == "ack" and r["odno"] == "0001569157"
        st = L.state_of("p1#1")
        assert st["state"] == "ack" and st["odno"] == "0001569157"
        assert st["ord_tmd"] == "142233" and st["side"] == "SELL"
        assert not L.is_locked("AAPL")               # ack는 잠금 아님(in-flight)
        assert L.open_order_count("AAPL") == 1       # 동시주문은 차단됨
    print("[PASS] 성공: ODNO/ORD_TMD 결속·ack·in-flight 1건")


def test_rate_limited_retry_then_ok():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch.object(KO._LIMITER, "acquire",
                               return_value=True) as acquire, \
             mock.patch("urllib.request.urlopen",
                        _fake_urlopen([_RATE, _OK_ORDER])), \
             mock.patch("time.sleep"):                # 백오프 즉시 통과
            r = KO.place_sell("p2#1", "TSLA", 1, 200.0)
        assert r["ok"] and r["act"] == "ack"
        assert acquire.call_count == 2
    print("[PASS] EGW00201 → 재시도 HTTP마다 슬롯 1개(총2) → 성공")


def test_rate_limited_persistent_p0():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch("urllib.request.urlopen",
                        _fake_urlopen([_RATE, _RATE])), \
             mock.patch("time.sleep"):
            r = KO.place_sell("p3#1", "TSLA", 1, 200.0)
        assert not r["ok"] and r["act"] == "rate_limited"
        assert L.state_of("p3#1")["state"] == "rejected"   # 명시 거부 — unknown 아님
        assert not L.is_locked("TSLA")
    print("[PASS] EGW00201 지속 → rejected(rate_limited, P0 대상)·잠금 없음")


def test_timeout_locks_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch("urllib.request.urlopen", _fake_urlopen(["timeout"])):
            r = KO.place_sell("p4#1", "NVDA", 2, 500.0)
        assert r["act"] == "unknown" and L.is_locked("NVDA")
        # 잠금 중 재주문은 원장 게이트가 차단
        r2 = KO.place_sell("p4#2", "NVDA", 2, 499.0)
        assert r2["act"] == "blocked" and "원장 게이트" in r2["why"]
    print("[PASS] 타임아웃 → unknown·종목 잠금 → 재주문 차단")


def test_reject_no_lock():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch("urllib.request.urlopen", _fake_urlopen([_REJ])):
            r = KO.place_sell("p5#1", "AMD", 1, 150.0)
        assert not r["ok"] and r["act"] == "reject"
        assert L.state_of("p5#1")["state"] == "rejected"
        assert not L.is_locked("AMD")
    print("[PASS] 확정 거부(rt_cd≠0) → rejected·잠금 없음")


def test_inflight_blocks_second_order():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        with mock.patch("urllib.request.urlopen", _fake_urlopen([_OK_ORDER])):
            KO.place_sell("p6#1", "META", 1, 480.0)
        r2 = KO.place_sell("p6#2", "META", 1, 479.0)   # ack(in-flight) 중
        assert r2["act"] == "blocked"
    print("[PASS] in-flight 중 동일종목 2번째 주문 차단(R3)")


def test_cancel_paths():
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        ok_cxl = {"rt_cd": "0", "output": {"ODNO": "0009", "ORD_TMD": "142300"}}
        with mock.patch("urllib.request.urlopen", _fake_urlopen([ok_cxl])):
            r = KO.cancel_order("c1", "AAPL", "0001569157", 3)
        assert r["ok"] and r["act"] == "canceled"
        with mock.patch.object(KO, "_post",
                               side_effect=AssertionError("중복 취소 전송")):
            dup = KO.cancel_order("c1", "AAPL", "0001569157", 3)
        assert dup["act"] == "blocked" and "키 재사용" in dup["why"]
        with mock.patch("urllib.request.urlopen", _fake_urlopen(["timeout"])):
            r = KO.cancel_order("c2", "AAPL", "0001569157", 3)
        assert r["act"] == "unknown" and L.is_locked("AAPL")   # 원주문 생사 불명
    print("[PASS] 취소: 키 재사용 차단 / 응답유실 → unknown·잠금(원주문 재조회 필요)")


def test_marketable_price():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        assert KO.marketable_limit_price(100.0, "SELL", 30) == 99.70
        assert KO.marketable_limit_price(100.0, "BUY", 30) == 100.30
        assert KO.marketable_limit_price(0.02, "SELL", 30) >= 0.01  # 바닥
    print("[PASS] marketable_limit_price 방향·반올림·바닥")


def main():
    test_gates()
    test_success_binds_odno()
    test_rate_limited_retry_then_ok()
    test_rate_limited_persistent_p0()
    test_timeout_locks_symbol()
    test_reject_no_lock()
    test_inflight_blocks_second_order()
    test_cancel_paths()
    test_marketable_price()
    print("\n모든 주문 primitive 테스트 통과 — 게이트·결속·UNKNOWN 잠금·취소.")


if __name__ == "__main__":
    main()
