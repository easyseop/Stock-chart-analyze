# Claude 재검토 요청 V2 — 정체청산·성과 리베이스 차단 결함 수정

## 요청 판정

PR #93의 1차 적대검토에서 확인된 `P0 1건`, `P1 2건`, `P2 3건`의 수정본입니다.
아래 반례를 다시 실행해 `P0/P1/P2/P3`, 승인/차단을 판정해 주세요.
`P0/P1`이 하나라도 남으면 병합하지 않습니다.

- 기준 브랜치: `ba30f9c7` 이후 `codex/stall-exit-performance-rebase`
- Draft PR: #93
- 병합·Oracle 배포·legacy apply·L1 해제·`STALL_EXIT_MODE=live`: 모두 미실행

## 1. P0 — 닫힌 시장의 정체 상태 소거

### 원인

`kis_exits.manage()`가 상태 소멸 여부를 현재 열린 시장의 `held`로 판단했습니다.
한국장 중 미국 종목은 `held`에서 빠지므로 12일까지 쌓인 정체일도 0으로
사라졌습니다.

### 수정

- 소멸 기준을 시장별 임시 `held`가 아니라 시장과 무관한
  `kis_positions.load()`의 봇 포지션 원장으로 바꿨습니다.
- `held={}`여도 포지션 원장에 종목이 있으면 상태를 유지합니다.
- 원장에 `close`가 기록된 뒤에만 정체 상태를 제거합니다.

### 재현 테스트

`tests/test_stall_exit.py::
test_closed_market_does_not_prune_other_market_stall_state`

- 12일 상태 + `held={}` → 12일 유지
- `kis_positions.close()` 뒤 같은 호출 → 상태 제거

## 2. P1 — autopaper의 stop0 없는 legacy 포지션

### 원인

초기 손절 `stop0`가 없는 포지션에서 현재 래칫 손절을 R 기준으로 재사용해
`risk_ps <= 0`이 됐습니다. live 정체 폭을 적용하면 손절선이 최고가보다 높아질
수 있었고, live가 15일 전에도 기존 3×ATR을 R 기반 폭으로 바꿨습니다.

### 수정

- `_initial_stop()`은 `stop0` 또는 진입계획의 최초 `plan.stop`만 인정합니다.
- `0 < initial_stop < entry`가 증명되지 않으면 R 기반 정체 래칫을 적용하지
  않습니다.
- 15일 전에는 live도 기존 `최고가 - 3×ATR`을 그대로 유지합니다.
- 15일 이상이고 초기 R이 증명된 경우에만 `최고가 - 1.0R`로 좁힙니다.
- 30일 청산 판단 전 보호선 상향을 먼저 계산해 청산 재시도 중에도 보호가
  멈추지 않게 했습니다.

### 재현 테스트

`tests/test_stall_exit.py::
test_autopaper_legacy_half_without_initial_stop_keeps_three_atr_trail`

- entry 100, 현재 stop 115, stop0 없음, 최고가 130, ATR 2
- 수정 뒤 stop은 124(`130 - 3×2`)이며 최고가를 넘지 않습니다.

## 3. P1 — 리베이스 첫날 계좌/지수 앵커 불일치

### 원인

리베이스 첫날 계좌는 첫 표본 기준인데 `daily_indices`만 전일종가 기준이었습니다.
기간 차트가 `daily_indices`를 우선 사용해 오버나이트 갭이 지수에만 들어가고,
그 오차가 1개월·3개월·전체에 계속 복리됐습니다.

### 수정

- `basis == "first_sample"`인 날은 `daily_indices`도 세션 `indices`와 같은
  첫 표본 기준으로 만듭니다.
- 이후 `basis == "previous_close"`인 정상 일자는 기존 전일종가 기준을
  유지합니다.
- 일별 API 행에도 `basis`를 보존합니다.

### 재현 테스트

`tests/test_alpha.py::
test_accounting_migration_rebase_is_atomic_and_idempotent`

- 첫 표본 계좌 `0%`
- 첫 표본 세션 지수 `0%`
- 첫 표본 `daily_indices["나스닥"] == 0%`
- 같은 plan SHA 재실행은 새 성과를 다시 지우지 않음

## 4. P2 — 손상 상태와 half_done 증명

### 수정

- `kis_positions.jsonl`에 멱등 `half_done` 이벤트를 추가했습니다.
- 1주라 실제 매도 없이 본전 래칫한 경우와 절반매도 체결 확정 경우를 모두
  영구 기록합니다.
- 정체 상태 JSON 손상 시 열린 시장만이 아니라 `kis_positions`의 모든 종목을
  `state_recovery_quarantine`으로 먼저 격리합니다.
- durable `half_done`, 확정 SELL 원장, 또는 기존 1주+본전손절의 3가지 증명 중
  하나가 있어야 격리를 해제합니다.
- 증명이 없으면 +1R 재매도와 21일 타임스탑을 모두 보류합니다. 기존 sentinel
  하드 손절은 계속 동작합니다.

### 재현 테스트

