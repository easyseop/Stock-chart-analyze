"""새 읽기 전용 웹앱을 기존 Pages 산출물의 ``/app`` 아래에 추가한다.

중요: 이 모듈은 ``public/api``를 읽거나 쓰지 않는다. 매매 봇과 기존 사이트가
사용하는 JSON 계약은 ``scanner.screener``가 계속 단독으로 발행하고, 여기서는
검증된 정적 자산만 별도 디렉터리로 복사한다.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


SOURCE_DIR = Path(__file__).with_name("site_app")
ASSETS = ("index.html", "app.css", "app.js", "og.png", "og-v2.png")


def publish(out_dir: str = "public") -> str:
    """정적 웹앱 자산을 ``<out_dir>/app``에 additive하게 발행한다."""
    target = Path(out_dir) / "app"
    target.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        src = SOURCE_DIR / name
        if src.exists():
            shutil.copy2(src, target / name)
    return str(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="읽기 전용 정적 웹앱 발행")
    parser.add_argument("--out-dir", default="public")
    args = parser.parse_args()
    print(publish(args.out_dir))


if __name__ == "__main__":
    main()
