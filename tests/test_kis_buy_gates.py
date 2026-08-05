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
        "BOT_SEED_KRW": "10000000",
        "BOT_OPERATING_TOTAL_KRW": "10000000",
        "BOT_OPERATING_BUFFER_PCT": "0",
        "TRADE_STAGE": "1.5",
        "ALLOWED_SYMBOLS": "AAPL", "ALLOW_BUY": "1",
        # 저장소의 실제 allowlist 파일이 테스트에 섞이지 않게 없는 경로로 고정
        "ALLOWED_SYMBOLS_FILE": os.path.join(tmp, "allowlist-absent.txt"),
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
        mt = C.market_totals("US", "A")
        assert mt["buy_cost"] == cost and mt["sell_proceeds"] == 560_000
        snap = C.budget_snapshot()
        assert snap and abs(snap["total"] - left) < 1e-6
        with open(os.environ["COSTBOOK_PATH"], "ab") as fp:
            fp.write(b'{"ev":"add","key":"torn"')
        assert C.budget_snapshot() is None          # 총시드 게이트는 손상 시 fail-closed
    print("[PASS] costbook: durable 원가·부분청산·총시드 스냅샷·손상 fail-closed")


def _ready_all_gates(M, tmp):
    """모든 게이트를 '열림'으로 세팅."""
    M["heartbeat"].write()                             # SLA ok
    M["ownership"].capture_baseline([])                # 깨끗한 계좌 arming
    M["kis_boot"]._STATE["done"] = True                # 부팅 대사 완료로 마킹


