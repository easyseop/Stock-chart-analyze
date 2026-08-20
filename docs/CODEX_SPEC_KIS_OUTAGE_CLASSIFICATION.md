# 구현지시서 — KIS 장애 오귀속 제거 + 사유별 자가복구 (하루 1회 수동 L1 해소)

작성: Claude, 2026-08-21 · 발주: 사용자("하루 한 번꼴 매수금지, 해결 불가해?")
역할: **Codex 구현 → Claude 적대 검토 → 사용자 승인 후 병합·배포**
전제: 상향 규칙·L2+·operator ack 의무·fail-closed 불변.

## 1. 문제 (2026-08-21 15:15 실측 — 알림 원문 보존됨)

KIS 모의서버가 9분간 타임아웃 버스트(TimeoutError 16연속)를 일으켰다.
파수꾼은 살아있었지만 호출당 `_HTTP_TIMEOUT=15s`씩 블록돼 heartbeat 86→120s+,
watchdog은 **무의미한 재시작 3회**(죽은 건 KIS인데) 뒤
`HEARTBEAT_EXHAUSTED_REASON`으로 L1 상향. 오전에 자가복구 하루 1회를 이미 써서
오후 사건은 수동 복구 필요. 이 패턴이 사실상 매일 반복된다(KIS 모의 장애는
외부 요인 — 우리는 증폭기만 제거할 수 있다).

## 2. 요구사항

### T1. 상향 전 원인 분류 (핵심)

`infra/server/watchdog.py`의 heartbeat 악화 처리(현재 :98~:105)에서, 재시작·상향
**전에** KIS 장애 여부를 판별한다:

- 판별 소스는 기존 관측 재사용: `kis.last_get_failure()` 및/또는
  `balance_health`의 실패 추적 — "직전 N분(제안 5분) 내 KIS 실패가 연속 M회
  (제안 3회) 이상"이면 kis-outage로 분류.
- kis-outage일 때: `_restart_sentinel()` **생략**(재시작은 KIS를 못 고치고
  재시작 예산 3회만 태운다 — 실측), 상향 사유는
  `BALANCE_FAILURE_REASON`("KIS 잔고 조회 실패 지속 — 신규 금지")으로.
  이 사유는 **이미 자가복구 화이트리스트에 있다**(`watchdog_policy.py:8`) —
  새 배관 금지, 기존 사유 재사용.
- 분류 근거(마지막 실패 유형·연속 수)를 상향 알림과 self-heal 로그에 남긴다.
- 판별 실패(추적 파일 없음·손상)는 기존 동작(재시작→HEARTBEAT 사유) 유지 —
  분류기는 완화 장치이므로 fail-open 금지.

### T2. 사유별 자가복구 일일 상한

- 현재: 사유 무관 하루 1회. 변경: **사유별 카운트** —
  `BALANCE_FAILURE_REASON` 사유의 자가복구는 하루 2회까지,
  `HEARTBEAT_EXHAUSTED_REASON`은 기존 1회 유지.
- 완화이므로 적대 검토 포인트: 관찰 30분·readiness GO·L2+ 금지·완전일치
  화이트리스트·operator 상향 불가침은 **전부 불변**임을 테스트로 증명.
  같은 날 3번째 kis-outage L1은 수동(무한 자동복구 금지).

### T3. 타임아웃 증폭 축소

- KIS `_get` 연속 실패 중 백오프: 직전 호출이 Timeout이면 다음 호출 타임아웃을
  15→5s로 낮추고, 성공 1회에 원복. (전역 상수 변경 금지 — 연속 실패 상태에서만.)
- 파수꾼 종목 루프의 heartbeat 기록을 **블로킹 호출 직전**에도 수행
  (`_beat` 재사용). 스레드 금지 — "루프가 멈추면 heartbeat도 멈춘다" 계약 유지
  (test_sentinel_heartbeat_progress가 이미 스레드 사용을 금지 단언).
- 목표: KIS 9분 장애 시 heartbeat 최대 나이가 120s를 넘지 않게(재시작·상향
  자체가 발생하지 않는 것이 1차 방어).

## 3. 테스트 (최소)

1. KIS 실패 연속 상태 + heartbeat 악화 → 재시작 0회·사유 BALANCE_FAILURE로 상향.
2. KIS 정상인데 heartbeat 악화(진짜 파수꾼 문제) → 기존대로 재시작→HEARTBEAT 사유.
3. BALANCE 사유 자가복구 2회째 성공·3회째 거부, HEARTBEAT 사유는 1회 유지.
4. 판별 소스 손상 → 기존 경로(fail-open 금지).
5. 타임아웃 백오프: 연속 실패 시 5s·성공 후 15s 원복.
6. 기존 스위트 무손상(test_kill_self_heal·test_sentinel·test_watchdog_observability·
   test_sentinel_heartbeat_progress·test_killswitch).
7. 뮤테이션 검증 전 커밋. 증거는 실패 테스트명 + 종료코드 원문.

## 4. 완료 기준

Claude 적대 검토 P0/P1=0 → 사용자 승인 → 병합. 배포 후 다음 KIS 버스트에서
"재시작 0 · BALANCE 사유 L1 → 30분 뒤 자동 L0 · 폰엔 복구 알림만"을 실측하면 완료.
