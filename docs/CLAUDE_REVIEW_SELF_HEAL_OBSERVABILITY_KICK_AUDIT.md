# Claude 적대 검토 요청 — 자가복구 관찰 완화·판정 관측성·외부 킥 감사

- 기준 브랜치/커밋: `claude/happy-gauss-cwoq21` @ `670dcce`
- 검토 브랜치: `codex/self-heal-observability-kick-audit`
- 구현 체크포인트: `069a5a2`, 권한 경계 보완: `4fbc7d6`
- 작성: Codex, 2026-08-17
- 금지선: 이 검토에서 P0/P1=0이고 사용자가 별도로 승인하기 전 병합·Oracle
  배포·kill 하향을 하지 않는다.

## 1. 구현 요약

### T1 — 연속성 완화와 만성 저하 방어

`bot/kill_self_heal.py`가 heartbeat를 세 구간으로 판정한다.

1. `0~60초`: 건강 표본. 기존 30분 관찰을 진행한다.
2. `60초 초과~90초 이하`: 첫 단발은 `degraded`로 기록하고 기존
   `healthy_since`를 유지하되 건강 시각은 갱신하지 않는다. 연속 2회면 즉시
   리셋한다.
3. `90초 초과`, 음수, 비수, 무한대, `None`: 즉시 리셋한다.

추가 적대 판단: 건강·단발 저하가 번갈아 나타나면 “연속 2회”만으로는 30분이
채워질 수 있다. 그래서 한 관찰창의 60~90초 단발을 최대 4회까지만 허용하고
5번째에 리셋한다. 기본값을 환경변수로 더 느슨하게 만들 수 없도록 reset age는
최대 90초, 단발 예산은 최대 4로 상한을 고정했다. 더 엄격한 값만 허용한다.

readiness GO, KST 하루 1회, L2+ 금지, 정확한 source/reason 완전일치, readiness
중 kill TOCTOU 재검사, 상태 손상·쓰기 실패 fail-closed는 유지했다. 특히 관찰
상태를 디스크에 성공적으로 쓰기 전에는 readiness 호출도 하지 않는다.

### T2 — 판정 노출·즉시 로그·알림 히스테리시스

- `cycle()`의 모든 결과는 `action`, `why`, `observed_s`, `remaining_s`,
  `used_today`를 가진다. readiness 예외도 `readiness:<Exception>`으로 남는다.
- watchdog은 `(action, why)`가 바뀌면 즉시, 그대로면 10분마다 한 번만
  `observed/remaining/used_today`를 `flush=True`로 기록한다.
- P0와 회복 텔레그램만 각각 연속 2표본 뒤에 보낸다. sentinel 재시작·L1 상향
  판정 코드는 기존 위치와 임계(90/120초)를 바꾸지 않았다. 전송 실패는 래치를
  잠그지 않아 다음 표본에서 재시도한다.
- watchdog/sentinel/buyloop/telegram 유닛에 `PYTHONUNBUFFERED=1`을 넣었다.
- 핵심 상태는 기존처럼 0600이다. watchdog(root)과 ops/telegram(bot)의 사용자
  차이 때문에 그대로 읽으면 권한 오류가 나므로, action/why/초/당일사용 여부만
  담은 별도 0644 원자 스냅샷을 발행한다. event, PID, pending notice, kill 원문,
  심볼, 수량, 금액, 시크릿은 넣지 않는다.
- ops에는 `self_heal` 객체를, `/진단`에는 L1일 때만 `자가복구: 관찰 ...` 한 줄을
  추가했다. 둘 다 공개 스냅샷을 읽기만 하며 state machine을 진행하지 않는다.

### T3 — 외부 킥 계층 조용한 죽음 감사

- `scripts/kick_audit.py`는 `daily.yml`의 최근 24시간 실행만 GitHub GET API로
  조회한다. 주문·kill·workflow dispatch 경로는 없다.
- `workflow_dispatch`이면서 actor가 명시적으로 bot이 아니면 외부,
  `schedule`은 GitHub 크론, bot dispatch는 내부 재실행으로 별도 집계한다.
  actor 결손은 외부 생존 증거로 세지 않는다.
- `created>=`, `per_page=100`, 최대 5페이지를 사용한다. 어느 페이지든 HTTP/
  JSON/행 계약 실패 또는 페이지 상한 도달이면 전체 `None`이다. 부분 결과로
  무발사를 판정하지 않는다.
- 외부 0, schedule 0, 둘 다 0의 세 판정을 텔레그램으로 보내고 UTC 날짜·판정별
  하루 1회 래치한다. 정상은 로그만 남긴다. API 실패는 `unknown` 로그만 남기고
  알림 0이다.
- 기존 `freshness-watchdog`에 checkout/audit/cache step만 더했다. 별도 workflow나
  cron은 만들지 않았다. 감사 step은 `continue-on-error`이며 기존 신선도 복구와
  독립이다. 캐시는 경보 전송 후 래치가 실제 변경됐을 때만 저장한다.

