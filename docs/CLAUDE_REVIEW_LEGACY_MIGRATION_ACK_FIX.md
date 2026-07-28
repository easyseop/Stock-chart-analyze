# Claude 재검토 요청 — legacy BUY 16건 이관·SELL ACK·절반익절 보호

작성일: 2026-07-28 KST
기준 브랜치/커밋: `claude/happy-gauss-cwoq21` / `9259bdb`
작업 브랜치: `codex/legacy-ledger-migration`
운영 상태: KIS mock, kill-switch L1, Oracle 적용 전
판정 규칙: P0/P1이 하나라도 있으면 기본 브랜치 merge/Oracle apply/L1 해제 차단

## 1. 검토 요청 결론

업그레이드 전에 체결된 BUY 16건은 `filled > accounted`이지만 구버전 주문에
`fx`·`pos_key`가 없어 원가장부로 넘어가지 못했다. 이 상태에서는 예약 원가를
안전하게 계산할 수 없어 신규매수가 fail-closed로 계속 잠긴다.

실제 Oracle 읽기 진단에서 함께 확인한 결함은 다음과 같다.

1. CAG·KKR·LW 절반익절 SELL은 KIS 잔고가 실제로 감소했지만 ACK에 고정됐다.
   - 해당 종목이 ownership baseline에 있어 일반 잔고대사가 보류된다.
   - 구버전 `_KisBroker.place_sell`은 절반매도 시 `hldg_before`에 전체 보유가
     아니라 주문수량을 기록했다. 따라서 `before - now == intended`가 성립하지
     않는다.
2. `kis_boot._resolve_acks`는 SELL 주문 한 건의 `residual == 0`을 종목 전체
   청산으로 오해해 `kis_positions.close(symbol)`을 호출했다. 절반익절 주문도
   주문 자체는 full-fill이므로, ACK가 정상 해소되면 남은 절반의 손절 기록을
   지울 수 있었다.
3. 잔고대사에서 SELL 체결가가 없을 때 KIS 보유 평단을 매도가로 사용했다.
   실현손익을 0 근처로 왜곡하는 회계 오류다.

이번 변경은 위 네 경로를 함께 닫는다. 아직 Oracle 운영 장부에는 적용하지 않았다.

## 2. 변경 파일

### 운영 코드

- `bot/legacy_migration.py` (신규)
  - 읽기 전용 plan 생성과 명시적 apply를 분리한 1회 이관 도구.
  - 주문 모듈을 import하거나 주문 API를 호출하지 않는다.
- `bot/kis_positions.py`
  - `legacy_migrate` 절대수량 이벤트 추가.
  - 일반 `buy_fill` delta와 분리하고 필수 `event_id`로 재실행을 멱등화한다.
- `bot/ledger.py`
  - `ORDER_LEDGER_PATH` override 추가.
  - 기존 주문 상태를 바꾸지 않고 누락 회계 메타만 append하는
    `migration_meta` 이벤트 추가.
- `bot/kis_reconcile.py`
  - baseline 예외를 모든 baseline 종목에 열지 않는다.
  - `legacy_migrated=True` 포지션, 동일 `pos_key`, 동일 original `before`,
    동일 costbook lot 수량이 모두 맞는 SELL만 통과한다.
  - legacy 절반익절의 잘못된 `hldg_before`는 위 3중 durable 증명이 있을 때만
    `legacy_hldg_before`로 교정한다.
  - SELL 잔고대사 체결가는 보유 평단을 사용하지 않고 주문 제출가만 fallback한다.
  - 이관 apply는 `only_keys`로 CAG/KKR/LW 대상 ACK만 대사한다. 후보 hmap에
    없는 같은 시장의 다른 ACK를 `현재수량 0`으로 오해하지 않는다.
- `bot/sentinel.py`
  - 새 SELL은 `hldg_before=safe_qty`가 아니라 주문 직전 전체 매도가능수량을 기록한다.
