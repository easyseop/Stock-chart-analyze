"""의존성 없는 전체 회귀 테스트 실행기.

pytest가 없는 Oracle/로컬 환경에서도 각 테스트를 별도 프로세스로 격리해 실행한다.
하나라도 실패하면 마지막에 비정상 종료하므로 배포 전 단일 명령으로 판정할 수 있다.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failed: list[str] = []
    files = sorted((ROOT / "tests").glob("test_*.py"))
    for path in files:
        module = f"tests.{path.stem}"
        print(f"\n===== {module} =====", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", module], cwd=str(ROOT), check=False)
        if result.returncode:
            failed.append(module)
    if failed:
        print("\nFAILED: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"\nALL PASS: Python test modules {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
