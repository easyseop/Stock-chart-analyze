"""KIS 어댑터(Stage 0/1, 읽기 전용) 검증 — 주문 없음·TR매핑·에러분류·키게이트.

  1) 키 없음 → 어댑터 전면 비활성(빈 결과)
  2) TR_ID 명시 테이블 — 미국 매도 비대칭(실전 TTTT1006U ↔ 모의 VTTT1001U),
     표에 없는 조합은 RuntimeError(접두 치환 금지)
  3) classify_error — HTTP200+rt_cd≠0=실패, EGW00201=재시도, 주문 타임아웃=UNKNOWN
  4) 이 파일에 주문 경로가 없다(Stage 0 안전 — grep 가드)

실행: python -m tests.test_kis
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload(env="mock", key=None, sec=None):
    for k in ("KIS_ENV", "KIS_MOCK_APPKEY", "KIS_MOCK_APPSECRET",
              "KIS_LIVE_APPKEY", "KIS_LIVE_APPSECRET"):
        os.environ.pop(k, None)
    os.environ["KIS_ENV"] = env
    pfx = "MOCK" if env != "live" else "LIVE"
    if key:
        os.environ[f"KIS_{pfx}_APPKEY"] = key
    if sec:
        os.environ[f"KIS_{pfx}_APPSECRET"] = sec
    from bot import kis
    importlib.reload(kis)
    return kis


def test_disabled_without_keys():
    kis = _reload()
    assert kis.enabled() is False
    assert kis.overseas_balance() is None
    assert kis.open_orders() is None
    assert kis.fills() is None
    print("[PASS] 키 없음 → 비활성·None(폴백 유지)")


def test_tr_id_us_sell_asymmetry():
    kis = _reload(env="live")
    # 매수는 접두만 다르지만 매도는 숫자까지 비대칭 — 표가 이를 정확히 반영해야 함
    assert kis.tr_id("buy", env="live") == "TTTT1002U"
    assert kis.tr_id("buy", env="mock") == "VTTT1002U"
    assert kis.tr_id("sell", env="live") == "TTTT1006U"
    assert kis.tr_id("sell", env="mock") == "VTTT1001U"   # ★ 접두치환이면 VTTT1006U(오답)
    assert kis.tr_id("sell", env="mock") != "V" + "TTTT1006U"[1:]
    print("[PASS] 미국 매도 TR 비대칭(mock=VTTT1001U) 정확")


def test_tr_id_missing_raises():
    kis = _reload()
    # KR·US 둘 다 정의됨 → 진짜 미정의 조합(없는 시장·없는 액션)만 raise 검증
    for bad in [("buy", "JP"), ("unknown_action", "US"), ("unknown_action", "KR")]:
        try:
            kis.tr_id(bad[0], market=bad[1])
        except RuntimeError:
            continue
        raise AssertionError(f"미정의 조합 {bad}에 RuntimeError가 안 남")
    print("[PASS] 미정의 (env×시장×액션) → RuntimeError(접두치환 금지)")


def test_classify_error():
    kis = _reload()
    C = kis.classify_error
    # 성공
    assert C("0", "", 200) == kis.ACT_OK
    # HTTP 200인데 rt_cd≠0 → 확정 실패(성공 처리 금지)
    assert C("1", "MCA05918", 200) == kis.ACT_REJECT
    assert C("1", "MCA05918", 200, is_order=True) == kis.ACT_REJECT
    # 레이트리밋(HTTP 500 + EGW00201) → 재시도
    assert C("1", "EGW00201", 500) == kis.ACT_RETRY
    assert C("1", "EGW00201", 500, is_order=True) == kis.ACT_RETRY
    # TR 첫글자 오류 → 재시도 무의미
    assert C("1", "EGW00356", 500) == kis.ACT_REJECT
    # 주문 타임아웃(http=0) → UNKNOWN, 조회는 RETRY
    assert C(None, "", 0, is_order=True) == kis.ACT_UNKNOWN
    assert C(None, "", 0, is_order=False) == kis.ACT_RETRY
    # EGW00201 아닌 5xx: 주문 UNKNOWN / 조회 RETRY
    assert C(None, "", 503, is_order=True) == kis.ACT_UNKNOWN
    assert C(None, "", 503, is_order=False) == kis.ACT_RETRY
    # 401 → 토큰 재발급
    assert C(None, "", 401) == kis.ACT_REFRESH
    print("[PASS] classify_error: rt_cd 1차·EGW00201 재시도·주문 타임아웃 UNKNOWN")


def test_no_order_path_present():
    """Stage 0 안전: 어댑터에 주문 생성 엔드포인트가 없어야 한다(grep 가드)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bot", "kis.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "trading/order" not in src, "Stage 0 어댑터에 주문 경로가 있으면 안 됨"
    print("[PASS] 주문 경로 없음(Stage 0 안전)")


def test_sellable_holdings_never_guesses_total():
    kis = _reload()
    balance = {"rt_cd": "0", "output1": [{
        "ovrs_pdno": "ALK", "ovrs_cblc_qty": "8", "ord_psbl_qty": "3"}]}
    with mock.patch.object(kis, "overseas_balance", return_value=balance):
        assert kis.sellable_holdings("US", excg="NYSE") == {"ALK": 3}
    # 매도가능 필드가 빠졌다고 총보유 8주로 폴백하면 초과매도 위험.
    del balance["output1"][0]["ord_psbl_qty"]
    with mock.patch.object(kis, "overseas_balance", return_value=balance):
        assert kis.sellable_holdings("US", excg="NYSE") is None
    print("[PASS] 주문 직전 수량=ord_psbl_qty, 필드 누락 시 총보유 추측 금지")


def test_get_retry_acquires_each_http_slot():
    kis = _reload(key="k", sec="s")
    state = {"n": 0}

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            body = json.dumps(
                {"rt_cd": "1", "msg_cd": "EGW00201"}).encode()
            raise urllib.error.HTTPError(req.full_url, 500, "rate", {},
                                         io.BytesIO(body))
        return Resp(json.dumps({"rt_cd": "0", "output": {}}).encode())

    with mock.patch.object(kis, "_token", return_value="tok"), \
         mock.patch.object(kis._LIMITER, "acquire",
                           return_value=True) as acquire, \
         mock.patch("urllib.request.urlopen", side_effect=fake), \
         mock.patch("time.sleep"):
        out = kis._get("/test", "TR", {})
    assert out["rt_cd"] == "0" and acquire.call_count == 2
    print("[PASS] GET 재시도도 HTTP마다 공용 유량 슬롯 재획득")


def main():
    test_disabled_without_keys()
    test_tr_id_us_sell_asymmetry()
    test_tr_id_missing_raises()
    test_classify_error()
    test_no_order_path_present()
    test_sellable_holdings_never_guesses_total()
    test_get_retry_acquires_each_http_slot()
    print("\n모든 KIS 어댑터 테스트 통과.")


if __name__ == "__main__":
    main()
