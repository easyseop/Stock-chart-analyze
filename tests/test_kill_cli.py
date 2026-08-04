"""kill-switch 운영 CLI — 문서의 명령이 실제로 상태를 바꾸는지 검증."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kill

_OPERATOR_LOG = kill.LOG_PATH          # 테스트가 바꾸기 전의 운영 감사로그 경로


def test_documented_commands():
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "kill.json")
        kill.LOG_PATH = os.path.join(tmp, "kill.jsonl")
        with mock.patch.dict(os.environ, {
                "KILL_STATE_PATH": state, "KILL_LEVEL": "0"}, clear=False), \
             mock.patch("bot.notify.send", return_value=True):
            assert kill.main(["1", "P0", "수정", "검증"]) == 0
            assert kill.level() == 1
            assert not kill.allows("buy_new") and kill.allows("protect_sell")
            assert kill.main(["0", "검증", "완료", "--lower"]) == 0
            assert kill.level() == 0
            with mock.patch.object(kill, "_write_file", return_value=False):
                assert kill.main(["1", "쓰기", "실패"]) == 2
            assert kill.level() == 0
    print("✅ kill CLI 통과 — 문서 명령으로 L1 상향·ack 하향·보호매도 유지.")


def test_lower_flag_position_does_not_block_recovery():
    """사고 대응 중 `kill 0 --lower "사유"` 순서로 쳐도 하향돼야 한다.

    종전에는 argparse가 가변 위치인자 사이의 옵션을 못 읽어 통째로 거부했다
    (2026-08-04 실측: watchdog L1 복구가 이 형식 때문에 한 번 막힘).
    """
    for argv in (
            ["0", "--lower", "operator: 원인 해소 확인"],       # 옵션이 사유 앞
            ["0", "operator: 원인 해소 확인", "--lower"],       # 옵션이 맨 뒤
            ["0", "--lower", "operator:", "원인", "해소"],      # 여러 토막
    ):
        with tempfile.TemporaryDirectory() as tmp:
            kill.LOG_PATH = os.path.join(tmp, "kill.jsonl")
            with mock.patch.dict(os.environ, {
                    "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
                    "KILL_LEVEL": "0"}, clear=False), \
                 mock.patch("bot.notify.send", return_value=True):
                assert kill.main(["1", "watchdog", "테스트"]) == 0
                assert kill.main(argv) == 0, argv
                assert kill.level() == 0, argv
                # 사유는 어느 순서로 줘도 감사 로그에 온전히 남는다.
                rows = [json.loads(line) for line
                        in open(kill._log_path(), encoding="utf-8")
                        if line.strip()]
                ack = [r for r in rows if r.get("ev") == "lower"][-1]["ack"]
                assert "원인 해소" in ack, ack

    # 사유 없는 하향과 모르는 옵션은 계속 거부(빈 ack 방지·오타 무시 금지).
    with tempfile.TemporaryDirectory() as tmp:
        kill.LOG_PATH = os.path.join(tmp, "kill.jsonl")
        with mock.patch.dict(os.environ, {
                "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
                "KILL_LEVEL": "0"}, clear=False), \
             mock.patch("bot.notify.send", return_value=True):
            assert kill.main(["1", "watchdog", "테스트"]) == 0
            for bad in (["0", "--lower"], ["0", "--lowerr", "사유"]):
                try:
                    kill.main(bad)
                except SystemExit as exc:
                    assert exc.code == 2, bad
                else:
                    raise AssertionError(f"거부되어야 함: {bad}")
            assert kill.level() == 1                # 실패해도 상태 불변
    print("✅ --lower 위치 무관 하향 · 빈 사유/오타 옵션은 계속 거부")


def test_isolated_state_path_does_not_touch_operator_audit_log():
    """상태 경로를 격리하면 감사 로그도 함께 격리된다.

    종전에는 LOG_PATH가 모듈 상수라, 테스트가 운영 `bot/kill_log.jsonl`에
    `who=test` 기록을 남겼다(실측 오염 — 사고 조사 방해).
    """
    operator_log = kill.LOG_PATH = _OPERATOR_LOG
    before = (open(operator_log, "rb").read()
              if os.path.exists(operator_log) else b"")
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict(os.environ, {
                "KILL_STATE_PATH": os.path.join(tmp, "kill.json"),
                "KILL_LEVEL": "0"}, clear=False), \
             mock.patch("bot.notify.send", return_value=True):
            assert kill._log_path() == os.path.join(tmp, "kill_log.jsonl")
            kill.raise_level(1, "test", "readiness")
            assert os.path.exists(os.path.join(tmp, "kill_log.jsonl"))
    after = (open(operator_log, "rb").read()
             if os.path.exists(operator_log) else b"")
    assert after == before, "운영 감사 로그가 테스트로 오염됨"
    print("✅ 격리 실행은 운영 감사 로그를 건드리지 않음")


def main() -> int:
    test_documented_commands()
    test_lower_flag_position_does_not_block_recovery()
    test_isolated_state_path_does_not_touch_operator_audit_log()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
