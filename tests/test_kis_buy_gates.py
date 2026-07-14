"""X1 매수 실행기 게이트 체인 + I6/I7/IS2/IS5/costbook 검증(모킹 — 전송 없음).

  kill(I6):    latch(하향 ack 필수)·allows 매핑·env 상향
  rollout(I7): allowlist 필수·세션 게이트·하루 한도·risk cap
  ownership:   baseline 미캡처=전거부·denylist 병합(불축소)·claim>broker 동결
  costbook:    add/close lot·open_cost·부분청산 비례차감·totals
  X1 체인:     게이트가 순서대로 막는지 → 전부 열면 place_buy 도달(ack)

실행: python -m tests.test_kis_buy_gates
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup(tmp):
    for k in list(os.environ):
        if k.startswith(("KIS_", "KILL_", "ALLOW", "ALLOWED", "TRADE_STAGE",
                         "BOT_SEED", "USER_BASELINE", "SYMBOL_FREEZE",
                         "SENTINEL_HEARTBEAT", "COSTBOOK")):
            del os.environ[k]
    os.environ.update({
        "KIS_ENV": "mock", "KIS_MOCK_APPKEY": "k", "KIS_MOCK_APPSECRET": "s",
        "KIS_MOCK_CANO": "50001234", "KIS_ORDERS_ENABLED": "1",
        "KIS_TOKEN_CACHE": os.path.join(tmp, "tok.json"),
        "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
        "USER_BASELINE_PATH": os.path.join(tmp, "base.json"),
        "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
        "SENTINEL_HEARTBEAT_PATH": os.path.join(tmp, "hb.json"),
        "COSTBOOK_PATH": os.path.join(tmp, "cost.jsonl"),
        "BOT_SEED_KRW": "10000000", "TRADE_STAGE": "1.5",
        "ALLOWED_SYMBOLS": "AAPL", "ALLOW_BUY": "1",
        "KIS_MOCK_BUYING_POWER": "50000",      # USD (모의 psamount 미지원 대체)
    })
    mods = {}
    for name in ("kis", "ledger", "kill", "rollout", "ownership", "costbook",
                 "envelope", "heartbeat", "kis_boot", "kis_orders", "kis_buy"):
        m = importlib.import_module(f"bot.{name}")
        importlib.reload(m)
        mods[name] = m
    mods["ledger"].LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")
    return mods


def test_kill_latch():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        K = M["kill"]
        assert K.level() == 0 and K.allows("buy_new")
        K.raise_level(2, "test", "사고")
        assert K.level() == 2
        assert not K.allows("buy_new") and not K.allows("buy_add")
        assert K.allows("manage") and K.allows("protect_sell")
        assert K.raise_level(1, "test", "하향 시도") == 2       # 래치 — 무시
        try:
            K.lower_level(0, ack="")
            raise AssertionError("빈 ack가 통과됨")
        except PermissionError:
            pass
        assert K.lower_level(0, ack="operator: 원인 제거 확인") == 0
        K.raise_level(4, "test", "전면 중지")
        assert not K.allows("protect_sell")                      # L4: 자동 전부 중지
    print("[PASS] kill: latch·ack 하향·allows 매핑(L2 신규금지/L4 전면)")


def test_rollout_gates():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        R = M["rollout"]
        ok, why = R.check_new_entry("AAPL", open_positions=0, risk_pct=0.001,
                                    session_open=True)
        assert ok, why
        assert not R.check_new_entry("TSLA", open_positions=0, risk_pct=0.001,
                                     session_open=True)[0]        # allowlist 밖
        os.environ.pop("ALLOWED_SYMBOLS")
        assert not R.check_new_entry("AAPL", open_positions=0, risk_pct=0.001,
                                     session_open=True)[0]        # allowlist 필수
        os.environ["ALLOWED_SYMBOLS"] = "AAPL"
        assert not R.check_new_entry("AAPL", open_positions=1, risk_pct=0.001,
                                     session_open=True)[0]        # 동시 1종목
        assert not R.check_new_entry("AAPL", open_positions=0, risk_pct=0.002,
                                     session_open=True)[0]        # risk cap 0.1%
        assert not R.check_new_entry("AAPL", open_positions=0, risk_pct=0.001,
                                     session_open=False)[0]       # 세션 게이트
        # 하루 한도: 원장에 오늘 BUY submit 1건 있으면 거부
        M["ledger"].record_submit("d#1", "AAPL", 1, "x", meta={"side": "BUY"})
        assert R.new_entries_today() == 1
        assert not R.check_new_entry("AAPL", open_positions=0, risk_pct=0.001,
                                     session_open=True)[0]
    print("[PASS] rollout: allowlist 필수·캡·risk·세션·하루 한도")


def test_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        O = M["ownership"]
        assert O.buy_denied("AAPL")[0]                 # baseline 미캡처=전 거부
        assert O.capture_baseline(None) is False       # 조회 실패로 캡처 금지
        assert O.capture_baseline([{"ovrs_pdno": "TSLA"}])
        assert O.buy_denied("TSLA")[0]                 # 사용자 기보유 영구 거부
        assert not O.buy_denied("AAPL")[0]
        assert O.capture_baseline([])                  # 빈 재캡처 —
        assert O.buy_denied("TSLA")[0]                 # denylist 안 줄어듦(불축소)
        issues = O.reconcile_claims({"AAPL": 5}, [{"ovrs_pdno": "AAPL",
                                                   "ovrs_cblc_qty": "3"}])
        assert issues and issues[0]["issue"] == "claim>broker"
        assert O.is_frozen("AAPL") and O.buy_denied("AAPL")[0]   # 동결=매수 금지
        O.unfreeze("AAPL", ack="op: 수동 확인")
        assert not O.is_frozen("AAPL")
        assert O.sell_cap("AAPL", 5, 3) == 3           # min(claim, sellable)
    print("[PASS] ownership: fail-closed·불축소·claim>broker 동결·sell_cap")


def test_costbook():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        C = M["costbook"]
        cost = C.add_lot("p1", "AAPL", 10, 100.0, fx=1400.0, commission_krw=500)
        assert cost == 10 * 100 * 1400 + 500
        assert C.open_cost_total() == cost and C.open_qty("AAPL") == 10
        C.close_lot("p1", 4, proceeds_krw=560_000)     # 부분 청산 40%
        left = C.open_cost_symbol("AAPL")
        assert abs(left - cost * 0.6) < 1e-6 and C.open_qty("AAPL") == 6
        t = C.totals()
        assert t["buy_cost"] == cost and t["sell_proceeds"] == 560_000
    print("[PASS] costbook: fx 고정 원가·부분청산 비례차감·totals")


def _ready_all_gates(M, tmp):
    """모든 게이트를 '열림'으로 세팅."""
    M["heartbeat"].write()                             # SLA ok
    M["ownership"].capture_baseline([])                # 깨끗한 계좌 arming
    M["kis_boot"]._STATE["done"] = True                # 부팅 대사 완료로 마킹


def test_x1_gate_chain_then_sent():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        # Stage 1.5 risk cap(0.1%)에 맞춘 호출 — 기본 1%면 rollout이 막는 게 정상
        kw = dict(price_usd=100.0, per_share_risk_usd=5.0, krw_per_usd=1400.0,
                  risk_pct=0.001)
        # 게이트가 순서대로 막는다
        os.environ["ALLOW_BUY"] = "0"
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "env"
        os.environ["ALLOW_BUY"] = "1"
        M["kill"].raise_level(1, "t", "x")
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "kill"
        M["kill"].lower_level(0, ack="op: 테스트")
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "boot"   # 대사 전
        M["kis_boot"]._STATE["done"] = True
        # 세션 닫힘을 명시 모킹(실제 미장 개장 중에도 결정론적으로 rollout에서 멈춤)
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=False):
            d = X.execute_entry("p#1", "AAPL", **kw)
        assert d.gate == "rollout" and "정규장" in d.why
        # 세션만 열린 것으로 모킹 → ownership(미캡처) → 캡처 → sizing/sent
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True):
            d2 = X.execute_entry("p#1", "AAPL", **kw)
            assert d2.gate == "ownership"                            # baseline 미캡처
            _ready_all_gates(M, tmp)
            fake = {"ok": True, "act": "ack", "key": "p#1", "odno": "0001"}
            with mock.patch("bot.kis_orders.place_buy", return_value=fake) as pb:
                d3 = X.execute_entry("p#1", "AAPL", **kw)
            assert d3.ok and d3.gate == "sent", (d3.gate, d3.why)
            # 사이징: SEED 1천만·risk0.1%(스테이지 캡 반영은 rollout, 여기선
            # risk_pct 기본 1%가 아니라 호출부가 낮춰 넘겨야 함 — 기본 호출은
            # rollout의 risk cap과 별개로 envelope이 계산)
            qty_sent = pb.call_args.args[2]
            assert qty_sent == d3.qty > 0
        # feasibility 미확인(None) → 수량 0 차단(보수적)
        os.environ.pop("KIS_MOCK_BUYING_POWER")
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True):
            d5 = X.execute_entry("p#3", "AAPL", **kw)
        assert d5.gate == "sizing" and "feasibility" in d5.why, (d5.gate, d5.why)
    print("[PASS] X1: env→kill→boot→rollout→ownership→(sizing)→sent 체인")


def main():
    test_kill_latch()
    test_rollout_gates()
    test_ownership()
    test_costbook()
    test_x1_gate_chain_then_sent()
    print("\n모든 게이트 체인 테스트 통과 — I6/I7/IS2/IS5/costbook/X1.")


if __name__ == "__main__":
    main()