def test_execute_entry_rejects_invalid_optional_numeric_args():
    """Codex V6 P2-3: 실행기 잔여 숫자 인자(limit_price·qty_fraction·비용/한도)
    도 bool·NaN·inf·비숫자를 입력 게이트에서 예외 없이 거부(직접 호출 가정)."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ["ALLOW_BUY"] = "1"
        nan = float("nan")
        base = dict(price_usd=100.0, per_share_risk_usd=5.0,
                    krw_per_usd=1400.0, risk_pct=0.001)
        for kw_extra in ({"limit_price": nan}, {"limit_price": float("inf")},
                         {"limit_price": True}, {"limit_price": 0},
                         {"limit_price": -1}, {"limit_price": "x"},
                         {"qty_fraction": nan}, {"qty_fraction": 0},
                         {"qty_fraction": 2}, {"qty_fraction": True},
                         {"seed_krw": True}, {"seed_krw": nan},
                         {"open_cost_krw": nan},
                         {"operating_limit_krw": True},
                         {"risk_pct": nan}):
            d = X.execute_entry("p#opt", "AAPL", **{**base, **kw_extra})
            assert d.gate == "input", (kw_extra, d)
        # order_meta.target 오염 직접 주입(Codex V6 M15) — stop 이하·비숫자·
        # bool·NaN 전부 input, 유효값·None은 통과.
        for bad_tgt in (True, nan, "x", 0, -1, 90.0):   # stop=95 이하 포함
            d = X.execute_entry("p#opt", "AAPL", **base,
                                order_meta={"stop": 95.0, "target": bad_tgt})
            assert d.gate == "input", (bad_tgt, d)
        d = X.execute_entry("p#opt", "AAPL", **base,
                            order_meta={"stop": 95.0, "target": 110.0})
        assert d.gate != "input", d
        # 정상 옵션 인자는 input을 통과해 이후 게이트로 진행(kill에서 멈춤 등).
        d = X.execute_entry("p#opt", "AAPL", **base,
                            limit_price=97.0, qty_fraction=0.5)
        assert d.gate != "input", d
    print("[PASS] 실행기 잔여 숫자 인자·order_meta.target 전부 input 차단")


def test_run_once_end_to_end_side_effects():
    """Codex V6 P2-2: 실행기 mock 없이 신호→검증기→실제 게이트 체인→주문
    primitive(place_buy spy)까지 연결. 차단 입력은 원장·원가장부·보호 포지션·
    kill 상태가 **전부 불변**, 허용 입력은 place_buy 정확히 1회 + 원장 선기록
    1건 + ack 단계 포지션 미기록을 단언한다."""
    import importlib
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        os.environ["ALLOW_BUY"] = "1"
        os.environ["TRADE_STAGE"] = "mirror"       # 직접진입 프로필(risk 1%)
        _ready_all_gates(M, tmp)
        import bot.kis_buyloop as BL
        importlib.reload(BL)
        kpos_path = os.path.join(tmp, "kpos.json")

        def snapshot_state():
            out = {}
            for label, path in (("ledger", M["ledger"].LEDGER_PATH),
                                ("costbook", os.environ["COSTBOOK_PATH"]),
                                ("kpos", kpos_path),
                                ("kill", os.environ["KILL_STATE_PATH"])):
                try:
                    with open(path, "rb") as f:
                        out[label] = f.read()
                except OSError:
                    out[label] = b""
            return out

        def run(signals, **rk):
            # 주문 primitive는 **HTTP 경계(_post)** 에서 spy — place_order의
            # 원장 선기록·ack 처리 등 실제 부작용 경로를 그대로 태운다.
            ok_resp = ({"rt_cd": "0", "msg_cd": "OK",
                        "output": {"ODNO": "0001"}}, 200)
            with mock.patch.object(BL.kis, "positions_detail",
                                   return_value=[]), \
                    mock.patch.object(BL.kis, "last_price",
                                      return_value=100.4), \
                    mock.patch.object(BL.settings, "market_open",
                                      return_value=True), \
                    mock.patch.object(M["rollout"], "us_regular_open",
                                      return_value=True), \
                    mock.patch.object(BL.kis_positions, "PATH", kpos_path), \
                    mock.patch.object(M["kis_orders"], "_post",
                                      return_value=ok_resp) as pb:
                res = BL.run_once(signals, fx=1400.0, **rk)
            return res, pb

        sig = {"group": "now", "id": "e2e-AAPL", "code": "AAPL", "name": "t",
               "ccy": "USD", "entry": 100.0, "stop": 95.0, "fresh": True,
               "stage": 3, "norm": 50, "target": 110.0}
        # ① 차단 입력(검증기: B target ≤ 발주가) — 주문 primitive 0 + 상태 불변.
        before = snapshot_state()
        bad = dict(sig, group="shelf", target=100.2, shelf={"rr": 2.0})
        res, pb = run([bad], sleeve="B", group="shelf")
        assert not pb.called and res[0]["gate"] == "input", res
        assert snapshot_state() == before          # 원장·원가·포지션·kill 불변
        # ② 허용 입력 — place_buy 정확히 1회, 원장 선기록, ack 단계 포지션 0.
        res, pb = run([sig])
        assert pb.call_count == 1, res
        assert res[0]["gate"] == "sent", res
        after = snapshot_state()
        assert after["ledger"] != before["ledger"]       # 선기록 남음
        assert after["kpos"] in (b"", b"{}")             # ack는 체결 아님
        assert after["kill"] == before["kill"]           # kill 무변경
        orders = M["ledger"]._fold()
        assert any(str(v.get("symbol")) == "AAPL" and v.get("side") == "BUY"
                   for v in orders.values())
    print("[PASS] e2e: 차단=부작용 0(파일 불변) · 허용=주문 primitive 1회")


def test_execute_entry_rejects_nonfinite_inputs():
    """Codex P2 이중방어: NaN은 `<=0` 비교를 전부 미끄러진다 — 호출부 검사와
    독립적으로 실행기 자신이 price/risk/fx NaN·inf를 input 게이트에서 차단."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ["ALLOW_BUY"] = "1"
        nan, inf = float("nan"), float("inf")
        base = dict(price_usd=100.0, per_share_risk_usd=5.0,
                    krw_per_usd=1400.0, risk_pct=0.001)
        for field, val in (("price_usd", nan), ("price_usd", inf),
                           ("per_share_risk_usd", nan),
                           ("krw_per_usd", nan)):
            kw = dict(base); kw[field] = val
            d = X.execute_entry("p#nan", "AAPL", **kw)
            assert d.gate == "input", (field, d)
            assert "NaN" in d.why or "무효" in d.why
        # Codex V2 P1 이중방어: order_meta.stop이 제공됐다면 finite·양수여야
        #   한다 — 무효 stop이 원장 메타로 남으면 체결 후 보호 기록이 거부돼
        #   무보호 실보유가 된다.
        for bad_stop in (0, -5.0, nan, -inf, "x"):
            d = X.execute_entry("p#stop", "AAPL", **base,
                                order_meta={"stop": bad_stop})
            assert d.gate == "input", (bad_stop, d)
        # stop 키가 없는 order_meta(호환 경로)·유효 stop은 input에서 막히지 않음.
        d = X.execute_entry("p#meta", "AAPL", **base, order_meta={"name": "t"})
        assert d.gate != "input", d
        d = X.execute_entry("p#ok", "AAPL", **base, order_meta={"stop": 95.0})
        assert d.gate != "input", d
    print("[PASS] 실행기 자체가 NaN·inf price/risk/fx·무효 stop을 input에서 차단")


