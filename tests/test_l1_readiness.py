"""L1 readiness는 증거가 하나라도 없으면 fail-closed하고 상태를 바꾸지 않는다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import l1_readiness as R  # noqa: E402


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
HALF = ["AQN", "CAG", "GPK", "KKR", "LW", "SNN", "STE", "VRSK", "WDAY"]
FROZEN = ["AQN", "CAG", "GPK", "LW", "SNN", "VRSK"]


def _snapshot() -> dict:
    return {
        "kis_env": "mock",
        "kill_level": 1,
        "buy_new_allowed": False,
        "protect_sell_allowed": True,
        "ledger_healthy": True,
        "local_open_orders": 0,
        "broker_open_orders": 0,
        "unresolved_unknowns": 0,
        "unaccounted_buy_fills": 0,
        "costbook_healthy": True,
        "open_cost_krw": 26_214_776.70,
        "operating_limit_krw": 33_250_000,
        "positions_match_broker": True,
        "ownership_armed": True,
        "baseline_symbol_count": 0,
        "baseline_path_volatile": False,
        "heartbeat_age_s": 23.0,
        "fallback_enabled": False,
        "stall_exit_mode": "shadow",
        "half_ratchet_verified": list(HALF),
        "frozen_symbols": list(FROZEN),
        "trade_stage": "mirror",
        "allowed_symbols": [],
        "allow_buy_enabled": True,
        "orders_enabled": True,
        "position_counts_by_sleeve": {"A": 16, "B": 0},
    }


def _evidence() -> dict:
    return {
        "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "stall_shadow_started_at": (NOW - timedelta(days=8)).isoformat(),
        "half_ratchet_symbols": list(HALF),
        "frozen_review_symbols": list(FROZEN),
        "frozen_decisions": {code: "keep_close_only" for code in FROZEN},
        "oracle_brain_sessions": {"KR": 1, "US": 1},
        "github_outage": {"minutes": 60, "new_orders": 0},
    }


def _blockers(report: dict) -> set[str]:
    return {gate["name"] for gate in report["blockers"]}


def _pending(report: dict) -> set[str]:
    return {gate["name"] for gate in report["informational_findings"]}


def test_complete_evidence_only_allows_operator_review():
    report = R.evaluate(_snapshot(), _evidence(), now=NOW)
    assert report["scope"] == "strict"
    assert report["ready_for_operator_review"] is True
    assert report["operator_approval_still_required"] is True
    assert not report["blockers"]
    assert "자동으로 내리지 않는다" in R.render_text(report)
    print("[PASS] 전체 기술 증거 → operator 검토 가능, 자동 L1 하향 없음")


def test_missing_or_stale_observations_block():
    evidence = _evidence()
    evidence["oracle_brain_sessions"]["US"] = 0
    evidence["github_outage"] = {"minutes": 59, "new_orders": 0}
    evidence["observed_at"] = (NOW - timedelta(hours=73)).isoformat()
    evidence["stall_shadow_started_at"] = (NOW - timedelta(days=6)).isoformat()
    report = R.evaluate(_snapshot(), evidence, now=NOW)
    assert {
        "oracle_brain_both_markets",
        "github_outage_injection",
        "observation_evidence_fresh",
        "stall_shadow_observed",
    } <= _blockers(report)
    print("[PASS] 세션·60분 장애·증거 신선도·7일 shadow 부족 → NO-GO")


def test_runtime_safety_invariants_block_independently():
    snapshot = _snapshot()
    snapshot.update({
        "kill_level": 0,
        "buy_new_allowed": True,
        "local_open_orders": 1,
        "broker_open_orders": None,
        "unaccounted_buy_fills": 1,
        "open_cost_krw": 34_000_000,
        "positions_match_broker": False,
        "heartbeat_age_s": 61,
        "fallback_enabled": True,
        "stall_exit_mode": "live",
        "half_ratchet_verified": HALF[:-1],
    })
    evidence = _evidence()
    evidence["frozen_decisions"].pop("VRSK")
    report = R.evaluate(snapshot, evidence, now=NOW)
    assert {
        "l1_latched",
        "l1_permissions",
        "no_open_orders",
        "orders_fully_reconciled",
        "operating_budget",
        "positions_match_broker",
        "heartbeat_fresh",
        "fallback_still_shadow",
        "stall_mode_shadow",
        "half_ratchets_durable",
        "frozen_symbols_reviewed",
    } <= _blockers(report)
    print("[PASS] 각 운영 불변식 위반은 독립적으로 L1 하향 차단")


def test_missing_evidence_fails_closed():
    report = R.evaluate(_snapshot(), {}, now=NOW)
    assert not report["ready_for_operator_review"]
    assert {
        "stall_shadow_observed",
        "half_ratchets_durable",
        "frozen_symbols_reviewed",
        "oracle_brain_both_markets",
        "github_outage_injection",
        "observation_evidence_fresh",
    } <= _blockers(report)
    print("[PASS] 증거 파일 없음/손상 → fail-closed NO-GO")


def test_required_symbol_lists_cannot_be_shortened_or_silently_unfrozen():
    evidence = _evidence()
    evidence["half_ratchet_symbols"].pop()
    evidence["frozen_review_symbols"].pop()
    snapshot = _snapshot()
    snapshot["frozen_symbols"].remove("AQN")
    report = R.evaluate(snapshot, evidence, now=NOW)
    assert {
        "half_ratchets_durable",
        "frozen_symbols_reviewed",
    } <= _blockers(report)

    evidence = _evidence()
    evidence["frozen_decisions"]["AQN"] = "unfreeze_approved"
    report = R.evaluate(_snapshot(), evidence, now=NOW)
    assert "frozen_symbols_reviewed" in _blockers(report)
    print("[PASS] 고정 검토목록 축소·결정 없는 동결해제·결정-상태 불일치 차단")


def test_l0_scope_separates_independent_observation_gates():
    report = R.evaluate(_snapshot(), {}, scope="l0", now=NOW)
    assert report["ready_for_operator_review"] is True
    assert not report["blockers"]
    assert {
        "stall_shadow_observed",
        "half_ratchets_durable",
        "frozen_symbols_reviewed",
        "oracle_brain_both_markets",
        "github_outage_injection",
        "observation_evidence_fresh",
    } == _pending(report)
    assert "[INFO] stall_shadow_observed" in R.render_text(report)
    print("[PASS] L0 scope는 별도 기능 관찰을 INFO로 분리")


def test_l0_scope_keeps_limited_mode_fences_blocking():
    snapshot = _snapshot()
    snapshot.update({
        "fallback_enabled": True,
        "stall_exit_mode": "live",
        "frozen_symbols": FROZEN[:-1],
        "allow_buy_enabled": False,
        "positions_match_broker": False,
    })
    report = R.evaluate(snapshot, {}, scope="l0", now=NOW)
    assert {
        "fallback_still_shadow",
        "stall_mode_shadow",
        "frozen_symbols_preserved",
        "limited_l0_fence",
        "positions_match_broker",
    } <= _blockers(report)
    assert "stall_shadow_observed" in _pending(report)
    print("[PASS] L0 scope도 fallback·shadow·동결6·주문설정·수량대조를 차단")


def test_l0_fence_requires_direct_entry_configuration_without_allowlist():
    # scanner-direct L0는 수동 종목목록이 없어야 한다. 낡은 목록이 남으면 실제
    # 집행범위를 사용자가 오해할 수 있으므로 readiness가 제거 전환을 차단한다.
    for field, value in (
            ("trade_stage", "1.5"),
            ("trade_stage", "MIRROR"),
            ("allowed_symbols", ["AAPL"]),
            ("allowed_symbols", ["  ", "AAPL"]),
            ("allow_buy_enabled", False),
            ("orders_enabled", False)):
        snapshot = _snapshot()
        snapshot[field] = value
        for scope in ("l0", "strict"):
            report = R.evaluate(snapshot, {}, scope=scope, now=NOW)
            assert "limited_l0_fence" in _blockers(report), (scope, field, value)
    snapshot = _snapshot()
    del snapshot["allowed_symbols"]
    report = R.evaluate(snapshot, {}, scope="l0", now=NOW)
    assert "limited_l0_fence" not in _blockers(report)
    print("[PASS] scanner-direct L0는 mirror·allowlist 없음·명시적 주문설정 필수")


def test_empty_env_disables_stale_file_and_allows_direct_entry_scope():
    """빈/공백 env는 낡은 파일을 되살리지 않고 scanner-direct 전체 후보 모드다."""
    import tempfile as _tempfile
    from bot import rollout
    with _tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "allowlist.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("AAPL\n")                       # 낡은 파일이 남아 있는 상황
        old_file = os.environ.get("ALLOWED_SYMBOLS_FILE")
        old_env = os.environ.get("ALLOWED_SYMBOLS")
        try:
            os.environ["ALLOWED_SYMBOLS_FILE"] = path
            for empty in ("", "   "):
                os.environ["ALLOWED_SYMBOLS"] = empty
                # collect_runtime과 같은 수집식(env 우선) — 파일 폴백이면 {'AAPL'}.
                collected = rollout.allowed_symbols() or set()
                assert collected == set(), repr(empty)
                snapshot = _snapshot()
                snapshot["allowed_symbols"] = sorted(collected)
                for scope in ("l0", "strict"):
                    report = R.evaluate(snapshot, {}, scope=scope, now=NOW)
                    assert "limited_l0_fence" not in _blockers(report), (empty, scope)
            os.environ.pop("ALLOWED_SYMBOLS")
            assert rollout.allowed_symbols() == {"AAPL"}   # env 부재 시만 파일
        finally:
            for key, val in (("ALLOWED_SYMBOLS_FILE", old_file),
                             ("ALLOWED_SYMBOLS", old_env)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
    print("[PASS] 빈 env는 낡은 파일을 되살리지 않고 scanner-direct 전체 후보 유지")


def test_unarmed_ownership_blocks_and_volatile_path_warns():
    # 2026-07-31 실사고 재현: baseline 소실(전 매수 거부)인데 체커가 GO를 냈다.
    snapshot = _snapshot()
    snapshot.update({
        "ownership_armed": False,
        "baseline_symbol_count": None,
        "baseline_path_volatile": True,
    })
    for scope in ("strict", "l0"):
        report = R.evaluate(snapshot, _evidence(), scope=scope, now=NOW)
        assert "ownership_armed" in _blockers(report), scope
        assert "baseline_path_persistent" in _pending(report), scope
        assert "baseline_path_persistent" not in _blockers(report), scope

    # 수집기가 필드를 아예 안 준(구버전 snapshot) 경우도 fail-closed.
    snapshot = _snapshot()
    del snapshot["ownership_armed"]
    del snapshot["baseline_path_volatile"]
    report = R.evaluate(snapshot, _evidence(), now=NOW)
    assert "ownership_armed" in _blockers(report)
    assert "baseline_path_persistent" in _pending(report)
    print("[PASS] baseline 미캡처=차단, 휘발성 경로=경고, 필드 결손=fail-closed")


def test_unknown_scope_is_rejected():
    try:
        R.evaluate(_snapshot(), _evidence(), scope="live", now=NOW)
    except ValueError as exc:
        assert "unknown readiness scope" in str(exc)
    else:
        raise AssertionError("unknown scope must fail closed")
    print("[PASS] 알 수 없는 readiness scope 거부")


def test_cli_forwards_l0_scope_without_mutation():
    cli = importlib.import_module("scripts.kis_l1_readiness")
    report = {
        "scope": "l0",
        "ready_for_operator_review": True,
        "operator_approval_still_required": True,
        "gates": [],
        "blockers": [],
        "informational_findings": [],
    }
    with mock.patch.object(cli.l1_readiness, "load_evidence",
                           return_value={}), \
            mock.patch.object(cli.l1_readiness, "collect_runtime",
                              return_value=_snapshot()), \
            mock.patch.object(cli.l1_readiness, "evaluate",
                              return_value=report) as evaluate, \
            mock.patch("builtins.print"):
        result = cli.main(["--scope", "l0", "--broker", "--json"])
    assert result == 0
    evaluate.assert_called_once_with(_snapshot(), {}, scope="l0")
    print("[PASS] CLI --scope l0가 평가기에 전달되고 자동 mutation 없음")


def test_collect_runtime_is_read_only_without_broker():
    with tempfile.TemporaryDirectory() as tmp:
        for key in list(os.environ):
            if key.startswith((
                    "KIS_", "KILL_", "BOT_", "COSTBOOK", "ORDER_LEDGER",
                    "SYMBOL_FREEZE", "SENTINEL_HEARTBEAT", "STALL_EXIT",
                    "ORACLE_SIGNAL", "ALLOW", "TRADE_STAGE")):
                os.environ.pop(key, None)
        os.environ.update({
            "KIS_ENV": "mock",
            "KIS_MOCK_APPKEY": "k",
            "KIS_MOCK_APPSECRET": "s",
            "KIS_MOCK_CANO": "50000000",
            "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
            "ORDER_LEDGER_PATH": os.path.join(tmp, "ledger.jsonl"),
            "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
            "KIS_POSITIONS_PATH": os.path.join(tmp, "positions.jsonl"),
            "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
            "SENTINEL_HEARTBEAT_PATH": os.path.join(tmp, "heartbeat.json"),
            "BOT_SEED_KRW": "30000000",
            "BOT_SEED_SB_KRW": "5000000",
            "BOT_OPERATING_TOTAL_KRW": "35000000",
            "BOT_OPERATING_BUFFER_PCT": "0.05",
            "ORACLE_SIGNAL_FALLBACK_ENABLED": "0",
            "STALL_EXIT_MODE": "shadow",
            "KIS_ORDERS_ENABLED": "1",
            "ALLOW_BUY": "1",
            "TRADE_STAGE": "mirror",
            "ALLOWED_SYMBOLS": "AAPL, msft",
        })
        modules = {}
        for name in (
                "kis", "ledger", "kill", "ownership", "costbook",
                "envelope", "heartbeat", "kis_positions", "settings"):
            module = importlib.import_module(f"bot.{name}")
            importlib.reload(module)
            modules[name] = module

        modules["kill"].raise_level(1, "test", "readiness")
        modules["heartbeat"].write()
        modules["ownership"].freeze("AQN", "legacy")
        before = {
            path: open(path, "rb").read()
            for path in (
                os.environ["KILL_STATE_PATH"],
                os.environ["SYMBOL_FREEZE_PATH"],
                os.environ["SENTINEL_HEARTBEAT_PATH"],
            )
        }
        with mock.patch.object(modules["kis"], "holdings") as holdings, \
             mock.patch.object(modules["kis"], "open_orders") as open_orders, \
             mock.patch.object(modules["kis"], "domestic_open_orders") as domestic:
            snapshot = R.collect_runtime(fetch_broker=False, evidence={})
        assert snapshot["broker_open_orders"] is None
        assert snapshot["positions_match_broker"] is None
        assert snapshot["trade_stage"] == "mirror"
        assert snapshot["allowed_symbols"] == ["AAPL", "MSFT"]
        assert snapshot["allow_buy_enabled"] is True
        assert snapshot["orders_enabled"] is True
        assert snapshot["position_counts_by_sleeve"] == {"A": 0, "B": 0}
        assert not holdings.called and not open_orders.called and not domestic.called
        for path, original in before.items():
            assert open(path, "rb").read() == original
        assert "kis_orders" not in sys.modules
    print("[PASS] 기본 수집은 KIS·주문 호출과 상태 파일 변경 0건")


def test_broker_order_parser_fails_closed():
    assert R._broker_open_count(None) is None
    assert R._broker_open_count({"rt_cd": "1"}) is None
    assert R._broker_open_count({"rt_cd": "0"}) is None
    assert R._broker_open_count({"rt_cd": "0", "output": {}}) is None
    assert R._broker_open_count({
        "rt_cd": "0", "output": [{"nccs_qty": "0"}, {"nccs_qty": "2"}],
    }) == 1
    assert R._broker_open_count({
        "rt_cd": "0", "output": [{"ord_qty": "4", "tot_ccld_qty": "4"}],
    }) == 0
    assert R._broker_open_count({
        "rt_cd": "0", "output": [{"unknown": "field"}],
    }) is None
    assert R._broker_domestic_unfilled_count(None) is None
    assert R._broker_domestic_unfilled_count({"rt_cd": "1", "output1": []}) is None
    assert R._broker_domestic_unfilled_count({"rt_cd": "0"}) is None
    assert R._broker_domestic_unfilled_count({"rt_cd": "0", "output1": {}}) is None
    assert R._broker_domestic_unfilled_count({
        "rt_cd": "0", "output1": [], "ctx_area_nk100": "next",
    }) is None
    assert R._broker_domestic_unfilled_count({
        "rt_cd": "0", "output1": [], "ctx_area_nk100": "",
    }) == 0
    # 미체결 필터 응답은 행에 수량 필드가 없어도 열린 주문으로 보수적으로 센다.
    assert R._broker_domestic_unfilled_count({
        "rt_cd": "0", "output1": [{"odno": "1"}, {"odno": "2"}],
    }) == 2
    print("[PASS] 브로커 미체결 응답 결손은 열린주문 0으로 추측하지 않음")


def test_mock_domestic_unfilled_query_contract():
    kis = importlib.import_module("bot.kis")
    response = {"rt_cd": "0", "output1": []}
    with mock.patch.object(kis, "account", return_value={
            "CANO": "masked", "ACNT_PRDT_CD": "01"}), \
         mock.patch.object(kis, "enabled", return_value=True), \
         mock.patch.object(kis, "tr_id", return_value="VTTC8001R"), \
         mock.patch.object(kis.time, "strftime", return_value="20260806"), \
         mock.patch.object(kis, "_get", return_value=response) as get:
        assert kis.domestic_unfilled_orders() is response
    path, tr, params = get.call_args.args
    assert path.endswith("/inquire-daily-ccld")
    assert tr == "VTTC8001R"
    assert params["CCLD_DVSN"] == "02"
    assert params["INQR_STRT_DT"] == params["INQR_END_DT"] == "20260806"
    print("[PASS] 국내 mock 미체결 증명은 일별체결 CCLD_DVSN=02만 사용")


def test_broker_read_only_snapshot_matches_three_ledgers():
    with tempfile.TemporaryDirectory() as tmp:
        for key in list(os.environ):
            if key.startswith((
                    "KIS_", "KILL_", "BOT_", "COSTBOOK", "ORDER_LEDGER",
                    "SYMBOL_FREEZE", "SENTINEL_HEARTBEAT", "STALL_EXIT",
                    "ORACLE_SIGNAL", "ALLOW", "TRADE_STAGE")):
                os.environ.pop(key, None)
        os.environ.update({
            "KIS_ENV": "mock",
            "KIS_MOCK_APPKEY": "k",
            "KIS_MOCK_APPSECRET": "s",
            "KIS_MOCK_CANO": "50000000",
            "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
            "ORDER_LEDGER_PATH": os.path.join(tmp, "ledger.jsonl"),
            "COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl"),
            "KIS_POSITIONS_PATH": os.path.join(tmp, "positions.jsonl"),
            "SYMBOL_FREEZE_PATH": os.path.join(tmp, "freeze.json"),
            "SENTINEL_HEARTBEAT_PATH": os.path.join(tmp, "heartbeat.json"),
            "BOT_SEED_KRW": "30000000",
            "BOT_SEED_SB_KRW": "5000000",
            "BOT_OPERATING_TOTAL_KRW": "35000000",
            "BOT_OPERATING_BUFFER_PCT": "0.05",
            "ORACLE_SIGNAL_FALLBACK_ENABLED": "0",
            "STALL_EXIT_MODE": "shadow",
            "KIS_ORDERS_ENABLED": "1",
            "ALLOW_BUY": "1",
            "TRADE_STAGE": "mirror",
            "ALLOWED_SYMBOLS": "AAPL",
        })
        modules = {}
        for name in (
                "kis", "ledger", "kill", "ownership", "costbook",
                "envelope", "heartbeat", "kis_positions", "settings"):
            module = importlib.import_module(f"bot.{name}")
            importlib.reload(module)
            modules[name] = module
        modules["kis_positions"].record(
            "AAPL", stop=90, ccy="USD", entry=100, qty=2, pos_key="p1")
        modules["costbook"].add_lot(
            "p1", "AAPL", 2, 100, fx=1, sleeve="A", event_id="buy:1")
        empty_orders = {"rt_cd": "0", "output": []}
        with mock.patch.object(
                modules["kis"], "holdings",
                side_effect=[{}, {"AAPL": 2}, {"AAPL": 2}, {"AAPL": 2}]), \
             mock.patch.object(
                 modules["kis"], "domestic_open_orders",
                 return_value=empty_orders) as domestic, \
             mock.patch.object(
                 modules["kis"], "open_orders",
                 return_value=empty_orders) as overseas:
            snapshot = R.collect_runtime(fetch_broker=True, evidence={})
        assert snapshot["positions_match_broker"] is True
        assert snapshot["broker_open_orders"] == 0
        assert snapshot["position_counts_by_sleeve"] == {"A": 1, "B": 0}
        assert domestic.call_count == 0 and overseas.call_count == 3

        # 비어 있지 않은 optional 축소목록에 KR 종목이 들어오면 국내 API도 필수다.
        # 이때 실패를 0건으로 추측하지 않고 L0 점검을 계속 차단한다.
        os.environ["ALLOWED_SYMBOLS"] = "AAPL,005930"
        with mock.patch.object(
                modules["kis"], "holdings",
                side_effect=[{}, {"AAPL": 2}, {"AAPL": 2}, {"AAPL": 2}]), \
             mock.patch.object(
                 modules["kis"], "domestic_open_orders",
                 return_value=None) as domestic, \
             mock.patch.object(
                 modules["kis"], "domestic_unfilled_orders",
                 return_value=None) as domestic_fallback, \
             mock.patch.object(
                 modules["kis"], "open_orders",
                 return_value=empty_orders) as overseas:
            snapshot = R.collect_runtime(fetch_broker=True, evidence={})
        assert snapshot["broker_open_orders"] is None
        assert domestic.call_count == 1 and domestic_fallback.call_count == 1
        assert overseas.call_count == 3

        # 사용자가 확정한 unrestricted scanner-direct는 목록이 없으므로 미래에
        # 어느 시장 신호가 와도 안전하도록 KR+US 양쪽 미체결을 모두 증명한다.
        # 국내 mock의 정정취소가능 조회가 미지원이어도 실측 지원되는 일별체결
        # 미체결(02) 필터가 완전한 0건을 증명하면 양 시장 0건으로 판정한다.
        os.environ["ALLOWED_SYMBOLS"] = ""
        with mock.patch.object(
                modules["kis"], "holdings",
                side_effect=[{}, {"AAPL": 2}, {"AAPL": 2}, {"AAPL": 2}]), \
             mock.patch.object(
                 modules["kis"], "domestic_open_orders",
                 return_value=None) as domestic, \
             mock.patch.object(
                 modules["kis"], "domestic_unfilled_orders",
                 return_value={"rt_cd": "0", "output1": [],
                               "ctx_area_nk100": ""}) as domestic_fallback, \
             mock.patch.object(
                 modules["kis"], "open_orders",
                 return_value=empty_orders) as overseas:
            snapshot = R.collect_runtime(fetch_broker=True, evidence={})
        assert snapshot["allowed_symbols"] == []
        assert snapshot["broker_open_orders"] == 0
        assert domestic.call_count == 1 and domestic_fallback.call_count == 1
        assert overseas.call_count == 3

        # 대체 응답도 실패·불완전하면 0으로 추측하지 않는다.
        with mock.patch.object(
                modules["kis"], "holdings",
                side_effect=[{}, {"AAPL": 2}, {"AAPL": 2}, {"AAPL": 2}]), \
             mock.patch.object(modules["kis"], "domestic_open_orders",
                               return_value=None), \
             mock.patch.object(modules["kis"], "domestic_unfilled_orders",
                               return_value={"rt_cd": "0", "output1": [],
                                             "ctx_area_nk100": "next"}), \
             mock.patch.object(modules["kis"], "open_orders",
                               return_value=empty_orders):
            snapshot = R.collect_runtime(fetch_broker=True, evidence={})
        assert snapshot["broker_open_orders"] is None

        # live에서는 mock 전용 대체 증명을 절대 호출하지 않는다. 기존 국내 조회를
        # 증명하지 못하면 그대로 None을 유지해 L1을 막는다.
        with mock.patch.object(modules["kis"], "IS_MOCK", False), \
             mock.patch.object(
                 modules["kis"], "holdings",
                 side_effect=[{}, {"AAPL": 2}, {"AAPL": 2}, {"AAPL": 2}]), \
             mock.patch.object(modules["kis"], "domestic_open_orders",
                               return_value=None) as domestic, \
             mock.patch.object(modules["kis"], "domestic_unfilled_orders",
                               return_value={"rt_cd": "0", "output1": []
                                             }) as domestic_fallback, \
             mock.patch.object(modules["kis"], "open_orders",
                               return_value=empty_orders) as overseas:
            snapshot = R.collect_runtime(fetch_broker=True, evidence={})
        assert snapshot["broker_open_orders"] is None
        assert domestic.call_count == 1 and domestic_fallback.call_count == 0
        assert overseas.call_count == 3
    print("[PASS] unrestricted는 양 시장 미체결 증명, fallback 결손은 fail-closed")


def main():
    test_complete_evidence_only_allows_operator_review()
    test_missing_or_stale_observations_block()
    test_runtime_safety_invariants_block_independently()
    test_missing_evidence_fails_closed()
    test_required_symbol_lists_cannot_be_shortened_or_silently_unfrozen()
    test_l0_scope_separates_independent_observation_gates()
    test_l0_scope_keeps_limited_mode_fences_blocking()
    test_l0_fence_requires_direct_entry_configuration_without_allowlist()
    test_empty_env_disables_stale_file_and_allows_direct_entry_scope()
    test_unarmed_ownership_blocks_and_volatile_path_warns()
    test_unknown_scope_is_rejected()
    test_cli_forwards_l0_scope_without_mutation()
    test_collect_runtime_is_read_only_without_broker()
    test_broker_order_parser_fails_closed()
    test_mock_domestic_unfilled_query_contract()
    test_broker_read_only_snapshot_matches_three_ledgers()
    print("\n모든 L1 readiness 테스트 통과.")


if __name__ == "__main__":
    main()
