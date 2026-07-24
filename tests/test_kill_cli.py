"""kill-switch 운영 CLI — 문서의 명령이 실제로 상태를 바꾸는지 검증."""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kill


def main() -> int:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