- `bot/kis_boot.py`
  - SELL 주문 한 건의 full-fill 뒤 포지션 전체를 강제 `close`하던 코드를 제거했다.
  - 실제 포지션 감소/소멸은 `kis_accounting.apply_sell_fill`의 체결수량만 따른다.
- `bot/trade_history.py`, `scanner/site_app/app.js`
  - 이관된 과거 BUY/SELL도 거래이력에 표시하되, 실제 체결가가 없는 BUY는
    `장부 복원 가격`, 잔고로만 체결을 증명한 SELL은 `주문가 기준 매도가`로
    명시한다. 둘을 `실제 체결가`로 과장하지 않는다.

### 테스트

- `tests/test_legacy_migration.py` (신규)
- `tests/test_kis_ack_resolve.py`
- `tests/test_kis_boot.py`

## 3. 이관 상태기계

### plan (항상 읽기 전용)

다음을 모두 증명하지 못하면 JSON plan도 만들지 않는다.

- KIS `mock`
- 주문 원장과 원가장부 무손상
- ownership baseline armed
- 종목별 legacy BUY가 정확히 한 건이고 full-filled
- broker 현재수량이 `0 <= current <= original BUY`
- 감소수량이 유일한 SELL 한 건과 정확히 일치
- 열린 SELL은 ACK/submitted, 체결 0, pos_key 일치
- 진입가·손절선·최초 손절선·opened·A/B 복원 가능
- 거래소별 중복 잔고 행이 수량·평단까지 동일
- 기존 costbook에 출처가 다른 동종목 lot 없음
- 가격·평단·손절선·시각이 NaN/Infinity가 아닌 유한값

plan은 생성 5분 뒤 만료하며 canonical JSON SHA-256을 포함한다. 파일은
원자 교체·`0600` 권한·`fsync`로 저장한다.

### apply (주문 전송 0)

다음 조건을 모두 요구한다.

- plan SHA-256과 `APPLY <sha256>` operator ack 일치
- KIS mock
- L1 이상이며 `buy_new=False`
- 운영자가 `--services-stopped`를 명시
- 코드가 sentinel/buyloop 모두 실제 `inactive`이며 `masked` 또는
  `masked-runtime`인지 재확인
- plan 생성 뒤 5분 이내
- apply 직전 KIS 잔고 재조회 결과가 plan과 수량·평단까지 완전 동일
- 아직 존재하지 않는 전용 `--backup-dir` 지정
- 원장·보호 포지션·원가장부의 byte-for-byte 백업·SHA-256·fsync 완료

적용 순서:

1. 세 운영 JSONL을 새 전용 디렉터리에 byte-for-byte 백업하고 manifest 작성.
2. original BUY 수량으로 costbook lot을 멱등 시딩.
3. original BUY 수량·진입가·손절선·pos_key로 보호 포지션을 멱등 복원.
4. 기존 BUY/SELL에 누락 회계 메타 append.
5. 이미 filled인 BAM형 SELL을 ledger 가격으로 회계.
6. CAG/KKR/LW형 ACK만 original 수량·현재 잔고 delta와 durable ownership
   증명을 모두 통과시켜 filled/accounted로 대사.
7. 최종 costbook qty와 보호 포지션 qty가 broker current와 정확히 같을 때만
   legacy BUY `accounted=original`을 기록해 예약을 해제.

중간 실패나 크래시가 나면 마지막 BUY accounted가 남지 않아 예약은 계속 유지된다.
costbook/position event_id는 동일 plan 재실행 시 중복 lot·중복수량·중복손익을
만들지 않는다. ACK를 `filled`로 append한 직후 accounting 전에 죽은 창도 같은
plan 재실행에서 미회계 SELL을 먼저 복구한다.

## 4. 실제 Oracle 읽기 전용 검증

2026-07-28 KST에 배포 경로와 별개인 `/tmp/legacy-migration-review` overlay에서
운영 주문 원장·보호 포지션·원가장부와 KIS 모의잔고를 읽기만 했다.