def test_x1_gate_chain_then_sent():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        # Stage 1.5 risk cap(0.1%)에 맞춘 호출 — 기본 1%면 rollout이 막는 게 정상
        kw = dict(price_usd=100.0, per_share_risk_usd=5.0, krw_per_usd=1400.0,
                  risk_pct=0.001, held_cost_krw=0.0,
                  total_held_cost_krw=0.0)
        # 게이트가 순서대로 막는다
        os.environ["ALLOW_BUY"] = "0"
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "env"
        os.environ["ALLOW_BUY"] = "1"
        M["kill"].raise_level(1, "t", "x")
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "kill"
        M["kill"].lower_level(0, ack="op: 테스트")
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "boot"   # 대사 전
        M["kis_boot"]._STATE["done"] = True
        # 첫 포지션도 heartbeat 없으면 SLA에서 막힌다.
        assert X.execute_entry("p#1", "AAPL", **kw).gate == "sla"
        M["heartbeat"].write()
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
        # feasibility 미확인 → 수량 0 차단(보수적). override를 무효값으로 명시해
        #   present-balance 폴백(네트워크)을 타지 않게 한다(결정론).
        os.environ["KIS_MOCK_BUYING_POWER"] = "x"
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True):
            d5 = X.execute_entry("p#3", "AAPL", **kw)
        assert d5.gate == "sizing" and "feasibility" in d5.why, (d5.gate, d5.why)
        os.environ["KIS_MOCK_BUYING_POWER"] = "50000"
    print("[PASS] X1: env→kill→boot→rollout→ownership→(sizing)→sent 체인")


