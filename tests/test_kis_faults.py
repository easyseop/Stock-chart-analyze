"""장애주입 통합 시나리오 — UNKNOWN 전체 루프(주문→타임아웃→대사→잔여 재주문).

리뷰 Q6-1(가장 위험한 델타 1위)의 전체 시퀀스를 한 번에 재현한다:
  ① 손절 매도 전송 → 타임아웃(UNKNOWN) → 종목 잠금
  ② 그 사이 실제론 부분체결(2/5)돼 있었음 — ccnl에서 발견
  ③ reconcile_unknowns → 유일 후보 HIGH → 확정(체결2)·잠금 해제·ODNO 늦결속
  ④ 60초 간격 게이트 확인 후 **잔여 3주만** 새 키(#2)로 재주문 → ack
  ⑤ 포지션 합산 체결 = 2 (초과매도 없음 — 원수량 5 재발주가 아니라 잔여만)

실행: python -m tests.test_kis_faults
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


def _setup(tmp):
    for k in list(os.environ):
        if k.startswith("KIS_"):
            del os.environ[k]
    os.environ.update({"KIS_ENV": "mock", "KIS_MOCK_APPKEY": "k",
                       "KIS_MOCK_APPSECRET": "s", "KIS_MOCK_CANO": "50001234",
                       "KIS_TOKEN_CACHE": os.path.join(tmp, "tok.json"),
                       "KIS_ORDERS_ENABLED": "1"})
    import bot.kis as kis
    import bot.ledger as L
    import bot.kis_orders as KO
    import bot.kis_reconcile as R
    importlib.reload(kis)
    importlib.reload(L)
    importlib.reload(KO)
    importlib.reload(R)
    L.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    return kis, L, KO, R


def main():
    with tempfile.TemporaryDirectory() as tmp:
        kis, L, KO, R = _setup(tmp)

        # ① 손절 매도 5주 — 네트워크 타임아웃(UNKNOWN)
        def fake_timeout(req, timeout=None):
            if "/oauth2/tokenP" in req.full_url:
                return _Resp(json.dumps({"access_token": "t",
                                         "expires_in": 86400}).encode())
            raise TimeoutError("simulated")

        with mock.patch("urllib.request.urlopen", fake_timeout):
            r1 = KO.place_sell("pos#1", "AAPL", 5, 99.0, reason="하드 손절")
        assert r1["act"] == "unknown" and L.is_locked("AAPL")
        print("  [①] 매도 5주 타임아웃 → UNKNOWN·AAPL 잠금")

        # ② 실제로는 부분체결(2/5)돼 있었다 — ccnl에만 존재(nccs 비어있다 가정 X:
        #    잔여 3주가 미체결로 nccs에 있을 수도 있으나, 최악(전부 ccnl)로 재현)
        ccnl = {"rt_cd": "0", "output": [
            {"odno": "7001", "pdno": "AAPL", "ft_ord_qty": "5",
             "ft_ccld_qty": "2", "ft_ccld_unpr3": "99.0",
             "sll_buy_dvsn_cd_name": "매도", "ord_tmd": "150000"}]}

        # ③ 대사 — 유일 후보 HIGH
        res = R.reconcile_unknowns({"rt_cd": "0", "output": []}, ccnl)
        assert len(res) == 1
        rr = res[0]
        assert rr["confidence"] == L.CONF_HIGH and rr["state"] == "partial"
        assert rr["filled"] == 2 and rr["residual"] == 3
        assert not L.is_locked("AAPL") and L.odno_of("pos#1") == "7001"
        print("  [③] 대사 HIGH: 부분체결2 확정·잔여3·잠금해제·ODNO 결속")

        # ④ 60초 간격 게이트가 즉시 재주문을 막는지 → 그 후 잔여만 재주문
        blocked = KO.place_sell("pos#2", "AAPL", 3, 98.7)
        assert blocked["act"] == "blocked", "간격 게이트가 즉시 재주문을 막아야 함"
        ok_order = {"rt_cd": "0", "output": {
            "ODNO": "7002", "KRX_FWDG_ORD_ORGNO": "06010", "ORD_TMD": "150130"}}

        def fake_ok(req, timeout=None):
            if "/oauth2/tokenP" in req.full_url:
                return _Resp(json.dumps({"access_token": "t",
                                         "expires_in": 86400}).encode())
            return _Resp(json.dumps(ok_order).encode())

        residual = rr["residual"]
        with mock.patch("urllib.request.urlopen", fake_ok):
            r2 = KO.place_sell("pos#2", "AAPL", residual, 98.7,
                               reason="잔여 재주문", min_interval_s=0.0)
        assert r2["ok"] and r2["odno"] == "7002"
        print("  [④] 간격 게이트 확인 → 잔여 3주만 새 키(#2)로 ack")

        # ⑤ 포지션 합산 체결 = 2 (원수량 5 재발주 없음 → 초과매도 불가)
        assert L.filled_for("pos") == 2
        total_intended = sum(v["intended"] for k, v in L._fold().items()
                             if k.startswith("pos#"))
        assert total_intended == 5 + 3  # 시도 합계(5 타임아웃분 + 잔여 3)
        print("  [⑤] 포지션 체결 합산 2·재주문은 잔여만 — 초과매도 없음")

    print("\n✅ 장애주입 통합 통과 — UNKNOWN 루프(타임아웃→대사→잔여 재주문) 무결.")


if __name__ == "__main__":
    main()
