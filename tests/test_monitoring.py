"""감시자도 코드다 — 모니터가 조용히 망가지는 것을 CI에서 잡는다.

과거 실측(A7): 배포 라이브 스모크가 grep하던 마커를 UI 개편이 지워 → 모든 배포가
빨강, 진짜 실패와 구분 불가. 그런 드리프트를 PR 단계에서 실패시킨다.

  1) 배포 템플릿(index.html)에 스모크 마커가 실제로 존재한다
  2) daily.yml 라이브 스모크 스텝이 '그' 마커들을 grep한다(마커 상수와 워크플로
     grep이 어긋나면 실패 — 둘 다 scanner/markers.py를 단일 출처로 삼게 강제)

실행: python -m tests.test_monitoring
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.markers import LIVE_SMOKE_MARKERS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fp:
        return fp.read()


def test_template_contains_markers():
    html = _read(os.path.join("scanner", "templates", "index.html"))
    missing = [m for m in LIVE_SMOKE_MARKERS if m not in html]
    assert not missing, (
        f"index.html에서 스모크 마커 사라짐: {missing} — 배포 스모크가 "
        f"영구 빨강이 된다(A7). 템플릿을 되돌리거나 scanner/markers.py를 갱신.")
    print(f"[PASS] 템플릿에 스모크 마커 {len(LIVE_SMOKE_MARKERS)}개 모두 존재")


def test_workflow_greps_markers():
    wf = _read(os.path.join(".github", "workflows", "daily.yml"))
    missing = [m for m in LIVE_SMOKE_MARKERS if m not in wf]
    assert not missing, (
        f"daily.yml 라이브 스모크가 grep하지 않는 마커: {missing} — 마커 상수와 "
        f"워크플로가 어긋남. 둘 다 scanner/markers.py를 따르게 맞출 것.")
    print(f"[PASS] daily.yml 스모크가 마커 {len(LIVE_SMOKE_MARKERS)}개 모두 검사")


if __name__ == "__main__":
    test_template_contains_markers()
    test_workflow_greps_markers()
    print("\n✅ 모니터 회귀 방지 통과 — 스모크 마커 드리프트를 PR에서 차단.")
