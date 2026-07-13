"""국내(KR) 주식 KIS 배선 검증 — 사용자 지적(2026-07-13): 국장 누락분 복구.

원래 국장·미장 둘 다였는데 KIS 실행 계층이 미국 해외주식만 배선돼 있던 것을
바로잡는다. 이 테스트는 **네트워크 없이**(POST 모킹) 다음을 증명:

  1) 심볼→시장 라우팅(6자리 숫자=KR)
  2) TR_ID 국내 매핑(매수/매도가 order-cash 같은 엔드포인트·TR로만 구분)
  3) 국내 호가단위 정렬 마켓터블 지정가(정수 원)
  4) 국내 매수 주문이 domestic 경로·바디(PDNO·ORD_DVSN·ORD_UNPR, OVRS_EXCG_CD 없음)
  5) 미국 경로 회귀 없음(여전히 overseas·OVRS_EXCG_CD)
  6) 국내 취소 바디(KRX_FWDG_ORD_ORGNO)
  7) 세션 게이트가 시장별(KR=한국 정규장)로 라우팅

실행: python -m tests.test_kis_domestic
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup(tmp, env="mock"):
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]
    os.environ["KIS_ENV"] = env
    pfx = "MOCK" if env != "live" else "LIVE"
    os.environ[f"KIS_{pfx}_APPKEY"] = "k_test"
    os.environ[f"KIS_{pfx}_APPSECRET"] = "s_test"
    os.environ[f"KIS_{pfx}_CANO"] = "50001234"
    os.environ["KIS_TOKEN_CACHE"] = os.path.join(tmp, "tok.json")
    os.environ["KIS_ORDERS_ENABLED"] = "1"
    import bot.kis as kis
    import bot.ledger as L
    import bot.kis_orders as KO
    importlib.reload(kis)
    importlib.reload(L)
    importlib.reload(KO)
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    return kis, L, KO


_OK = {"rt_cd": "0", "msg_cd": "APBK0013",
       "output": {"ODNO": "0001569157", "KRX_FWDG_ORD_ORGNO": "06010",
                  "ORD_TMD": "142233"}}


def _recording_urlopen(captured):
    """POST/GET 요청을 captured에 기록하고 항상 _OK 응답. 토큰은 자동."""
    def fake(req, timeout=None):
        url = req.full_url
        if "/oauth2/tokenP" in url:
            return _Resp(json.dumps({"access_token": "tok",
                                     "expires_in": 86400}).encode())
        body = None
        if getattr(req, "data", None):
            try:
                body = json.loads(req.data.decode())
            except Exception:
                body = None
        captured.append({"url": url, "tr": req.headers.get("Tr_id"),
                         "body": body})
        return _Resp(json.dumps(_OK).encode())
    return fake


def test_market_routing():
    kis, _, _ = _setup(tempfile.mkdtemp())
    assert kis.market_of_symbol("005930") == "KR"     # 삼성전자
    assert kis.market_of_symbol("000660") == "KR"     # SK하이닉스
    assert kis.market_of_symbol("00088K") == "KR"     # 신형우선주(6번째 문자)
    assert kis.market_of_symbol("AAPL") == "US"
    assert kis.market_of_symbol("BRK.B") == "US"
    assert kis.market_of_symbol("12345") == "US"      # 5자리(코드 아님)
    assert kis.market_of_symbol("1234567") == "US"    # 7자리
    assert kis.market_of_ccy("KRW") == "KR"
    assert kis.market_of_ccy("USD") == "US"
    print("[PASS] 심볼/통화 → 시장 라우팅(6자리 숫자=KR)")


def test_tr_id_kr():
    kis, _, _ = _setup(tempfile.mkdtemp())
    assert kis.tr_id("buy", market="KR", env="mock") == "VTTC0802U"
    assert kis.tr_id("sell", market="KR", env="mock") == "VTTC0801U"
    assert kis.tr_id("buy", market="KR", env="live") == "TTTC0802U"
    assert kis.tr_id("sell", market="KR", env="live") == "TTTC0801U"
    assert kis.tr_id("rvsecncl", market="KR", env="mock") == "VTTC0803U"
    # 미국은 그대로(회귀 없음)
    assert kis.tr_id("buy", market="US", env="mock") == "VTTT1002U"
    print("[PASS] 국내 TR_ID 매핑(매수/매도 order-cash·TR 구분)")


def test_kr_tick_marketable():
    _, _, KO = _setup(tempfile.mkdtemp())
    # 호가단위 밴드
    assert KO._kr_tick(1_500) == 1
    assert KO._kr_tick(3_000) == 5
    assert KO._kr_tick(12_000) == 10
    assert KO._kr_tick(35_000) == 50
    assert KO._kr_tick(80_000) == 100
    assert KO._kr_tick(300_000) == 500
    assert KO._kr_tick(700_000) == 1_000
    # 마켓터블: 매수는 위 tick, 매도는 아래 tick, 전부 정수 원
    buy = KO.marketable_limit_price(80_000, "BUY", 30, market="KR")
    sell = KO.marketable_limit_price(80_000, "SELL", 30, market="KR")
    assert buy == float(int(buy)) and buy % 100 == 0 and buy > 80_000
    assert sell == float(int(sell)) and sell % 100 == 0 and sell < 80_000
    # 미국은 2자리 반올림 유지(회귀)
    assert KO.marketable_limit_price(100.0, "SELL", 30) == 99.70
    print("[PASS] 국내 호가단위 정렬 마켓터블 지정가(정수 원)·미국 회귀 없음")


def test_kr_buy_uses_domestic_path_body():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            r = KO.place_buy("kr#1", "005930", 1, 71_000, min_interval_s=0.0)
        assert r["ok"] and r["act"] == "ack"
        post = [c for c in cap if "order-cash" in c["url"]]
        assert post, f"국내 order-cash 경로 미사용: {[c['url'] for c in cap]}"
        b = post[0]["body"]
        assert post[0]["tr"] == "VTTC0802U"
        assert b["PDNO"] == "005930" and b["ORD_DVSN"] == "00"
        assert b["ORD_QTY"] == "1" and b["ORD_UNPR"] == "71000"
        assert "OVRS_EXCG_CD" not in b        # 해외 필드 없음
        assert "SLL_TYPE" not in b
    print("[PASS] 국내 매수 → domestic order-cash 경로·바디(OVRS 없음)")


def test_us_buy_still_overseas():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            r = KO.place_buy("us#1", "AAPL", 1, 190.0, min_interval_s=0.0)
        assert r["ok"]
        post = [c for c in cap if "overseas-stock/v1/trading/order" in c["url"]]
        assert post and post[0]["tr"] == "VTTT1002U"
        assert post[0]["body"]["OVRS_EXCG_CD"] == "NASD"
    print("[PASS] 미국 매수 회귀 없음(overseas·OVRS_EXCG_CD)")


def test_kr_market_sell_body():
    """국내 손절 시장가(ORD_DVSN=01·ORD_UNPR=0) — 사용자 정책(2026-07-13)."""
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            r = KO.place_sell("kr#m1", "005930", 1, 71_000, min_interval_s=0.0,
                              order_type="market")
        assert r["ok"] and r["act"] == "ack"
        b = [c for c in cap if "order-cash" in c["url"]][0]["body"]
        assert b["ORD_DVSN"] == "01" and b["ORD_UNPR"] == "0"
        assert b["PDNO"] == "005930" and b["ORD_QTY"] == "1"
    print("[PASS] 국내 손절 시장가 바디(ORD_DVSN=01·단가 0)")


def test_kr_buy_stays_limit():
    """진입 매수는 시장가 아님 — 지정가(00) 유지(가격 통제)."""
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            KO.place_buy("kr#b1", "005930", 1, 71_000, min_interval_s=0.0)
        b = [c for c in cap if "order-cash" in c["url"]][0]["body"]
        assert b["ORD_DVSN"] == "00" and b["ORD_UNPR"] == "71000"
    print("[PASS] 국내 매수는 지정가 유지(00)")


def test_us_market_blocked():
    """미국주 시장가 요청 → 차단(연속장 시장가 부재, fail-closed)."""
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        r = KO.place_sell("us#m1", "AAPL", 1, 190.0, order_type="market")
        assert r["act"] == "blocked" and "시장가는 국내" in r["why"]
    print("[PASS] 미국주 시장가 차단(마켓터블 지정가만)")


def test_sentinel_kr_uses_market():
    """파수꾼 국내 매도는 order_type='market'로 place_sell 호출."""
    import bot.sentinel as S
    importlib.reload(S)
    br = object.__new__(S._KisBroker)            # __init__(키 검증) 우회
    br.quote = lambda code, ccy: 70_000.0
    with mock.patch("bot.kis_orders.place_sell",
                    return_value={"ok": True, "act": "ack"}) as ps:
        br.place_sell("005930", 2, "손절", "k#1")
        assert ps.call_args.kwargs.get("order_type") == "market"
        assert ps.call_args.kwargs.get("market") == "KR"
    # 미국은 order_type 미지정(지정가)
    with mock.patch("bot.kis_orders.place_sell",
                    return_value={"ok": True, "act": "ack"}) as ps2:
        br.place_sell("AAPL", 2, "손절", "k#2")
        assert ps2.call_args.kwargs.get("order_type") is None
    print("[PASS] 파수꾼: 국내=시장가·미국=지정가 라우팅")


def test_kr_cancel_body():
    with tempfile.TemporaryDirectory() as tmp:
        _, _, KO = _setup(tmp)
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            r = KO.cancel_order("kr#1:cxl", "005930", "0001569157", 1,
                                orgno="06010")
        assert r["ok"] and r["act"] == "canceled"
        post = [c for c in cap if "order-rvsecncl" in c["url"]]
        assert post and post[0]["tr"] == "VTTC0803U"
        b = post[0]["body"]
        assert b["KRX_FWDG_ORD_ORGNO"] == "06010"
        assert b["ORGN_ODNO"] == "0001569157" and b["RVSE_CNCL_DVSN_CD"] == "02"
        assert "OVRS_EXCG_CD" not in b
    print("[PASS] 국내 취소 바디(KRX_FWDG_ORD_ORGNO·domestic 경로)")


def test_no_self_block_after_pre_record():
    """파수꾼이 hldg_before 남기려 place_sell 전에 record_submit해도, place_order가
    자기 주문키를 in-flight로 오인해 스스로를 막지 않아야 한다(자기 차단 버그 회귀)."""
    with tempfile.TemporaryDirectory() as tmp:
        _, L, KO = _setup(tmp)
        # 파수꾼 pre-record(메타에 hldg_before) — send 이전 크래시 대비 기록
        L.record_submit("k#1", "005930", 1, "손절",
                        meta={"side": "SELL", "market": "KR", "hldg_before": 1})
        cap = []
        with mock.patch("urllib.request.urlopen", _recording_urlopen(cap)):
            r = KO.place_sell("k#1", "005930", 1, 71_000, market="KR",
                              order_type="market", min_interval_s=0.0)
        assert r["ok"] and r["act"] == "ack", r        # 차단되지 않고 전송됨
        # 다른 키의 동일종목 주문은 여전히 차단(in-flight 보호 유지)
        r2 = KO.place_sell("k#2", "005930", 1, 71_000, market="KR",
                           order_type="market", min_interval_s=0.0)
        assert r2["act"] == "blocked", r2
    print("[PASS] pre-record 자기 차단 없음 + 타 주문 in-flight 보호 유지")


def test_session_gate_routing():
    import bot.rollout as R
    import bot.settings as cfg
    importlib.reload(cfg)
    importlib.reload(R)
    # 한국장만 열려 있고 미국장은 닫힌 상황을 강제
    with mock.patch.object(cfg, "market_open",
                           side_effect=lambda c: c == "KRW"):
        assert R.session_open_for("KR") is True
        assert R.session_open_for("US") is False
        ok, why = R.check_new_entry("005930", open_positions=0,
                                    risk_pct=0.001, market="KR")
        # allowlist 필수 Stage라 세션은 통과하고 allowlist에서 막혀야 함(세션 사유 아님)
        assert "정규장" not in why, why
    # 미국장·한국장 모두 닫힘 → KR은 '한국 정규장 아님'
    with mock.patch.object(cfg, "market_open", return_value=False):
        ok, why = R.check_new_entry("005930", open_positions=0,
                                    risk_pct=0.001, market="KR")
        assert not ok and "한국" in why and "정규장" in why, why
    print("[PASS] 세션 게이트 시장별 라우팅(KR=한국 정규장)")


def main():
    test_market_routing()
    test_tr_id_kr()
    test_kr_tick_marketable()
    test_kr_buy_uses_domestic_path_body()
    test_us_buy_still_overseas()
    test_kr_market_sell_body()
    test_kr_buy_stays_limit()
    test_us_market_blocked()
    test_sentinel_kr_uses_market()
    test_kr_cancel_body()
    test_no_self_block_after_pre_record()
    test_session_gate_routing()
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]
    print("\n국내(KR) 배선 검증 통과 — 국장·미장 둘 다 실행 경로 확보.")


if __name__ == "__main__":
    main()