- plan 생성 성공: 16건
- 현재 그대로 보유: 12건
- ACK 잔고대사 대상: 3건(CAG, KKR, LW)
- 완전청산 회계 대상: 1건(BAM)
- 대상 종목:
  `AQN, BAM, BIPC, CAG, CHYM, GPK, KKR, LW, MAIN, PUK, SNN, STE, TAP, VRSK, WAL, WDAY`
- original BUY 합계: 980주
- 현재 broker 합계: 920주
- 차이: 60주
  - BAM 28주 완전청산
  - CAG 13주, KKR 7주, LW 12주 절반청산
- plan 단계 주문 전송: 0
- 운영 JSONL 변경: 0
  - plan 직전·직후 세 파일 SHA-256 목록 `cmp` 일치
  - 현재 크기: 주문 19,921 / 포지션 4,635 / 원가 659 bytes
- L1 변경: 0
- 서비스 재시작/정지: 0

이 plan은 5분 만료형이므로 검토 뒤 apply에 재사용하면 안 된다. 승인 후 서비스를
정지한 상태에서 새 plan을 생성하고 다시 동일 snapshot을 검증해야 한다.

## 5. 자동 검증 결과

### 집중 테스트

- plan 생성이 원장·포지션·costbook을 변경하지 않음
- 잘못된 operator ack 거부
- 서비스 미정지 플래그 거부
- 실제 sentinel/buyloop 중 하나라도 active, unmasked이거나 상태조회가 실패하면 거부
- plan 이후 broker qty 변경 거부
- broker qty > original BUY 거부
- NaN/Infinity 가격·시각 거부
- 현재보유·부분청산 ACK·완전청산 세 형태 복원
- SELL 잔고대사에서 broker 평단이 아니라 주문 제출가 사용
- 후보가 아닌 같은 시장 ACK는 hmap 누락을 잔고 0으로 해석하지 않고 그대로 유지
- 절반익절 full-fill 뒤 남은 포지션 수량·손절 기록 유지
- baseline 사용자 보유는 pos_key 불일치 시 계속 보류
- apply 전 세 JSONL 백업, 권한 0600, 크기·SHA-256 manifest 검증
- costbook/position 기록 뒤 BUY accounted 직전 fault injection
  - 예약 유지
  - 동일 plan 재실행 복구
  - 중복 lot·중복수량 0
- ACK reconcile append 직후 accounting fault injection
  - SELL·BUY accounted 0으로 예약 유지
  - 같은 plan 재실행에서 SELL 회계 후 최종 복구
- 성공 plan 재실행 byte 멱등
- 이관 BUY 3건과 SELL 2건 거래이력 생성, 추정 가격은 `verified=false`

### 전체 회귀

- Python compileall: 통과
- 독립 Python 테스트 모듈: `45/45` 통과
- Node 계산 테스트: `8/8` 통과
- JavaScript·shell 문법 검사: 통과
- `git diff --check`: 통과
- 실제 Oracle read-only plan: `16/16` 분류 성공

## 6. Claude 적대 검토 질문

아래 반례를 코드 경로와 테스트 입력으로 직접 검증해 달라.

1. baseline에 있는 순수 사용자 보유가 잔고 감소만으로 봇 SELL에 귀속될 수 있는가?
2. 공격자가 `legacy_hldg_before`만 넣어 original before를 조작할 수 있는가?
   - position `legacy_migrated`, pos_key, costbook symbol/qty 세 증명이 모두 필요한가?
3. CAG형 `hldg_before=주문수량` ACK가 정확히 한 번만 풀리고, current qty만
   costbook/position에 남는가?
4. 동일 종목 SELL 두 건, pos_key 불일치, costbook lot 불일치, broker delta
   부분/초과에서 모두 fail-closed인가?
5. 절반익절 SELL 주문의 residual 0이 남은 포지션 전체 close를 다시 유발할 경로가
   다른 곳에 남아 있는가?
