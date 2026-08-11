# Claude 적대 검토 요청 — 관측·감시 안전 3건 통합

검토 브랜치: `codex/observability-safety`

기준: `claude/happy-gauss-cwoq21@f0599b2`
범위: 배포 유예창 + 잔고 경보 위생/진단 + 제한적 L1 자가복구

## 판정 기준

- P0/P1 하나라도 있으면 병합·Oracle 배포 차단.
- 이 변경은 관측·감시 계층뿐이다. 신규매수·매도·주문 primitive의 허용 조건,
  수량, 가격, 원장 전이는 변경하면 안 된다.
- 코드 병합, Oracle 배포, 현재 kill 하향은 이 검토와 별개로 사용자 승인이 필요하다.

## 구현 요약

### A. 배포 유예창

- `bot/deploy_grace.py`: `/opt/stock/deploy_grace.json`을 0644·fsync·원자교체.
- `infra/server/autodeploy.sh`: import smoke 통과 뒤 서비스 재시작 직전에만 마커 기록.
  마커 쓰기 실패 시 이전 HEAD로 롤백하고 재시작하지 않는다.
- watchdog: 기본 300초, 최대 600초. 손상/비유한/61초 초과 미래/만료 마커는
  모두 유예하지 않는다. 유예 종료 뒤 heartbeat가 이미 임계 초과면 즉시 기존
  restart/L1 경로로 간다. 회복 알림은 유지한다.
- ops snapshot에는 `deploy_grace: bool`만 노출한다.

### B. 잔고 경보 위생

- `bot/balance_health.py`: 원인 계열별 첫 실패 즉시, 30분 묶음, 60분 1회 격상,
  회복 1회. 발송 성공 뒤에만 래치한다.
- `bot/kis.py`의 조회 전용 `_get`이 HTTP/rt_cd/msg_cd/예외 타입/레이트리밋을
  무시크릿 원인으로 남긴다. 주문 POST·주문 판정에는 연결하지 않는다.
- sentinel의 기존 캐시 감시·캐시 만료 시 `{}`·주문 직전 재조회 차단은 그대로다.
- 별도 프로세스인 `/진단`·ops가 볼 수 있도록 숫자 요약만 0600 원자 파일로 공유.
  24시간보다 오래되거나 미래/손상 파일은 0으로 처리한다.
- ops의 4시장 KIS 구간은 daemon 격리 + 전체 60초 예산. 초과 시장과 남은 시장은
  `None`, snapshot 발행은 계속한다.

### C. L1 자가복구

- `bot/watchdog_policy.py`가 생산자 상향 사유와 소비자 허용 사유의 단일 상수다.
  `(who, why)` 완전일치만 허용하고 미등록 값은 기본 거부한다.
- 정확히 L1, 허용 자동 사유, 30분 연속 `<60s`, l0 readiness broker GO,
  KST 하루 1회, 상태 0600 정상일 때만 `kill.lower_level(0, ack="self-heal: …")`.
- watchdog PID가 바뀌면 관찰시간을 0부터 다시 시작한다. readiness 도중 L2 또는
  다른 kill 사건으로 바뀌는 TOCTOU도 하향 직전 재검사해 차단한다.
- 하향 전에 하루 권한과 pending 알림을 원자 저장한다. L0가 된 경우에만 알림을
  보내며 전송 실패는 다음 사이클에 재시도한다.
- 상태 손상/쓰기 실패/조회 예외는 모두 하향 0건이며 watchdog 본체 예외와 격리된다.

## 필수 반례 20개 재검토

### 배포 유예 7

1. 유예 중 age=300: restart 0, raise 0.
2. 만료 age=301: 기존 restart 발동.
3. JSON 손상, NaN/inf, 미래 +61초: 유예 없음.
4. `DEPLOY_GRACE_S=9999`: 600.
5. 유예 종료 + age=130 + restart 3회: 같은 사이클 L1 상향.
6. autodeploy 마커는 smoke 뒤·systemctl restart 직전이며 실패 시 rollback.
7. grace 중 heartbeat 복구 알림은 유지.

### 잔고 경보 6

