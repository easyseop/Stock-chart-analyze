# Claude 적대 검토 요청 — quiesce P1 재검증 + KIS 장애 오귀속 제거

작성: Codex, 2026-08-21  
브랜치: `codex/kis-outage-classification-v2`  
기준: `b5b5135f9f531c0a347efa287c73547ae492566a`  
구현 커밋: `a5d2acea`, `a248d578`

## 1. `b5b5135` quiesce P1 역검토 재검증

직전 지적이었던 `multi-user.target` 재부팅 부활 경로를 다시 확인했다.

- guardian 대체 증명은 `sentinel.service`와 `buyloop.service`가 정확히
  `disabled`일 때만 허용한다. 부팅 링크가 남은 `enabled`는 guardian 둘이
  `inactive`여도 거부한다.
- guardian(`watchdog`, `autodeploy.timer`)은 정확히 `inactive`만 인정한다.
  `deactivating`, `failed`, `unknown`은 모두 거부한다.
- 기존 `masked` 경로는 그대로 유지한다.
- `python3 -m tests.test_legacy_migration`, `compileall`, `git diff --check` 통과.

Codex 판정은 **P0 0 · P1 0**이다. CVNA apply 당시 재부팅은 없었으므로 과거
apply 결과에는 영향이 없고, 이 수정은 향후 quiesce 사용을 보호한다.

Claude 확인 요청:

1. `multi-user.target` 외 다른 enablement target/alias가 disabled 증명을 우회하지
   않는지.
2. transient systemd 상태가 정지 완료로 오인되지 않는지.
3. 기존 masked 배치가 퇴행하지 않았는지.

## 2. KIS 장애 분류 구현 요약

구현지시서: `docs/CODEX_SPEC_KIS_OUTAGE_CLASSIFICATION.md`

### T1. watchdog 상향 전 KIS 장애 분류

- 별도 프로세스인 watchdog이 볼 수 있도록 기존 `balance_health` 상태 파일에
  `consecutive_failures`, `last_failure_at`, `last_success_at`, `last_cause`를
  원자적으로 기록한다. 주문 원장이나 새 배관은 추가하지 않았다.
- 최근 5분 안에 KIS 실패가 3회 이상 연속됐을 때만 KIS outage로 인정한다.
- KIS outage이면 무의미한 sentinel 재시작을 생략하고, hard-disable 상향에는 기존
  정확 문자열 `BALANCE_FAILURE_REASON`을 사용한다.
- 분류 파일 없음·손상·비정상 타입·미래 시각·노후·원인 문자열 오염은 전부 분류
  실패로 처리하여 기존 재시작→`HEARTBEAT_EXHAUSTED_REASON` 경로를 보존한다.
- watchdog도 독립적으로 `last_failure_age_s <= 300`을 다시 검사한다.
- 알림과 self-heal 로그에 연속 실패 수와 최근 원인을 남긴다. 심볼·금액·시크릿은
  기록하지 않는다.

### T2. 사유별 자가복구 상한

- `BALANCE_FAILURE_REASON`: KST 하루 2회.
- `HEARTBEAT_EXHAUSTED_REASON`: 기존대로 KST 하루 1회.
- 기존 30분 관찰, readiness GO, L2+ 금지, operator 상향 금지, 사유 완전일치
  allowlist, 상태 손상 fail-closed는 유지했다.
- 구버전 `used=true` 상태는 모든 사유의 당일 한도를 이미 소진한 것으로 이관한다.
  업그레이드가 새 자동 하향 예산을 만들지 않는다.
- 사유별 카운터는 정확한 정수만 허용하며 bool/float/string/범위 초과는 손상으로
  처리한다.

### T3. 타임아웃 증폭 축소

- KIS 읽기 `_get`이 timeout이면 다음 읽기 timeout만 `15s → 5s`로 낮춘다.
  정상 응답 1회 뒤 15초로 복원한다.
- token·주문·취소 timeout과 전역 상수는 바꾸지 않았다.
- 파수꾼은 `broker.holdings()`와 각 `broker.quote()` 직전에 `_beat()`를 기록한다.
  별도 heartbeat 스레드는 없으며, 블로킹 호출이 멈추면 heartbeat도 다시 멈춘다.

