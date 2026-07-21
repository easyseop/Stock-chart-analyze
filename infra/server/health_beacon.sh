#!/usr/bin/env bash
# 서버 헬스 비콘 — 봇 운영상태를 ntfy 토픽에 주기 발행(원격 세션에서 읽기용).
#
#   목적: 격리된 원격(웹) 세션은 이 서버에 SSH가 안 된다. 서버가 자기 상태를
#     ntfy에 올려두면, 원격에서 그 토픽을 HTTPS로 읽어 봇 가동여부·마지막
#     사이클·에러수를 확인할 수 있다.
#   ★ 민감정보(잔고·보유수량·계좌번호) 발행 금지 — 운영 헬스만. ntfy 토픽은
#     이름을 알면 누구나 읽을 수 있으니(추측 어려운 문자열 권장) 금전정보 배제.
#
# 환경변수(kis.env 등에서 주입 — BEACON_ENV로 소스):
#   NTFY_HEALTH_TOPIC   발행 토픽(필수, 미설정=무동작). 예: stock-health-<랜덤>
#   NTFY_BASE           기본 https://ntfy.sh
#   BEACON_UNITS        점검 유닛(기본 "sentinel buyloop telegram watchdog")
#   BEACON_ENV          env 파일(기본 /etc/stock/kis.env) — export 형식이어도 OK
#
# 읽기(원격): curl -s "https://ntfy.sh/<TOPIC>/json?poll=1"  (최근 캐시 메시지)
set -uo pipefail

ENVF="${BEACON_ENV:-/etc/stock/kis.env}"
# kis.env가 `export KEY=val` 형식이어도 bash source면 정상 로드(systemd
#   EnvironmentFile은 export를 못 읽는다 — autodeploy와 동일 패턴).
# shellcheck disable=SC1090
[ -f "$ENVF" ] && . "$ENVF" 2>/dev/null || true

TOPIC="${NTFY_HEALTH_TOPIC:-}"
[ -z "$TOPIC" ] && exit 0                       # 토픽 미설정 = 비콘 비활성(무해)
BASE="${NTFY_BASE:-https://ntfy.sh}"
UNITS="${BEACON_UNITS:-sentinel buyloop telegram watchdog}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR" 2>/dev/null || true

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
host="$(hostname 2>/dev/null || echo '?')"
sha="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# 유닛 가동상태(시스템 유닛 읽기는 권한 불필요)
unit_json=""
down=0
for u in $UNITS; do
  st="$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
  [ "$st" != "active" ] && down=$((down+1))
  unit_json="${unit_json}\"${u}\":\"${st}\","
done
unit_json="{${unit_json%,}}"

# 원장 최신 이벤트 시각·라인수(있으면) — 매매 파이프라인 생존 신호
led="bot/order_ledger.jsonl"
if [ -f "$led" ]; then
  lines="$(wc -l < "$led" 2>/dev/null | tr -d ' ')"
  last_led="$(tail -1 "$led" 2>/dev/null | python3 -c 'import sys,json
try:
    d=json.loads(sys.stdin.read() or "{}"); print(d.get("ts") or "")
except Exception:
    print("")' 2>/dev/null)"
else
  lines=0; last_led=""
fi

# 최근 1시간 에러 로그 수(best-effort; 권한 없으면 -1)
err1h="$(journalctl $(for u in $UNITS; do printf -- '-u %s ' "$u"; done) \
          --since '1 hour ago' -p err --no-pager 2>/dev/null | wc -l | tr -d ' ')"
[ -z "$err1h" ] && err1h=-1

body="{\"ts\":\"$now\",\"host\":\"$host\",\"sha\":\"$sha\",\"units\":$unit_json,\"down\":$down,\"ledger_lines\":${lines:-0},\"last_ledger\":\"${last_led}\",\"err_1h\":${err1h}}"

prio="default"; tags="hospital"
[ "$down" -gt 0 ] && { prio="high"; tags="rotating_light"; }

curl -s -m 10 \
  -H "Title: stock-health ${host} (down=${down})" \
  -H "Priority: ${prio}" -H "Tags: ${tags}" \
  -d "$body" "${BASE}/${TOPIC}" >/dev/null 2>&1 || true
