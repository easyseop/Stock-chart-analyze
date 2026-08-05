# KIS 직접진입 V10 — 기본 브랜치 통합·Oracle 실측 readiness 보완 재검토

## 1. 검토 대상과 범위

- 브랜치: `codex/kis-direct-v9-default`
- 기본 브랜치 기준: `claude/happy-gauss-cwoq21@af5232c8`
- 직접진입 V9 기준: `codex/kis-direct-scanner-entry-v8@fb680b66`
- V9는 Claude 재검토에서 P0/P1/P2 없음으로 승인됐다.
- 이번 V10은 V9를 실제 기본 브랜치에 통합하고, Oracle KIS mock에서 확인된
  국내 미체결 조회의 업무 미지원만 브로커-진실 대체 경로로 보완한다.
- 병합·Oracle 배포·운영 baseline 생성·장부 대사·kill 하향은 이 코드 변경에
  포함하지 않는다.

## 2. Oracle 읽기 전용 실측

2026-08-06 미국 정규장 중에는 운영 상태를 변경하지 않고 다음만 확인했다.

- Oracle은 기본 브랜치 `af5232c8`, clean, KIS mock, kill L1이었다.
- sentinel/watchdog/buyloop/telegram/portfolio-web/autodeploy.timer는 active였다.
- process env와 `/home/ubuntu/kis.env`에 `ALLOWED_SYMBOLS`는 이미 없었다.
- fallback 0, stall shadow, UNKNOWN 0, 미회계 BUY 0, heartbeat는 정상이었다.
- 브로커 미체결은 US 3거래소 0건, 국내
  `inquire-psbl-rvsecncl`은 mock 업무 미지원(`msg_cd=90000000`)이었다.
- 같은 mock 계정에서 `inquire-daily-ccld`, `CCLD_DVSN=02`를 호출한 결과
  `rt_cd=0`, 미체결 0행, 연속조회 키 없음이 실측됐다.

별도 운영 결함도 발견했다. SELL ACK 5건이 브로커에는 체결됐지만 전용계좌의
영속 `user_baseline.json`이 없어 잔고 대사가 fail-closed로 멈춰 있었다.
운영 세 원장을 공유락 아래 임시 디렉터리로 복제하고, 운영 파일 대신 복제본에
정상 형식의 빈 baseline(`{"symbols":[]}`)을 둔 뒤 `_resolve_acks()`를 실행했다.
그 결과 정확히 다음 5건만 회계됐고 원장 무결성도 유지됐다.

- AQN 129 → 0 (전량 SELL 129)
- GPK 123 → 0 (전량 SELL 123)
- SNN 25 → 0 (전량 SELL 25)
- CHYM 94 → 47 (절반 SELL 47)
- MAIN 92 → 46 (절반 SELL 46)

이 재현은 임시 복제본만 변경했으며 운영 baseline·ledger·costbook·positions와
kill-switch에는 쓰기 0건이다. 운영 반영은 미국 연장장 종료 후 별도 런북으로 한다.

## 3. 코드 변경

### 3.1 국내 mock 미체결 브로커-진실 대체 증명

- `bot/kis.py`
  - `domestic_unfilled_orders()` 추가.
  - 기존 일별주문체결 endpoint와 mock TR을 사용하되 `CCLD_DVSN=02`만 지정한다.
  - 기본 조회기간은 당일 하나이며 주문·취소·원장 경로를 import하거나 호출하지 않는다.
- `bot/l1_readiness.py`
  - 기존 `domestic_open_orders()` 응답을 먼저 사용한다.
  - 그 응답을 증명할 수 없고 `kis.IS_MOCK`일 때만 위 미체결 필터를 대체 조회한다.
  - `rt_cd=0`, list 응답, 연속조회 키 없음이 모두 맞아야 행 수를 인정한다.
  - 행이 하나라도 있으면 세부 수량 필드가 없어도 열린 주문으로 보수적으로 센다.
  - 실패·손상·페이지 미완은 `None`으로 남아 L1을 계속 차단한다.
  - live에서는 대체 경로를 사용하지 않고 기존 조회 실패를 그대로 fail-closed한다.

### 3.2 기본 브랜치 통합

- V9 전체를 최신 기본 브랜치에 merge한 통합본이다.
- 충돌은 `infra/server/README.md` 한 파일뿐이었고, 직접진입 buyloop 설명과
  기본 브랜치의 `/진단`·ops-status 설명을 모두 보존했다.
- TWR 격리·누적 지수 비교·익절 사후추적·거래이력·웹 설명 변경도 기본 브랜치와
  함께 전체 회귀했다.

## 4. 반드시 반증할 질문

1. mock에서 기존 국내 정정취소가능 조회가 `None`/업무 미지원이고 미체결 필터가
   성공·0행·완전 페이지일 때만 `broker_open_orders=0`이 되는가?
2. 대체 응답이 `rt_cd!=0`, output 결손/비-list/비-dict 행, 연속조회 키 존재이면
   0건으로 추측하지 않고 `None`으로 L1을 유지하는가?
3. 미체결 필터 응답에 행이 있으면 수량 필드가 없어도 전부 열린 주문으로 세는가?
4. live에서는 기존 국내 조회 실패 뒤 대체 endpoint를 호출하지 않는가?
5. unrestricted scanner-direct가 여전히 KR·US 양쪽을 모두 증명하며, 미국 한
   거래소라도 실패하면 합계 0으로 세탁하지 않는가?
6. 새 함수가 GET 조회만 사용하며 주문·취소·kill·원장·baseline을 변경하지 않는가?
7. `CCLD_DVSN`이 실수로 `00`/`01`이 되거나 조회기간이 불필요하게 넓어지는
   mutation을 테스트가 잡는가?
8. V9의 allowlist 제거와 기존 안전 게이트(mock hard-block·신선도·전략계약·
   세션·risk·예산·ownership·heartbeat·원장 멱등성)가 통합 과정에서 퇴행하지
   않았는가?
9. 기본 브랜치의 TWR 격리와 장기 누적 지수 비교, 정체청산 shadow, 사후추적,
   거래이력, 웹 보안 경계가 통합 merge로 퇴행하지 않았는가?
10. 운영 baseline 복구와 5건 대사는 코드 PR과 분리돼 있고, 이 PR 자체가 서버
    상태를 자동으로 바꾸지 않는가?

## 5. 로컬 검증

- 번들 Python으로 `python tests/run_all.py` → `ALL PASS: Python test modules 50`
- `python -m tests.test_l1_readiness` → 통과
- `python -m tests.test_kis` → 통과
- Node 웹 계산 테스트 → 15/15
- `node --check scanner/site_app/app.js` → 통과
- `python -m compileall -q bot scripts tests` → 통과
- `git diff --check` → 통과

테스트의 주문 primitive는 mock/spy이며 실제 주문 HTTP는 0건이다. Oracle 실측도
조회 API만 사용했고 운영 상태 파일 쓰기는 0건이다.

## 6. 판정 요청

P0~P3로 판정한다. P0/P1이 하나라도 있으면 병합·배포를 차단한다. 특히 mock
업무 미지원 우회가 아니라 **공식 체결조회 응답으로 미체결 0을 새로 증명하는
경로**인지, 응답 결손과 live에서 fail-open이 생기지 않았는지 적대적으로 본다.

