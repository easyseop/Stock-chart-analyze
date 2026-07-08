#!/usr/bin/env bash
# 계좌 상태 영구 백업(state 브랜치) + 매매 락 해제 — daily.yml 두 차선(build/fast) 공용.
#
#   매 빌드 후 스냅샷 + 월별 append-only 원장 + 기계용 feed를 state 브랜치에 커밋.
#   cache 밖 source of truth — 복구 순서: 본파일 → .bak → state 스냅샷 → 새 계좌.
#
#   ★ 매매 락(autopaper._trading_lock_status)이 걸어둔 state/trading.lock을
#     스냅샷과 '같은 커밋'에서 제거한다 — 매매→스냅샷 보존까지가 임계구역이라
#     해제가 스냅샷 push와 원자적이어야 다른 차선이 낡은 계좌로 매매하지 않는다.
#   ★ push 경합(다른 런의 락/백업 커밋이 먼저 붙음) 대비 — 처음부터 다시
#     만들어 최대 3회 재시도(rebase 대신 재생성: 스냅샷은 전체 파일 덮어쓰기라
#     재생성이 충돌 없이 단순·안전).
#
#   실패해도 빌드는 죽이지 않는다(::warning + 연속 실패 카운터, 2회+ 텔레그램).
set +e

ok=0
for attempt in 1 2 3; do
  (
    set -e
    [ -f data_cache/autopaper.json ] || { echo "상태 파일 없음 — 백업 생략"; exit 0; }
    git config user.name "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    git worktree remove --force /tmp/st 2>/dev/null || true
    rm -rf /tmp/st
    if git fetch -q origin state 2>/dev/null; then
      git worktree add -q /tmp/st FETCH_HEAD
    else
      git worktree add -q --detach /tmp/st
      git -C /tmp/st checkout -q --orphan state
      git -C /tmp/st rm -rf . >/dev/null 2>&1 || true
    fi
    mkdir -p /tmp/st/state/ledger /tmp/st/feed
    cp data_cache/autopaper.json /tmp/st/state/autopaper.snapshot.json
    # 기계용 feed 미러 — 알림봇·파수꾼·guard가 Pages 대신 1순위로 읽는 경로
    cp public/api/signals.json    /tmp/st/feed/signals.latest.json    2>/dev/null || true
    cp public/api/paper_auto.json /tmp/st/feed/autopaper.public.json  2>/dev/null || true
    # 매매 락 해제 — 이 커밋이 곧 "매매+스냅샷 완료" 선언(autopaper 락과 한 쌍)
    rm -f /tmp/st/state/trading.lock
    python3 - <<'PY'
import json, os, datetime
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
hb = {"generated_at": now.isoformat(timespec="seconds"),
      "run_id": os.environ.get("GITHUB_RUN_ID"),
      "event": os.environ.get("GITHUB_EVENT_NAME"),
      "lane": os.environ.get("LANE", "bulk")}
json.dump(hb, open("/tmp/st/feed/heartbeat.json", "w"))
try:
    out = json.load(open("public/api/paper_auto.json"))
    led = {"ts": hb["generated_at"], "run_id": hb["run_id"],
           "lane": hb["lane"],
           "cash": out.get("cash"), "equity": out.get("equity"),
           "ret_pct": out.get("ret_pct"),
           "positions": len(out.get("positions", [])),
           "pending": len(out.get("pending", [])),
           "trades": out.get("trades")}
    path = f"/tmp/st/state/ledger/{now.strftime('%Y-%m')}.jsonl"
    with open(path, "a") as fp:
        fp.write(json.dumps(led, ensure_ascii=False) + "\n")
except Exception as e:
    print("원장 기록 생략:", e)
PY
    git -C /tmp/st add -A
    if git -C /tmp/st diff --cached --quiet; then
      echo "변경 없음 — 커밋 생략"
    else
      git -C /tmp/st commit -q -m "state: $(date -u +%FT%TZ) run=${GITHUB_RUN_ID} lane=${LANE:-bulk} [skip ci]"
      git -C /tmp/st push -q origin HEAD:state
      echo "상태 백업 완료 → state 브랜치 (시도 ${attempt})"
    fi
  )
  rc=$?
  git worktree remove --force /tmp/st 2>/dev/null || true
  if [ $rc -eq 0 ]; then ok=1; break; fi
  echo "백업 시도 ${attempt} 실패(경합/일시 오류) — 재시도"
  sleep 3
done

# 연속 실패 카운터(cache 영속) — 계좌 원본은 cache+.bak에 안전하나,
#   백업이 '조용히' 계속 실패하면 원장·복구층이 낡는다. 2연속+면 경보.
FC=data_cache/.backup_fails
if [ $ok -eq 1 ]; then
  echo 0 > "$FC"
else
  n=$(( $(cat "$FC" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$FC"
  echo "::warning::계좌 상태 백업 실패(${n}회 연속) — 빌드는 계속, 다음 빌드 재시도"
  if [ "$n" -ge 2 ] && [ -n "${TG_TOKEN}" ] && [ -n "${TG_CHAT}" ]; then
    curl -sf -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT}" \
      -d "text=🟠 계좌 상태 백업 ${n}회 연속 실패 — state 브랜치 원장/복구층이 낡는 중. 매매·계좌 원본(cache+.bak)은 정상. 지속되면 확인 필요." >/dev/null || true
  fi
fi
exit 0