## 3. 실행 검증

- 전체 Python: `ALL PASS: Python test modules 70`
- 웹 계산: `19/19 PASS`
- 집중 테스트: `KIS outage classification 7/7 PASS`
- 기존 자가복구: `kill self-heal 15/15 PASS`
- 기존 경보 위생: `H1-H3 7/7 + H4 4/4 PASS`
- watchdog 관측성: `4/4 PASS`
- 레거시 이관, KIS 어댑터, kill-switch, 파수꾼 heartbeat 테스트 통과
- `python3 -m compileall -q bot infra tests`, 실제 경로
  `node --check scanner/site_app/app.js`, `git diff --check` 통과

시스템 Python에는 pandas/numpy가 없어 전체 스위트가 의존성 오류를 냈고, 저장소
코드 문제가 아닌 것을 확인한 뒤 Codex 번들 Python으로 70개 전부 재실행했다.

## 4. mutation 증거

구현 체크포인트 `a5d2acea`를 먼저 커밋한 뒤 각각 독립 적용·실행·원복했다.

| 변이 | 기대 방어 | 결과 |
|---|---|---|
| M1 KIS outage 판별을 항상 끔 | outage에서 재시작 0 | KILLED, 해당 테스트 exit 1 |
| M2 BALANCE 한도 2→3 | 세 번째 자동복구 금지 | KILLED, exit 1 |
| M3 HEARTBEAT 한도 1→2 | 기존 하루 1회 유지 | KILLED, exit 1 |
| M4 outage timeout 5→15 | timeout 뒤 `[15,5,15]` | KILLED, exit 1 |
| M5 성공 후 timeout 복원 제거 | 성공 뒤 15초 복원 | KILLED, exit 1 |
| M6 quote 직전 heartbeat 제거 | blocking 직전 진행표시 | KILLED, exit 1 |
| M7 최소 실패 3→1 | 1·2회는 outage 아님 | KILLED, exit 1 |
| M8 watchdog 독립 5분 상한 제거 | stale 양성도 기존 경로 | KILLED, exit 1 |

## 5. 적대 반증 요청

다음을 실제 코드·프로브로 반증해 P0~P3 판정해 달라. **P0/P1이 하나라도 있으면
병합 차단**이다.

1. 손상·부분쓰기·타입 혼동·노후 `balance_health`가 KIS outage로 오인되어 진짜
   sentinel 장애의 재시작을 생략할 수 있는가.
2. 장애 분류가 hard-disable 전에 kill을 내리거나, L1 상향 자체를 누락시키는가.
3. BALANCE 두 번째 자동복구가 30분 관찰/readiness/L2+/operator/완전일치 사유 중
   하나라도 우회하는가.
4. 구버전 상태 또는 날짜 전환으로 하루 예산이 부당하게 부활하는가.
5. `_get` 이외 주문·취소·토큰 호출의 timeout이 5초로 짧아졌는가. timeout이 아닌
   HTTP 오류도 잘못 backoff 상태를 만드는가.
6. blocking 직전 heartbeat가 별도 스레드처럼 멈춘 루프를 살아 있다고 위장하는가.
7. balance status 파일 쓰기 실패나 성공-reset 실패 시 완화 방향으로 동작하는가.
8. 경보/로그/상태 파일에 시크릿·주문키·심볼·금액이 새는가.
9. T1~T3가 주문 생성·매도 보호·원장/kill 하향 경로를 직접 변경했는가.

## 6. 금지선과 다음 단계

이 브랜치는 로컬 검토 준비만 완료했다. **원격 push, PR, 기본 브랜치 병합, Oracle
배포, kill 하향, env 변경은 수행하지 않았다.** Claude P0/P1=0과 사용자 별도 승인
후에만 병합·배포한다. 배포 후에는 다음 실제 KIS timeout burst에서 재시작 0,
BALANCE 사유 L1, 30분 뒤 자동 L0, 복구 알림 1건을 관찰해야 운영 완료다.
