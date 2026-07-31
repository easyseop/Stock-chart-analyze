"""baseline 기본 경로는 재부팅에 살아남아야 한다(2026-07-31 /tmp 소실 실사고).

동결 파일은 감사 수정 #10에서 이미 영속 경로로 옮겨졌지만 baseline은 /tmp에
남아 있었다. 커널 재부팅으로 /tmp가 비워지자 fail-closed 설계에 따라 전 종목
매수 거부가 조용히 발동했다. 이 모듈은 그 회귀를 막는다.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_default_baseline_path_is_persistent():
    os.environ.pop("USER_BASELINE_PATH", None)
    from bot import ownership
    importlib.reload(ownership)
    tmp_root = os.path.realpath(tempfile.gettempdir())
    path = os.path.realpath(ownership.baseline_path())
    assert not path.startswith(tmp_root + os.sep), path
    assert path == os.path.realpath(os.path.join(
        os.path.dirname(ownership.__file__), "user_baseline.json"))
    print("[PASS] 기본 baseline 경로는 tempdir 밖(모듈 옆 영속 경로)")


def test_env_override_wins_and_fail_closed_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["USER_BASELINE_PATH"] = os.path.join(tmp, "base.json")
        os.environ["SYMBOL_FREEZE_PATH"] = os.path.join(tmp, "freeze.json")
        from bot import ownership
        importlib.reload(ownership)
        assert ownership.baseline_path() == os.environ["USER_BASELINE_PATH"]

        # 파일 없음 = None = 전 종목 거부. 읽기 경로가 파일을 자동 생성하지 않는다.
        assert ownership.baseline() is None
        denied, why = ownership.buy_denied("AAPL")
        assert denied and "미캡처" in why
        assert not os.path.exists(os.environ["USER_BASELINE_PATH"])

        # 정상 캡처(빈 계좌 포함) 후에만 매수 허용.
        assert ownership.capture_baseline(None) is False     # 조회 실패=거부
        assert ownership.baseline() is None
        assert ownership.capture_baseline([]) is True
        assert ownership.baseline() == set()
        denied, _ = ownership.buy_denied("AAPL")
        assert denied is False
    for key in ("USER_BASELINE_PATH", "SYMBOL_FREEZE_PATH"):
        os.environ.pop(key, None)
    print("[PASS] env override 우선·fail-closed 유지·자동 생성 없음")


def main():
    test_default_baseline_path_is_persistent()
    test_env_override_wins_and_fail_closed_unchanged()
    print("\n모든 ownership baseline 경로 테스트 통과.")


if __name__ == "__main__":
    main()