def test_mirror_stage():
    """mirror(legacy명=scanner-direct) — 동시보유 무제한·하루10건·risk1%·
    **allowlist 필수**(2026-08-05 정정: autopaper가 후보를 좁혀준다는 전제 폐기)."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        R = M["rollout"]
        os.environ["TRADE_STAGE"] = "mirror"
        os.environ.pop("ALLOWED_SYMBOLS", None)
        ok, why = R.check_new_entry("ANYTHING", open_positions=0, risk_pct=0.01,
                                    session_open=True)
        assert not ok and "allowlist" in why               # allowlist 없으면 전 거부
        os.environ["ALLOWED_SYMBOLS"] = "ANYTHING,X," + ",".join(
            f"S{i}" for i in range(10))
        ok, why = R.check_new_entry("ANYTHING", open_positions=0, risk_pct=0.01,
                                    session_open=True)
        assert ok, why
        assert R.profile()["max_positions"] is None
        assert R.check_new_entry("X", open_positions=12, risk_pct=0.01,
                                 session_open=True)[0]
        assert R.check_new_entry("X", open_positions=10_000, risk_pct=0.01,
                                 session_open=True)[0]     # 종목 수만으로 차단하지 않음
        assert not R.check_new_entry("X", open_positions=0, risk_pct=0.02,
                                     session_open=True)[0]  # risk 1% 캡
        # 하루 10건(사용자 지정 2026-07-15): 9건까지 허용, 10건째 기록 후 거부
        for i in range(9):
            M["ledger"].record_submit(f"m#{i}", f"S{i}", 1, "x",
                                      meta={"side": "BUY"})
        assert R.check_new_entry("X", open_positions=0, risk_pct=0.01,
                                 session_open=True)[0]      # 9/10 → 허용
        M["ledger"].record_submit("m#9", "S9", 1, "x", meta={"side": "BUY"})
        assert not R.check_new_entry("X", open_positions=0, risk_pct=0.01,
                                     session_open=True)[0]  # 10/10 → 차단
        # 목록 밖 종목은 거부
        os.environ["ALLOWED_SYMBOLS"] = "AAPL"
        assert not R.check_new_entry("TSLA", open_positions=0, risk_pct=0.01,
                                     session_open=True)[0]
        os.environ["TRADE_STAGE"] = "1.5"
    print("[PASS] mirror: 동시보유 수 무제한·하루10건·risk1%·allowlist 필수")


def test_broker_truth_open_cost_gate():
    """검토 수정 — costbook이 비어도(미배선 #25) 브로커-진실 open_cost_krw가
    총량 게이트(deployable=SEED−open_cost)·불변식을 실제로 물게 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ["TRADE_STAGE"] = "mirror"
        os.environ["ALLOWED_SYMBOLS"] = "AAPL,MSFT,TSLA,NVDA,GAP1,X"
        _ready_all_gates(M, tmp)
        kw = dict(price_usd=100.0, per_share_risk_usd=5.0, krw_per_usd=1400.0,
                  risk_pct=0.01, held_cost_krw=9_850_000,
                  total_held_cost_krw=9_850_000)
        fake = {"ok": True, "act": "ack", "key": "q", "odno": "0001"}
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True), \
             mock.patch("bot.kis_orders.place_buy", return_value=fake):
            # SEED 1천만 중 브로커 실투입 985만 → 남은 15만 < 1주(14만)×2 → 1주만
            d = X.execute_entry(
                "q#1", "AAPL", open_positions=10_000,
                open_cost_krw=9_850_000, **kw)
            assert d.ok and d.qty == 1, (d.gate, d.qty, d.why)
            # 실투입 999만 → 남은 1만 < 1주 가격 → sizing 차단(deployable 바인딩)
            d2 = X.execute_entry(
                "q#2", "TSLA", open_cost_krw=9_990_000,
                **{**kw, "held_cost_krw": 9_990_000,
                   "total_held_cost_krw": 9_990_000})
            assert d2.gate == "sizing" and "deployable" in d2.why, (d2.gate, d2.why)
            # 실투입 > SEED = 설명불가 초과 → 불변식 발동(kill L1 + 차단)
            d3 = X.execute_entry(
                "q#3", "NVDA", open_cost_krw=11_000_000,
                **{**kw, "held_cost_krw": 11_000_000,
                   "total_held_cost_krw": 11_000_000})
            assert d3.gate == "invariant"
            assert M["kill"].level() >= 1
        os.environ["TRADE_STAGE"] = "1.5"
    print("[PASS] 브로커-진실 open_cost → 총량 게이트·불변식 실동작(costbook 공백 보완)")


def test_half_never_promotes_zero_sizing():
    """half 전술이 size_buy=0을 max(1, ...)로 1주 승격시키지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        _ready_all_gates(M, tmp)
        os.environ["KIS_MOCK_BUYING_POWER"] = "x"   # feasibility=None → qty 0
        kw = dict(price_usd=100.0, per_share_risk_usd=5.0,
                  krw_per_usd=1400.0, risk_pct=0.001, qty_fraction=0.5)
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True), \
             mock.patch("bot.kis_orders.place_buy") as place:
            decision = X.execute_entry("half#0", "AAPL", **kw)
        assert decision.gate == "sizing" and decision.qty == 0
        assert not place.called
    print("[PASS] half sizing 0 → 1주 강제 승격 없이 주문 차단")


def test_combined_a_b_total_gate():
    """A/B 각 명목한도 안이어도 합계가 총시드-5% 완충을 넘으면 차단."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ.update({
            "BOT_OPERATING_TOTAL_KRW": "35000000",
            "BOT_OPERATING_BUFFER_PCT": "0.05",
            "BOT_SEED_KRW": "30000000",
            "BOT_SEED_SB_KRW": "5000000",
            "TRADE_STAGE": "mirror",
        })
        os.environ["ALLOWED_SYMBOLS"] = "AAPL,MSFT,TSLA,NVDA,GAP1,X"
        _ready_all_gates(M, tmp)
        kw = dict(price_usd=100.0, per_share_risk_usd=5.0,
                  krw_per_usd=1400.0, risk_pct=0.01,
                  seed_krw=30_000_000, open_cost_krw=29_000_000,
                  total_open_cost_krw=33_500_000,
                  operating_limit_krw=33_250_000)
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True), \
             mock.patch("bot.kis_orders.place_buy") as place:
            decision = X.execute_entry("sum#1", "AAPL", **kw)
        assert decision.gate == "total_invariant"
        assert M["kill"].level() >= 1 and not place.called
    print("[PASS] A 2900만+B 450만 각 명목내·합계 3350만 → 5%완충 한도 차단")