- 손상을 닫힌 다른 시장에서 먼저 감지한 뒤 개장해도 격리 유지
- 증명 없는 5주 포지션은 live·오래된 진입일이어도 매도 0건
- 1주+본전손절 legacy 포지션은 `half_done`을 1회 영속하고 즉시 청산 0건
- 확정 절반 SELL은 손상 뒤 half를 복구하고 재매도 0건

## 5. P2 — 회계 완료 후 성과 리베이스 장애 복구

### 수정

새 CLI를 추가했습니다.

```bash
python -m bot.legacy_migration recover-performance \
  --plan /path/to/exact-plan.json \
  --services-stopped \
  --backup-dir /path/to/original-apply-backup \
  --ack "RECOVER <plan_sha256>"
```

만료된 plan을 허용하는 것은 이 복구 명령뿐입니다. 아래 조건을 모두 다시
증명한 뒤 `alpha` epoch만 멱등 변경합니다.

- plan version·canonical SHA·별도 `RECOVER` operator ack
- KIS mock·L1·서비스 inactive+runtime mask·수동 프로세스 0·heartbeat stale
- plan과 같은 세 운영 원장 절대경로
- broker snapshot 완전일치
- 주문·포지션·costbook 무손상
- 모든 entry의 costbook/보호수량/SELL 회계와 BUY `accounted == original`
- apply 전에 만든 4파일 backup manifest와 실제 바이트 SHA/크기 완전일치

하나라도 다르면 alpha를 바꾸지 않고 주문 0건으로 거부합니다.

### 장애주입 테스트

- 3원장과 BUY accounted가 모두 fsync된 뒤 alpha rebase에서 강제 `OSError`
- plan이 5분 만료된 뒤 변조 backup으로 복구 시도 → 거부, epoch 불변
- 원본 backup으로 복구 → epoch 1회 전환
- 같은 복구 재실행 → `already_applied`, 3원장 바이트 크기 불변

## 6. P2 — 기간 중 부분수집 동일가중 비교

### 수정

- 오늘은 기존처럼 해당 시점 `covered == eligible > 0`일 때만 표시합니다.
- 1개월·3개월·전체는 선택 기간의 **모든 일자**가 완전수집이어야 동일가중
  보유수익률을 복리합니다.
- 하루라도 부분수집이면 해당 기간 보유 동일가중 전체를 숨기고
  `선택 기간 중 부분수집 N일 · 지수 비교 제외`로 표시합니다.
- 계좌 TWR·전략 A/B·지수는 그대로 표시하며 서로 다른 종목 부분집합을 전체
  지수와 비교하는 값만 차단합니다.

Node 입력→출력 테스트와 로컬 브라우저의 `전체` 탭에서
`부분수집 1일 · 지수 비교 제외`를 확인했습니다. 선별 토글도 정상입니다.

## 7. 함께 닫은 비차단 항목

- 리베이스 `_save()` 직후 빈 epoch를 ntfy 대시보드 캐시에 즉시 발행해 옛
  `-17%`가 다음 틱까지 남는 시간을 제거했습니다. 발행 실패는 기존처럼 다음
  틱 재시도이며 회계/epoch 저장을 되돌리지 않습니다.
- `STALL_BASE_TRAIL_R` 환경값이 off/shadow의 기존 KIS 1.5R 보호를 바꾸지
  않도록 기본 보호폭을 고정했습니다.
- 30일 매도 액션과 보호선 상향이 함께 나오면 상향을 먼저 기록합니다.

## 날짜 의미 결정

이번 버그 수정에서는 기존 사양인 **고유 KST 날짜**를 유지했습니다. 미국 정규장
한 세션이 KST 자정을 넘으므로 이름상의 30거래일보다 일찍 도달할 수 있습니다.
동작을 조용히 미국 세션일로 바꾸지 않고, shadow 결과에서 실제 세션 수를 함께
검토합니다. live 전환 때 정체 상태를 종목별로 사람이 확인해야 하며, 30일 누적
종목의 일괄 청산 가능성도 별도 승인 항목입니다.

## 검증 결과

- 전체 Python 독립 테스트 모듈 `46/46` 통과
- Node 계산 테스트 `10/10` 통과
- `python -m compileall -q bot scanner tests`
- `node --check scanner/site_app/app.js`
- `node --check scanner/site_app/portfolio_math.js`
- `git diff --check`
- 로컬 브라우저: 오늘/전체 전환, 부분수집 기간 차단 문구, 차트 선 토글 확인
- 구현 커밋 `d0f001d1`에서 GitHub `CI` run #94와 `Site UI CI` run #50 성공

## 운영 금지선

- 이 PR을 아직 병합하지 않습니다.
- Oracle 코드 배포와 legacy apply를 하지 않습니다.
- L1을 해제하지 않습니다.
- `STALL_EXIT_MODE`는 기본 `off`를 유지합니다.
- 승인 후에도 순서는 `merge → L1 유지 배포 → legacy apply → 수량/총시드 검증
  → shadow 1~2주 → 누적상태 사람 검토 → 별도 live 승인`입니다.
