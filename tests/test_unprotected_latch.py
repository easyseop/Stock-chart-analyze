"""무보호 보유 경보의 영속 래치 — 반복 경보가 진짜 새 고아를 묻는 것을 막는다.

실측 2026-08-31~09-01: 파수꾼이 무보호 집합을 프로세스 메모리(`state["_unprot"]`)에
들고 있어서, 재기동과 일시적 잔고 이상마다 같은 고아가 '새것'으로 다시 P0가 됐다.
OMCL 2주 하나가 하룻밤에 세 번 올라왔다. 반복 경보는 그 자체로 위험하다 —
익숙해지는 순간 진짜 새 고아를 그냥 넘기게 된다.
"""
import json
import os
import tempfile

import pytest


@pytest.fixture()
def po(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("PROTECTION_ALERT_LATCH_PATH",
                       os.path.join(tmp, "latch.json"))
    monkeypatch.delenv("UNPROTECTED_CLEAR_CONFIRMATIONS", raising=False)
    from bot import protection_observability
    return protection_observability


def test_first_sighting_is_new(po):
    assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == ({"OMCL"}, set())


def test_same_orphan_is_not_reported_twice(po):
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    for _ in range(5):
        assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == (set(), set())


def test_survives_process_restart(po):
    """모듈 상태가 아니라 파일이라 재기동해도 조용하다."""
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    import importlib
    reloaded = importlib.reload(po)
    assert reloaded.unprotected_transitions(
        {"OMCL"}, scope_markets={"US", "KR"}) == (set(), set())


def test_new_orphan_still_surfaces_alongside_known_one(po):
    """이게 핵심 — 알려진 고아가 있어도 새것은 반드시 보고돼야 한다."""
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    fresh, resolved = po.unprotected_transitions({"OMCL", "CVNA"}, scope_markets={"US", "KR"})
    assert fresh == {"CVNA"} and resolved == set()


def test_single_absence_does_not_resolve(po):
    """브로커 응답에서 한 사이클 빠졌다고 해소로 보면 경보가 되살아난다."""
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())      # 1회
    fresh, resolved = po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})           # 다시 관측
    assert fresh == set() and resolved == set(), "깜빡임이 경보를 되살렸다"


def test_reappearance_resets_the_absence_counter(po):
    """부재 카운터는 **연속**이어야 한다.

    깜빡임(있음→없음→있음→없음)에서 카운터가 누적되면, 브로커 응답이 불안정한
    구간에 래치가 조용히 풀린다. 그러면 다음 관측에서 같은 고아가 '새것'으로
    다시 P0가 되고 — 고치려던 반복 경보가 그대로 돌아온다.
    """
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())        # 부재 1
    assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == (set(), set())     # 재관측 → 리셋
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set()), \
        "재관측 뒤 부재 1회로 해소됐다 — 카운터가 초기화되지 않았다"
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), {"OMCL"})     # 부재 2 연속


def test_two_consecutive_absences_resolve(po):
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), {"OMCL"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())       # 재알림 없음


def test_resolved_symbol_can_alert_again_later(po):
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    po.unprotected_transitions(set(), scope_markets={"US", "KR"})
    po.unprotected_transitions(set(), scope_markets={"US", "KR"})
    assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == ({"OMCL"}, set())


def test_confirmations_are_configurable(po, monkeypatch):
    monkeypatch.setenv("UNPROTECTED_CLEAR_CONFIRMATIONS", "3")
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), set())
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"}) == (set(), {"OMCL"})


@pytest.mark.parametrize("raw", ["0", "-1", "abc", ""])
def test_confirmations_env_is_bounded(po, monkeypatch, raw):
    monkeypatch.setenv("UNPROTECTED_CLEAR_CONFIRMATIONS", raw)
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    assert po.unprotected_transitions(set(), scope_markets={"US", "KR"})[1] in (set(), {"OMCL"})


