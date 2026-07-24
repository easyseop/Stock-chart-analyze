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
#   BEACON_UNITS        점검 유닛(기본 "sentinel buyloop telegram portfolio-web watchdog")
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
UNITS="${BEACON_UNITS:-sentinel buyloop telegram portfolio-web watchdog}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR" 2>/dev/null || true

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
host="$(hostname 2>/dev/null || echo '?')"
sha="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# 유닛 가동상태(시스템 유닛 읽기는 권한 불필요)
#   · is-active는 inactive여도 상태를 찍고 exit!=0 → `|| echo`를 겹치면 두 줄이
#     된다(실측 "inactive\nunknown"). head -1로 한 값만.
#   · 미설치 유닛(unit file 없음)은 감시 대상에서 제외 — 선택 구성요소(예:
#     watchdog)가 영구 down=1로 잡혀 진짜 장애와 구분 안 되던 것 방지.
unit_json=""
down=0
for u in $UNITS; do
  if ! systemctl cat "$u" >/dev/null 2>&1; then
    unit_json="${unit_json}\"${u}\":\"not_installed\","
    continue                          # 미설치 = down으로 세지 않음
  fi
  st="$(systemctl is-active "$u" 2>/dev/null | head -1)"
  [ -z "$st" ] && st=unknown
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
#   -o cat: 매치 0건일 때 "-- No entries --" 1줄이 세어지던 오탐 방지(실측 2026-07-22).
err1h="$(journalctl $(for u in $UNITS; do printf -- '-u %s ' "$u"; done) \
          --since '1 hour ago' -p err --no-pager -o cat 2>/dev/null | wc -l | tr -d ' ')"
[ -z "$err1h" ] && err1h=-1

