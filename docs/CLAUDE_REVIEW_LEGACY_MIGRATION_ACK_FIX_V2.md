# Claude 재검토 요청 V2 — legacy 이관 P1 2건·P2 4건 수정

작성일: 2026-07-28 KST

기준 브랜치/커밋: `codex/legacy-ledger-migration` / `913ac92199042d8287feac9e4db7adec68a7efeb`

운영 상태: KIS mock, kill-switch L1, Oracle apply 전

판정 규칙: P0/P1이 하나라도 있으면 merge·Oracle apply·L1 해제 차단

## 1. 1차 검토 지적과 수정

### P1-1 SELL 회계 중간 크래시의 팬텀 lot 재시딩

수정:

- `kis_accounting._sync_fill_locked`가 SELL `fill_event`를 계산한 뒤 costbook
  `event_results`를 먼저 확인한다.
- 같은 close 이벤트가 이미 durable하면 legacy lot 시딩을 건너뛴다.
- 그 뒤 `close_lot`과 `apply_sell_fill`은 같은 event_id 멱등성을 사용하고,
  마지막에만 ledger `accounted`를 기록한다.

필수 반증:

1. costbook close fsync 직후 `kis_positions.apply_sell_fill`에서 강제 크래시.
2. ledger accounted는 0, costbook close는 존재하는 상태 확인.
3. 동일 plan 재실행.
4. BAM `buy_cost`가 2배가 되지 않고 원본 1회분, `sell_proceeds`·실현손익도
   1회분, 열린 lot/position 0, SELL accounted=원수량인지 확인.

회귀 테스트:
`test_crash_after_sell_costbook_close_does_not_reseed_phantom_lot`.

### P1-2 balance-average 오염가 확정·표시

수정:

- terminal legacy SELL의 `fill_price_source=balance-average`이면 오염된
  `fill_price`를 버리고 원 주문 `price`를 사용한다.
- source를 `submitted-fallback`으로 바꾸며 실제 체결가로 승격하지 않는다.
- `trade_history`의 추정 source 집합에도 `balance-average`를 추가했다.
- costbook close의 `day_kst`는 이관 실행일이 아니라 원 SELL
  `submitted_at`의 KST 날짜다.

필수 반증:

1. 주문 제출가 80, 오염 fill_price 100, source balance-average를 주입.
2. plan/apply 후 exit price가 80, source submitted-fallback,
   `price_estimated=true`, `verified=false`인지 확인.
3. 실현손익이 `(80-100)*수량*fx`이며 원 매도일에 귀속되고 오늘 일일손실에
   들어가지 않는지 확인.

## 2. P2 네 건 수정

### P2-1 positions apply 재게이트

- apply mutation 전 `_assert_apply_journals_healthy`가 ledger, positions,
  costbook을 검사한다.
- 백업 뒤에도 동일 검사를 다시 한다.
- plan 생성 후 positions에 손상 JSON을 추가하면 backup/mutation 전에 거부하는
  테스트를 추가했다.

### P2-2 systemd 밖 프로세스와 TOCTOU

- 기존 systemd inactive+mask 검사를 유지한다.
- `pgrep -f 'bot\.(sentinel|kis_buyloop)'`가 PID를 하나라도 찾으면 거부한다.
- sentinel heartbeat가 120초 이내면 거부한다.
- 백업 완료 후 서비스·heartbeat, broker snapshot, 세 원장 무손상을 다시 검사한다.
- 두 번째 검사에서 프로세스 재등장을 주입하면 forensic backup만 남고 운영
  원장은 바뀌지 않는 테스트를 추가했다.

### P2-3 cwd 상대 주문 원장

- ledger 기본 경로를 `ledger.py` 디렉터리 기준으로 고정했다.
- plan version 2에 세 저널 절대경로를 포함한다.
- apply의 현재 세 경로와 완전히 같지 않으면 SHA가 맞아도 거부한다.

### P2-4 부분 hmap 계약

- `resolve_acks_by_balance`는 `complete_snapshot=True` 또는 exact
  `only_keys` 중 하나가 없으면 아무 ACK도 확정하지 않는다.
- 일반 boot 경로는 전체 잔고 조회 성공 뒤 `complete_snapshot=True`,
  legacy 이관은 plan의 exact `only_keys`를 사용한다.
- 누락 TSLA를 0주로 오인할 수 있는 부분 map 반례를 추가했다.

## 3. 추가 방어

- 비숫자 `legacy_hldg_before`는 예외로 배치 전체를 중단하지 않고 해당 주문만
  보류한다. 방향은 fail-closed다.
- `PLAN_VERSION=2`라 구 plan은 apply할 수 없다. 어차피 plan은 5분 만료형이다.
- 주문 전송 모듈 import와 주문 API 호출은 추가하지 않았다.

## 4. 완료 검증

```text
python -m tests.test_legacy_migration
  -> 8개 시나리오 PASS

python -m tests.test_kis_ack_resolve
  -> 8개 시나리오 PASS

python -m tests.test_kis_accounting
python -m tests.test_kis_boot
python -m tests.test_trade_history
  -> 모두 PASS

python tests/run_all.py
  -> ALL PASS: Python test modules 45

node --test tests/site_math.test.js
  -> 8/8 PASS

python -m compileall -q bot scanner tests
node --check scanner/site_app/app.js
node --check scanner/site_app/portfolio_math.js
git diff --check
  -> 모두 PASS
```

## 5. 재검토 반증 질문

1. close durable/accounted 0 상태에서 재실행할 때 어떤 경로로도
   `:legacy` add 이벤트가 새로 생길 수 있는가?
2. 부분청산과 완전청산 모두 `sold > original/2` 조건에서 팬텀 lot 없이
   최종 broker qty와 정확히 일치하는가?
3. `balance-average`가 plan, costbook, ledger meta, API/UI 어느 단계에서도
   actual/verified로 다시 승격되는가?
4. 과거 SELL 실현손익이 apply 당일 서킷브레이커에 들어갈 경로가 남았는가?
5. positions가 plan 뒤 또는 backup 중 손상되면 첫 mutation 전에 차단되는가?
6. 다른 cwd, 상대 env path, journal path 변경을 plan SHA 재사용으로 우회할 수
   있는가?
7. systemd mask 뒤 수동 프로세스 실행 또는 신선 heartbeat가 있으면 차단되는가?
8. 첫 quiescence 검사 뒤·백업 중 프로세스 재등장이 두 번째 검사에서 차단되는가?
9. 부분 hmap 호출자가 `complete_snapshot`도 `only_keys`도 없이 ACK를
   fake-resolve할 수 있는가?
10. `complete_snapshot=True`를 쓰는 boot 경로가 필요한 시장의 모든 거래소
    잔고 조회 중 하나라도 실패하면 None/fail-closed를 유지하는가?
11. 새 인자와 plan v2가 일반 ACK/UNKNOWN/BUY/SELL 회계 경로를 회귀시키는가?
12. ledger → costbook/positions 락 순서와 네트워크 무잠금 규약이 유지되는가?

## 6. 현재 허용 범위

- 이 브랜치 commit/push와 Claude 재검토까지만 허용.
- 기본 브랜치 merge, Oracle 코드 배포, migration apply, L1 해제는 하지 않는다.
- 승인 후 실제 apply에서도 서비스 stop+runtime mask 뒤 heartbeat가 120초를
  넘은 것을 확인하고 새 5분 plan을 생성해야 한다.
- apply 성공 뒤에도 L1 해제는 총시드, 열린 주문, 보호수량을 재검증하는 별도
  승인 사항이다.
