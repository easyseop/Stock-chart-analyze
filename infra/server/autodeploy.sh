#!/usr/bin/env bash
# 자동 배포 — 원격 브랜치에 새 커밋이 오면 pull + 봇 재시작. 변경 없으면 무동작.
#
#   · fast-forward만 허용 — 서버에 로컬 커밋/수정이 있어 충돌하면 아무것도 안
#     바꾸고 P0 알림(수동 개입 필요). 봇 코드가 반쯤 섞인 상태로 뜨는 일 없음.
#   · 배포 전 임포트 스모크 — 새 코드가 임포트조차 안 되면 즉시 롤백하고 기존
#     코드로 계속 동작(+P0 알림). 깨진 배포로 손절 감시가 죽는 사고 방지.
#   · 재시작은 실제 새 커밋이 있을 때만 — 파수꾼 공백은 부팅 대사가 즉시 복구.
#
# systemd timer(autodeploy.timer)가 주기 실행. 유닛의 User 계정이
#   `sudo systemctl restart <services>`를 비번 없이 실행할 수 있어야 한다
#   (sudoers drop-in — infra/server/README.md 참조).
# 환경변수:
#   AUTODEPLOY_SERVICES  재시작 대상(기본 "sentinel buyloop telegram")
#   AUTODEPLOY_ENV       알림용 env 파일(기본 $HOME/kis.env — TELEGRAM_* 사용)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SERVICES="${AUTODEPLOY_SERVICES:-sentinel buyloop telegram}"
ENVF="${AUTODEPLOY_ENV:-$HOME/kis.env}"

notify() {  # $1=메시지 $2=1이면 critical(ntfy 이중화) — 실패해도 배포는 계속
  ( set +e
    [ -f "$ENVF" ] && . "$ENVF"
    python3 -c 'import sys; from bot import notify; notify.send(sys.argv[1], critical=(len(sys.argv) > 2 and sys.argv[2] == "1"))' "$1" "${2:-0}"
  ) >/dev/null 2>&1 || true
}

git fetch origin "$BRANCH" --quiet
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
[ "$LOCAL" = "$REMOTE" ] && exit 0            # 변경 없음 — 아무것도 안 함

if ! git merge --ff-only "origin/$BRANCH" --quiet >/dev/null 2>&1; then
  notify "🚨 자동배포 보류 — 서버 로컬 변경과 충돌(fast-forward 불가). 수동 pull 필요." 1
  exit 1
fi

if ! python3 -c "import importlib
for m in ('bot.kis', 'bot.kis_boot', 'bot.kis_buyloop', 'bot.sentinel',
          'bot.kis_telegram', 'bot.kis_reconcile', 'bot.rollout'):
    importlib.import_module(m)" >/dev/null 2>&1; then
  git reset --hard "$LOCAL" --quiet
  notify "🚨 자동배포 실패(임포트 오류) — ${REMOTE:0:7} 롤백, 기존 코드로 계속 동작. 수동 확인 필요." 1
  exit 1
fi

# shellcheck disable=SC2086  # SERVICES는 의도적 워드 스플릿(여러 유닛)
sudo systemctl restart $SERVICES
notify "🔄 서버 자동배포 — $(git log -1 --format='%h %s') · 재시작: $SERVICES"