def test_gap_price_reserves_marketable_limit_not_stale_quote():
    """급변 시 현재가가 아니라 실제 발주 상한(마켓터블 지정가)으로 예산 예약."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ["TRADE_STAGE"] = "mirror"
        os.environ["ALLOWED_SYMBOLS"] = "AAPL,MSFT,TSLA,NVDA,GAP1,X"
        _ready_all_gates(M, tmp)
        fake = {"ok": True, "act": "ack", "key": "gap", "odno": "1"}
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True), \
             mock.patch("bot.kis_orders.place_buy", return_value=fake) as place:
            decision = X.execute_entry(
                "gap#1", "AAPL", price_usd=100.0,
                per_share_risk_usd=5.0, krw_per_usd=1400.0,
                risk_pct=0.01, held_cost_krw=0.0,
                total_held_cost_krw=0.0)
        sent_limit = float(place.call_args.args[3])
        meta = place.call_args.kwargs["order_meta"]
        assert sent_limit > 100.0
        assert abs(meta["reservation_cost_krw"]
                   - decision.planned_qty * sent_limit * 1400.0) < 1e-6
        assert meta["reservation_cost_krw"] > decision.planned_qty * 100 * 1400
    print("[PASS] 갭/슬리피지 예산은 stale 현재가 아닌 BUY 지정가 상한으로 예약")


def test_missing_broker_budget_snapshot_blocks_send():
    """호출부가 A/B 브로커 원가를 생략하면 원자 총시드 검증을 우회할 수 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        X = M["kis_buy"]
        os.environ["TRADE_STAGE"] = "mirror"
        os.environ["ALLOWED_SYMBOLS"] = "AAPL,MSFT,TSLA,NVDA,GAP1,X"
        _ready_all_gates(M, tmp)
        with mock.patch.object(M["rollout"], "us_regular_open", return_value=True), \
             mock.patch("bot.kis_orders.place_buy") as place:
            decision = X.execute_entry(
                "budget#missing", "AAPL", price_usd=100.0,
                per_share_risk_usd=5.0, krw_per_usd=1400.0,
                risk_pct=0.01)
        assert decision.gate == "budget" and not place.called
    print("[PASS] 브로커 A/B 원가 스냅샷 없으면 원자 총시드 게이트 fail-closed")


def test_mock_feasibility_fallbacks():
    """모의 매수여력: US는 psamount(실측 지원)·ord_psbl_frcr_amt(cash-only, R6),
    KR은 국내잔고 주문가능현금. 실패 시 원화/fx → SEED 폴백(사이징 0 방지)."""
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        K = M["kis"]
        os.environ.pop("KIS_MOCK_BUYING_POWER", None)
        # KR: 국내잔고 output2의 주문가능현금(D+2)
        with mock.patch.object(K, "_get", return_value={
                "rt_cd": "0", "output1": [],
                "output2": [{"prvs_rcdl_excc_amt": "5000000"}]}):
            assert K.domestic_buying_power("005930", 70000) == 5_000_000
        # US: psamount의 ord_psbl_frcr_amt(외화예수금 기준 USD, 실측 $92742)
        with mock.patch.object(K, "_get", return_value={
                "rt_cd": "0", "output": {"ord_psbl_frcr_amt": "92742.31",
                                         "frcr_ord_psbl_amt1": "205873.13"}}):
            assert K.buying_power("AAPL", 190.0) == 92742.31   # 통합증거금 안 씀(R6)
        # psamount 실패 시 → 원화예수금/fx 폴백(자동환전, 자가치유)
        def _by_path(path, tr, params):
            if "inquire-psamount" in path:
                return {"rt_cd": "1", "msg1": "일시 오류"}     # psamount 실패
            if "domestic-stock/v1/trading/inquire-balance" in path:
                return {"rt_cd": "0", "output1": [],
                        "output2": [{"prvs_rcdl_excc_amt": "9400000"}]}
            return None
        with mock.patch.object(K, "_get", side_effect=_by_path):
            assert abs(K.buying_power("AAPL", 190.0) - 9_400_000 / 1380.0) < 1e-6
        # 조회 전부 실패 → SEED 폴백(비바인딩, 자가치유)
        with mock.patch.object(K, "_get", return_value=None):
            assert K.domestic_buying_power("005930", 70000) == 10_000_000  # KR=SEED
            assert abs(K.buying_power("AAPL", 190.0) - 10_000_000 / 1380.0) < 1e-6
        # SEED 미설정(0)이면 실패 시 None(그땐 사이징 자체가 0)
        os.environ["BOT_SEED_KRW"] = "0"
        with mock.patch.object(K, "_get", return_value=None):
            assert K.buying_power("AAPL", 190.0) is None
        os.environ["BOT_SEED_KRW"] = "10000000"
        # env override는 항상 최우선
        os.environ["KIS_MOCK_BUYING_POWER"] = "777"
        assert K.buying_power("AAPL", 190.0) == 777.0
    print("[PASS] 모의 매수여력 — US=psamount(cash-only)·KR현금·실패=폴백·env우선")