6. SELL 잔고대사에서 broker 평균매수가가 매도가로 들어갈 다른 경로가 있는가?
7. costbook add 뒤, position migrate 뒤, SELL 회계 뒤, BUY accounted 직전 각각
   크래시해도 예약 과소·중복 lot·중복 실현손익이 생기지 않는가?
8. operator 플래그, 실제 systemd `inactive`, runtime mask 3중확인 뒤
   snapshot·backup·apply 사이에 주문 프로세스가 재기동할 TOCTOU가 남는가?
   systemd를 우회한 수동 프로세스까지 별도 확인해야 하는가?
9. ledger → costbook/position 락 순서에서 자기교착이나 역순 락이 생기는가?
10. plan JSON 변조·만료·미래시각·KIS live·L0·손상 JSONL이 전부 적용 전에
    차단되는가?
11. 이관 도구의 전이 import graph에 `kis_orders`, `kis_buy`, `sentinel` 주문
    전송 경로가 포함되지 않는가?
12. BAM의 과거 매도가는 ledger 체결가를 쓰고, CAG/KKR/LW의 잔고 증명 매도가는
    제출가 fallback임이 허용 가능한 보수성인가? 더 정확한 KIS 체결가가 없을 때
    잘못된 평단가보다 이 선택이 안전한가?
13. 이관 대상 전용 `only_keys`가 모든 호출에서 유지돼, 같은 시장의 관계없는
    오래된 ACK가 후보 hmap의 누락을 잔고 0으로 오인해 풀릴 수 없는가?
14. ACK reconcile 이벤트 직후 크래시·재실행에서 SELL accounted와
    costbook/position event_id가 정확히 한 번만 반영되는가?
15. 최초 백업 manifest만으로 세 JSONL을 정확히 원상복구할 수 있는가? 백업 중
    실패·기존 디렉터리·symlink source가 모두 mutation 전에 차단되는가?
16. 과거 KIS 체결가가 없는 거래가 UI/API 어디에서도 `실제 매수가/매도가`로
    표현되지 않고 추정임이 끝까지 보존되는가?

## 7. 승인/차단 기준

- P0/P1 발견: 수정 전 기본 브랜치 merge/Oracle apply/L1 해제 차단.
- P0/P1 없음:
  1. 코드 commit/push 및 PR
  2. CI 전체 통과
  3. 기본 브랜치 병합
  4. Oracle 코드 단계배포, L1 유지
  5. sentinel/buyloop 정지 후 `systemctl mask --runtime`으로 재기동 경로 차단
  6. 새 5분 plan 생성·사람이 16건 표 확인
  7. 존재하지 않는 전용 backup dir와 exact SHA ack로 apply
     - 도구가 mutation 전에 세 JSONL 백업·SHA-256 manifest를 완료
  8. 최초 backup manifest를 별도 보존
  9. costbook/position/ledger와 KIS current qty 16건 대조
  10. `systemctl unmask --runtime` 후 sentinel/buyloop 재시작,
      heartbeat·보호 SELL 가능 확인
  11. 최소 한 사이클 뒤에도 L1 유지

L1 해제는 이 이관 승인·적용과 별도 결정이다. 이관 후에도 총시드 운영한도,
회계 손익, 열린 주문 0, 보호수량 일치가 모두 확인되기 전에는 신규매수를 열지 않는다.

## 8. 현재 변경 상태

- 코드 수정: 완료
- 집중/전체 테스트(45/45): 완료
- Oracle 실제 plan: 읽기 전용 완료
- 자체 적대검토에서 대상 밖 ACK 오귀속 반례를 발견해 `only_keys` 제한과
  회귀 테스트로 수정 완료
- commit/push: Claude 검토용 `codex/legacy-ledger-migration` 브랜치에만 수행
- PR/merge: 안 함
- Oracle 운영 코드 배포: 안 함
- Oracle 장부 apply: 안 함
- L1 해제: 안 함
