# Claude 1차 적대 검토 결과와 Codex 보완 — SELL 거절 대사 R0~R5

검토 대상: `codex/sell-reject-reconcile` 초기 구현(기준 `262e61d`)

## 1차 판정

Claude의 1차 판정은 **병합 차단**이었다.

- P0 1건: 미국 ACK의 원장 거래소가 틀리거나 비어 있으면 그 거래소만
  조회해, 다른 미국 거래소에 살아 있는 주문을 “부재”로 오인할 수 있음.
- P1 2건:
  1. 응답 본문의 연속키만 확인하고 `tr_cont` 헤더 및
     `msg_cd=...12000` 연속 신호를 버림.
  2. 부재 증명 경로가 ownership baseline·동결 보호를 재확인하지 않음.
- P2 3건:
  1. 근거 meta의 미체결·체결 행수를 항상 0으로 기록.
  2. 미국 청산 재시도 세션을 KST 날짜로 세어 한 미국 세션이 KST 자정에서
     둘로 갈림.
  3. 프로세스 공유 health 파일의 `done/low`가 로컬 부팅 게이트 상태와 섞일
     가능성.
- P3 3건: ACK 방치 알림 래치가 프로세스 재시작 시 사라짐, 응답 상위 메시지와
  실제 행 거절사유의 출처 구분 부족, 부분 조회 실패가 전체 대사 실패 streak로
  보이는 진단 소음.

## 적용한 보완

1. 미국 주문은 원장 `excg`를 신뢰해 단일 거래소만 보지 않고
   `NASD/NYSE/AMEX` 미체결·접수일 체결을 전부 조회해 합집합한다. 세 거래소 중
   하나라도 실패하거나 불완전하면 부재 증명을 하지 않는다.
2. KIS GET 응답의 `tr_cont` 헤더를 보존하고, 연속키·`tr_cont=F/M`·
   `msg_cd=...12000`·페이지 상한 포화를 모두 불완전 응답으로 처리한다.
3. 부재 증명 전에 ownership baseline 존재를 확인하고, 동결/baseline 종목은
   검증된 legacy 이관 SELL 예외가 아니면 자동종결하지 않는다.
4. 근거 meta에 실제 `nccs_count`, `ccnl_count`, `odno_absent=true`를 저장한다.
5. 미국 주문일·R5 세션 상한은 `America/New_York`, 한국은 `Asia/Seoul` 기준으로
   계산한다. 미국 한 세션이 KST 자정을 지나도 상한이 다시 열리지 않는다.
6. 공유 health 파일에는 진단 필드만 저장하고 로컬 전용 `done/low`는 읽거나
   쓰지 않는다. 다른 프로세스가 악성/구형 파일에 `done=true`를 써도 로컬
   매매 게이트는 열리지 않는다.
7. ACK 방치 경보 래치를 0600·flock·fsync·원자교체 파일로 영속화하고, 여러
   해소 건은 한 번의 회복 알림으로 요약한다.
8. `msg_source=row|response`를 기록해 행 자체 거절사유와 일반 조회 응답 메시지를
   구별한다. 응답 상위 `msg1`은 `broker_reason`으로 승격하지 않는다.

부분 조회 실패를 실패 streak로 세는 동작은 유지했다. 이는 부재 증명에 필요한
모든 시장 스냅샷이 완전하지 않다는 사실을 fail-visible하게 드러내며, 자동종결은
하지 않는 안전 방향이다.

## V2 검증 결과

- `/usr/local/bin/python -m tests.run_all`: `ALL PASS: Python test modules 52`
- `python -m compileall -q bot tests scanner scripts`: exit 0
- Node 계산 테스트: 19/19
- `node --check scanner/site_app/app.js`: exit 0
- `git diff --check`: exit 0

추가 회귀는 미국 3거래소 합집합, 잘못된/누락 거래소에서 NYSE 생존 주문 보존,
응답 헤더 연속 신호, 페이지 포화, ownership 미armed·동결·baseline, 실제 근거
행수, 미국 세션 경계, 공유 health/로컬 게이트 분리, 프로세스 재시작 후 ACK 경보
중복 억제를 직접 검증한다.

1차 보완 뒤 M7~M12 뮤테이션도 독립 적용했다. 미국 단일거래소 회귀,
`tr_cont` 검사 제거, ownership 가드 제거, 근거 행수 0 고정, 미국 세션의 KST
회귀, ACK 래치의 프로세스 메모리 회귀가 각각 대응 테스트를 exit 1로 깨뜨렸다.
각 변이는 `apply_patch`로 즉시 원복했고 원복 후 집중 테스트가 다시 통과했다.

최종 병합 판단은 이 보완본을 Claude가 다시 적대 검토해 P0/P1=0인지 확인한 뒤
내린다. 사용자 승인 전 병합·Oracle 배포는 금지한다.