## 2. 반드시 반증할 질문

1. 60~90초 단발 뒤 정상 표본이 이어지면 30분 관찰이 유지되는가?
2. 90초 초과 1회, 60초 초과 연속 2회, 건강/저하 교대의 누적 5번째가 각각
   readiness 전에 관찰을 리셋하는가?
3. `SELF_HEAL_RESET_AGE_S=9999`, `SELF_HEAL_MAX_SOFT_SAMPLES=9999`로 안전선을
   완화할 수 없는가?
4. operator reason, 부분/접두/공백 reason, L2+, readiness NO-GO/예외, 하루 1회
   소진, 상태 손상/쓰기 실패에서 L0가 0건인가?
5. 알림 히스테리시스가 notification만 늦추고 90초 restart, 120초+ 상향 조건과
   deploy grace를 바꾸지 않았는가?
6. 공개 판정 파일이 root→bot 권한 경계에서 읽히면서 핵심 0600 파일과 민감정보
   경계를 유지하는가? 공개 파일 위조/손상은 복구를 진행시키지 않는가?
7. `/진단`과 ops가 `cycle()` 또는 주문/kill mutation을 호출하지 않는가?
8. GitHub API 두 번째 페이지 실패, 반복/상한, actor 결손, malformed row가 외부
   생존 또는 0건으로 세탁되지 않는가?
9. 같은 날 workflow가 여러 번 실행돼도 같은 경보가 1회인가? cache miss/손상은
   매매나 재실행에 영향을 주지 않는가?
10. `kick_audit.py`나 workflow 추가가 `daily.yml`을 dispatch하거나 기존 freshness
    자동 재실행 경로를 약화하지 않는가?

## 3. 테스트 결과

현재 최종 작업트리에서 다음을 통과했다.

- Python 독립 회귀: `60/60 PASS`
- Node 계산: `19/19 PASS`
- 집중: self-heal `14/14`, watchdog observability `4/4`, kick audit `8/8`,
  deploy grace `7/7`, ops/telegram 전체 통과
- `python -m compileall -q bot scanner scripts tests`: exit `0`
- 두 JavaScript `node --check`: exit `0`
- `git diff --check`: exit `0`

## 4. 뮤테이션 증거

구현을 체크포인트 커밋한 뒤 각각 독립 적용·실행·복원했다. 모두 종료코드 1로
KILLED됐다.

| 변이 | 제거한 방어 | 실패 테스트(원문 이름) | exit |
|---|---|---|---:|
| M1 | 90초 hard reset 상한 제거 | `test_t1_hard_age_resets_immediately` | 1 |
| M2 | soft 연속 2회→3회로 완화 | `test_s2_29_minutes_flap_and_restart_reset` | 1 |
| M3 | 관찰창 soft 누적 예산 제거 | `test_t1_chronic_alternating_soft_samples_never_recovers` | 1 |
| M4 | 경보 확인 2표본→1표본 | `test_heartbeat_alert_and_recovery_require_two_consecutive_samples` | 1 |
| M5 | self-heal 10분 로그 억제 제거 | `test_self_heal_log_on_change_and_every_ten_minutes` | 1 |
| M6 | ops `self_heal` 필드 제거 | `test_snapshot_shape_and_no_secrets` (`KeyError`) | 1 |
| M7 | API 실패 `unknown` 분기 제거 | `test_api_failure_is_unknown_and_never_alerts` | 1 |
| M8 | kick 하루 1회 래치 제거 | `test_same_verdict_is_latched_once_per_day` | 1 |
| M9 | 2페이지 실패를 빈 성공으로 변경 | `test_fetch_paginates_and_partial_failure_is_unknown` | 1 |
| M10 | telegram unbuffered 설정 제거 | `test_long_running_python_units_are_unbuffered` | 1 |
| M11 | root→bot 공개 판정 발행 제거 | `test_normal_path_lowers_and_audits_self_heal` | 1 |
| M12 | 상태 쓰기 실패 후 readiness 진행 허용 | `test_state_write_failure_blocks_before_readiness_and_lower` | 1 |

## 5. 배포 전후 금지선·수동 확인

이 PR은 주문 로직·readiness 내용·kill 상향/하향 허용 사유를 바꾸지 않는다.
승인 후 배포해도 autodeploy 기본 목록에는 watchdog이 없으므로 장외에 유닛을
설치하고 `sudo systemctl restart watchdog`이 필요하다. sentinel/buyloop/telegram도
새 unbuffered 환경을 읽도록 한 번 재시작한다. `journalctl -u watchdog -f -o
short-iso`에서 로그가 각 관찰 시각에 즉시 기록되는지 확인한다.

첫 오인 L1 자동복구 실측 전까지 L1을 인위적으로 만들거나 L0를 수동/자동으로
내리는 작업은 이 PR의 범위가 아니다. Claude 판정 P0/P1=0과 사용자 별도 승인 전
병합·Oracle 배포를 금지한다.