def test_allowlist_file_fallback_and_env_precedence():
    with tempfile.TemporaryDirectory() as tmp:
        M = _setup(tmp)
        R = M["rollout"]
        path = os.path.join(tmp, "allowlist.txt")
        os.environ["ALLOWED_SYMBOLS_FILE"] = path
        os.environ.pop("ALLOWED_SYMBOLS")
        assert R.allowed_symbols() is None          # 파일도 env도 없음 = 미설정
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 주석\nFRPT\nestc, DASH  # 인라인 주석\n\n")
        assert R.allowed_symbols() == {"FRPT", "ESTC", "DASH"}
        ok, why = R.check_new_entry("FRPT", open_positions=0, risk_pct=0.001,
                                    session_open=True)
        assert ok, why
        assert not R.check_new_entry("AAPL", open_positions=0, risk_pct=0.001,
                                     session_open=True)[0]   # 파일 목록 밖
        os.environ["ALLOWED_SYMBOLS"] = "AAPL"
        assert R.allowed_symbols() == {"AAPL"}      # env가 파일을 이김
        # Codex P1-1: env가 **존재하면** 빈 값·공백 값도 확정이다 — 파일로
        #   폴백하면 운영자가 빈 env로 잠갔다고 믿는 동안 파일이 매수를 되살린다.
        for empty in ("", "   ", " , ,"):
            os.environ["ALLOWED_SYMBOLS"] = empty
            assert R.allowed_symbols() == set(), repr(empty)   # 파일 폴백 금지
            blocked, why = R.check_new_entry(
                "FRPT", open_positions=0, risk_pct=0.001, session_open=True)
            assert not blocked, (empty, why)                   # 전 종목 거부
        os.environ.pop("ALLOWED_SYMBOLS")
        assert R.allowed_symbols() == {"FRPT", "ESTC", "DASH"}  # env 제거 시만 파일
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 심볼 없음 — 편집 실수 시나리오\n")
        assert R.allowed_symbols() == set()
        assert not R.check_new_entry("FRPT", open_positions=0, risk_pct=0.001,
                                     session_open=True)[0]   # 빈 파일=전 거부
    print("[PASS] allowlist 파일 폴백·env 우선·빈 파일 전 거부(fail-closed)")


def main():
    test_kill_latch()
    test_rollout_gates()
    test_allowlist_file_fallback_and_env_precedence()
    test_execute_entry_rejects_nonfinite_inputs()
    test_execute_entry_rejects_invalid_optional_numeric_args()
    test_run_once_end_to_end_side_effects()
    test_ownership()
    test_costbook()
    test_x1_gate_chain_then_sent()
    test_mirror_stage()
    test_broker_truth_open_cost_gate()
    test_half_never_promotes_zero_sizing()
    test_combined_a_b_total_gate()
    test_gap_price_reserves_marketable_limit_not_stale_quote()
    test_missing_broker_budget_snapshot_blocks_send()
    test_mock_feasibility_fallbacks()
    print("\n모든 게이트 체인 테스트 통과 — I6/I7/IS2/IS5/costbook/X1/mirror.")


if __name__ == "__main__":
    main()