def test_symbols_are_normalised(po):
    assert po.unprotected_transitions({"omcl", " "}, scope_markets={"US", "KR"}) == ({"OMCL"}, set())
    assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == (set(), set())


def test_unwritable_latch_reports_nothing_new(po, monkeypatch):
    """래치를 못 쓰면 전부 '새것'으로 처리해 매 사이클 폭주하면 안 된다."""
    monkeypatch.setenv("PROTECTION_ALERT_LATCH_PATH", "/proc/nonexistent/x.json")
    assert po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"}) == (set(), set())


def test_does_not_disturb_other_latches(po):
    """같은 파일을 쓰는 F3/F4 래치를 밟지 않는다."""
    po._update_latch("blocked", add={"INGR"}, remove=set())
    po.unprotected_transitions({"OMCL"}, scope_markets={"US", "KR"})
    state = po._read_latches()
    assert state["blocked"] == {"INGR"}
    assert state["unprotected"] == {"OMCL"}


def test_latch_file_stays_valid_json(po):
    po.unprotected_transitions({"OMCL", "CVNA"}, scope_markets={"US", "KR"})
    with open(os.environ["PROTECTION_ALERT_LATCH_PATH"], encoding="utf-8") as fp:
        payload = json.load(fp)
    assert sorted(payload["unprotected"]) == ["CVNA", "OMCL"]
    assert payload["blocked"] == [] and payload["sellable_gap"] == []


def test_closed_market_snapshot_does_not_resolve(po):
    """장이 닫히면 holdings()가 조회를 아예 안 하고 빈 맵을 준다.

    실측 2026-09-02: 그 빈 맵을 '사라졌다'로 읽어 미장 마감 중 OMCL 래치가
    풀렸고, 22:31 개장과 함께 같은 고아가 또 P0로 올라왔다. `조회 실패 ≠ 부재`와
    같은 원칙이 `미조회 ≠ 부재`에도 적용돼야 한다.
    """
    po.unprotected_transitions({"OMCL"}, scope_markets={"US"})
    for _ in range(5):                       # 마감 내내 빈 스냅샷
        assert po.unprotected_transitions(set(), scope_markets=set()) == (set(), set())
    # 개장 — 여전히 알려진 고아여야 한다(새 P0 금지)
    assert po.unprotected_transitions(
        {"OMCL"}, scope_markets={"US"}) == (set(), set())


def test_other_market_open_does_not_resolve_us_orphan(po):
    """한국장만 열린 사이클은 미국 심볼을 판단할 근거가 없다."""
    po.unprotected_transitions({"OMCL"}, scope_markets={"US"})
    for _ in range(5):
        assert po.unprotected_transitions(set(), scope_markets={"KR"}) == (set(), set())
    assert po.unprotected_transitions(
        {"OMCL"}, scope_markets={"US"}) == (set(), set())


def test_kr_symbol_is_scoped_to_kr(po):
    """6자리 숫자는 국내 — 미장만 열린 스냅샷으로 해소되면 안 된다."""
    po.unprotected_transitions({"016360"}, scope_markets={"KR"})
    for _ in range(5):
        assert po.unprotected_transitions(set(), scope_markets={"US"}) == (set(), set())
    assert po.unprotected_transitions(
        set(), scope_markets={"KR"}) == (set(), set())            # 1회차
    assert po.unprotected_transitions(
        set(), scope_markets={"KR"}) == (set(), {"016360"})       # 2회차 해소


def test_in_scope_resolution_still_works(po):
    """범위 안이면 종전대로 연속 2회에 해소된다 — 가드가 해소를 막지 않는다."""
    po.unprotected_transitions({"OMCL"}, scope_markets={"US"})
    assert po.unprotected_transitions(set(), scope_markets={"US"}) == (set(), set())
    assert po.unprotected_transitions(set(), scope_markets={"US"}) == (set(), {"OMCL"})