# 원격 진단 필드(2026-07-22 매수 0건·알림 누락 조사) — 값이 아닌 '상태'만:
#   tg_env      : 이 env(kis.env)에 텔레그램 토큰·챗ID가 있는가(존재 여부만, 값 미발행)
#   buyloop_tail: 최근 14시간 buyloop 저널 꼬리(게이트 판정 라인) — 왜 안 샀는지
#   err_last    : 최근 24시간 마지막 에러 라인 — err_1h의 정체
tg_env=0
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] && tg_env=1
# 각 프로세스가 '실제로' 특정 env 키를 물고 있나(/proc environ) — 매수 알림
#   누락 진단(2026-07-23). buyloop이 0인데 telegram이 1이면 원인 확정.
#   인자: $1=프로세스 패턴, $2=env 키(기본 TELEGRAM_BOT_TOKEN). 값이 아닌 존재여부만.
_key_in_proc() {
  local pid key; pid="$(pgrep -f "$1" 2>/dev/null | head -1)"
  key="${2:-TELEGRAM_BOT_TOKEN}"
  { [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; } || { echo '"?"'; return; }
  if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -q "^${key}="; then
    echo 1; else echo 0; fi
}
tg_buyloop="$(_key_in_proc bot.kis_buyloop)"
tg_teleg="$(_key_in_proc bot.kis_telegram)"
# ★ 새 일일손실 서킷브레이커(2026-07-24)는 seed 미로드 시 신규매수를 전면 차단한다.
#   → buyloop 프로세스가 BOT_SEED_KRW를 실제로 물고 있는지 상시 자가점검(값 미발행).
#   seed_buyloop=0이면 systemd EnvironmentFile이 export형식을 못 읽는 등 env 유실 →
#   매수가 조용히 멎기 전에 원격에서 즉시 감지 가능.
seed_buyloop="$(_key_in_proc bot.kis_buyloop BOT_SEED_KRW)"
seed_env=0
[ -n "${BOT_SEED_KRW:-}" ] && seed_env=1
#   개별 신호 판정 라인(A: '  · CODE', B: '  [B] · CODE')만 — 장외노이즈 제외.
bl_tail="$(journalctl -u buyloop --since '16 hours ago' --no-pager -o cat 2>/dev/null \
           | grep -E '^[[:space:]]+(\[B\] )?(✓|·)' | grep -v '\[session\]' | tail -12)"
#   게이트 히스토그램(16시간 집계) — 어느 게이트가 몇 건 걸렀는지 총계. 꼬리가
#   최근 노이즈에 밀려도 밤새 판정 분포는 이 한 줄로 확정된다.
gate_hist="$(journalctl -u buyloop --since '16 hours ago' --no-pager -o cat 2>/dev/null \
             | grep -oE '\[[a-z_]+\]' | sort | uniq -c | sort -rn | head -8 \
             | awk '{printf "%s=%s ", $2, $1}')"
err_last="$(journalctl $(for u in $UNITS; do printf -- '-u %s ' "$u"; done) \
            --since '24 hours ago' -p err --no-pager -o cat 2>/dev/null | tail -2)"
#   n_open=12 부풀림 판별(2026-07-22): baseline 파일 상태 + 원장 in-flight BUY 수.
#   전부 로컬 파일 읽기(API 0회) — 심볼은 KIS 종목코드뿐(수량·금액 미발행).
diag="$(python3 -c '
import os, sys, json, time
sys.path.insert(0, ".")
out = {}
try:
    from bot import ownership
    bp = ownership._bpath()
    if os.path.exists(bp):
        st = os.stat(bp)
        try: n = len(json.load(open(bp)).get("symbols", []))
        except Exception: n = -1
        out["baseline"] = {"path": bp, "n": n,
                           "age_h": round((time.time()-st.st_mtime)/3600, 1)}
    else:
        out["baseline"] = {"path": bp, "missing": True}
except Exception as e:
    out["baseline"] = {"err": type(e).__name__}
try:
    from bot import ledger
    f = ledger._fold()
    inflight = [k for k, v in f.items()
                if (v.get("side") or "").upper() == "BUY"
                and v.get("state") in ledger._INFLIGHT]
    out["inflight_buys"] = {"n": len(inflight),
                            "syms": sorted({str(f[k].get("symbol") or "")
                                            for k in inflight})[:15]}
    # 원장 전체 요약 — 봇이 역사적으로 뭘 샀/팔았나(수량·금액 미발행, 심볼만)
    bought = sorted({str(v.get("symbol") or "") for v in f.values()
                     if (v.get("side") or "").upper() == "BUY"
                     and int(v.get("filled") or 0) > 0})
    sold = sorted({str(v.get("symbol") or "") for v in f.values()
                   if (v.get("side") or "").upper() == "SELL"
                   and int(v.get("filled") or 0) > 0})
    out["ledger_hist"] = {"buy_filled": bought[:20], "sell_filled": sold[:20]}
except Exception as e:
    out["inflight_buys"] = {"err": type(e).__name__}
try:
    # 봇 자체 포지션 기록(매수 시 기록·매도 시 close) — 봇이 아는 자기 보유
    import collections
    opens = {}
    with open("bot/kis_positions.jsonl", encoding="utf-8") as fp:
        for line in fp:
            try: e = json.loads(line)
            except Exception: continue
            c = str(e.get("code") or "").upper()
            if e.get("ev") == "close": opens.pop(c, None)
            elif e.get("ev") == "open" and c: opens[c] = e   # 최신 open 이벤트 보존
    out["kis_positions_open"] = sorted(opens)[:20]
    # 봇 투입 총액 추정(진입가×수량, USD는 FX 환산) — 합계 1개 숫자만 발행
    #   (SEED 총량 게이트 검증용, 사용자 요청 2026-07-22). 개별 금액 미발행.
    try:
        from bot import settings as _st
        fxv = float(getattr(_st, "FX_USDKRW", 1380.0))
    except Exception:
        fxv = 1380.0
    tot = 0.0
    for c, e in opens.items():
        try:
            q = float(e.get("qty") or 0); px = float(e.get("entry") or 0)
            tot += q * px * (fxv if e.get("ccy") == "USD" else 1.0)
        except Exception:
            pass
    out["bot_cost_krw_est"] = round(tot)
except FileNotFoundError:
    out["kis_positions_open"] = "no_file"
except Exception as e:
    out["kis_positions_open"] = type(e).__name__
print(json.dumps(out, ensure_ascii=False)[:600])' 2>/dev/null)"
[ -z "$diag" ] && diag='{}'
extra="$(BL="$bl_tail" EL="$err_last" GH="$gate_hist" python3 -c '
import os, json
trim = lambda s, n=6: [l[:200] for l in (s or "").splitlines()[-n:]]
print(json.dumps({"buyloop_tail": trim(os.environ.get("BL",""), 12),
                  "gate_hist": (os.environ.get("GH","") or "").strip()[:300],
                  "err_last": trim(os.environ.get("EL",""), 2)},
                 ensure_ascii=False)[1:-1])' 2>/dev/null)"
[ -z "$extra" ] && extra='"buyloop_tail":[],"gate_hist":"","err_last":[]'

body="{\"ts\":\"$now\",\"host\":\"$host\",\"sha\":\"$sha\",\"units\":$unit_json,\"down\":$down,\"ledger_lines\":${lines:-0},\"last_ledger\":\"${last_led}\",\"err_1h\":${err1h},\"tg_env\":${tg_env},\"tg_buyloop\":${tg_buyloop},\"seed_env\":${seed_env},\"seed_buyloop\":${seed_buyloop},\"diag\":${diag},${extra}}"

prio="default"; tags="hospital"
[ "$down" -gt 0 ] && { prio="high"; tags="rotating_light"; }

curl -s -m 10 \
  -H "Title: stock-health ${host} (down=${down})" \
  -H "Priority: ${prio}" -H "Tags: ${tags}" \
  -d "$body" "${BASE}/${TOPIC}" >/dev/null 2>&1 || true