1. 같은 원인 10회: 즉시 1 + 30분 1 + 60분 격상 1.
2. 회복 통계 1회 뒤 새 실패는 새 사건.
3. 실패·회복 알림 전송 실패는 다음 사이클 재시도.
4. HTTP 500, TimeoutError, EGW00201 분리 + `/진단` 공유값 확인.
5. 90초 상당 블로킹 주입: 축소 예산 시점에 반환, 4시장 None.
6. sentinel 공개 feed 수량 매도 금지·주문 직전 재조회 계약 유지.

### 자가복구 7

1. 허용 watchdog L1 + 연속관찰 + GO: L0, who=self-heal 감사, 알림.
2. operator/불일치 사유/L2: 하향 0.
3. 29분, heartbeat 61초, watchdog PID 변경: 관찰 리셋.
4. NO-GO/예외/readiness 중 L2 상향: 하향 0.
5. 같은 KST일 두 번째 자동상향: 하향 0 + 수동 경보, 다음 날짜 리셋.
6. 상태 JSON 손상: 하향 0.
7. 하향 성공·알림 실패: L0 유지 + 다음 사이클 알림 재시도.

## 추가 적대 질문

1. autodeploy 마커가 영구 잔존하거나 시계가 역행해 watchdog을 600초 넘게 막는가?
2. grace 종료가 restart 카운터/heartbeat age를 새로 시작시키는가?
3. 서로 다른 잔고 원인이 번갈아 나올 때 경보가 과잉 억제되거나 사건이 섞이는가?
4. ops timeout daemon이 주문 함수나 kill을 호출하는 경로가 있는가?
5. 공유 요약 파일의 심볼·계좌·토큰·원문 응답 노출이 0인가?
6. 허용 사유의 부분문자열·대소문자·공백 변형이 self-heal을 통과하는가?
7. readiness 도중 L2, operator L1, 다른 자동 L1로 바뀌면 이전 증거로 하향되는가?
8. watchdog 재시작/관측공백을 정상 연속으로 세탁할 수 있는가?
9. 일일 상태 쓰기 실패 또는 손상에서 하향이 가능한가?
10. pending 알림을 미리 기록한 뒤 하향 전 크래시하면 거짓 L0 알림이 나가는가?
11. self-heal 예외가 기존 restart/L1 상향 루프를 중단시키는가?
12. `git diff`에 buyloop/kis_buy/kis_orders/ledger 주문 경로 변경이 정말 0인가?

## 실행 증거

- 신규 계약: `test_deploy_grace` 7/7, `test_balance_alert_hygiene` 6/6,
  `test_kill_self_heal` 7/7 — 종료코드 0.
- 핵심 회귀: `test_sentinel`, `test_ops_status`, `test_kis_telegram` — 종료코드 0.
- `python3 -m compileall -q bot infra/server/watchdog.py tests` — 종료코드 0.
- `git diff --check` — 종료코드 0.
- 로컬 전체 55모듈 실행: 변경과 무관한 46모듈은 통과했고 9모듈은 기본 Python의
  `pandas/numpy` 부재로 import 단계 종료코드 1. 기존 프로젝트 venv 재실행은 macOS가
  venv 바이너리를 iCloud `dataless`로 오프로드해 `dlopen ... errno=89`로 중단됐다.
  코드 assertion 실패는 0건이며 원격 CI 결과를 최종 무손상 증거로 삼는다.

## Claude 요청 판정

P0~P3로 판정하고, 특히 “진짜 사고를 오인으로 덮어 L0로 내리는 경로”를
mutation/fault injection으로 반증해 달라. P0/P1이면 병합 차단. P2/P3도 파일·줄,
재현 입력, 영향, 최소 수정과 함께 제시해 달라.

배포 운영 주의: 현재 설치된 autodeploy 기본 재시작 목록에는 watchdog이 없으므로,
사용자 승인 배포 때 코드 fast-forward 뒤 `watchdog.service`를 별도로 1회 재시작해
새 grace/self-heal 루프를 적재해야 한다. 이 브랜치는 서비스 상태를 바꾸지 않는다.
