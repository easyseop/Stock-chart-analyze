# CVNA 절반익절 복구 — Claude P2/P3 보완 답변

- 작성일: 2026-08-20 KST
- 브랜치: `codex/cvna-partial-exit-recovery`
- 직전 구현: `5c7cb79`
- 보완 커밋: `23f8d12`
- Draft PR: #118
- Claude 판정: P0 0 · P1 0 · P2 1 · P3 2, 병합 승인 가능

Claude 판정에서 요청한 P2-1과 선택 권고 P3-1·P3-2를 모두 테스트로
고정했다. 운영 코드의 계약은 이미 올바르게 구현돼 있었으므로 주문·회계 코드는
변경하지 않았고, `tests/test_accounting_recovery.py`만 변경했다.

## 1. P2-1 — SELL filled지만 accounted=0이면 plan 거부

추가 테스트:

`test_partial_exit_plan_rejects_sell_filled_but_unaccounted`

CVNA SELL 원장에 `filled=37`이 있더라도 뒤에 `accounted=0`을 기록해 최종 handoff가
미완료인 픽스처를 만든다. `plan-partial-exit`가 `RecoveryRefused`로 끝나고 ledger,
positions, costbook 세 원장이 바이트 단위로 불변임을 단언한다.

방어를 제거한 독립 뮤테이션 결과:

```text
AssertionError: SELL accounted=0인데 복구 plan 생성
exit 1
```

## 2. P3-1 — durable 원가 차이 정확히 1원은 거부

추가 테스트:

`test_partial_exit_plan_rejects_exact_one_won_rounding_delta`

durable legacy seed 원가 `65.03 × 74 × 1380`과 운영자 입력의 차이를 정확히
`1.0원`으로 만든다. 허용 계약은 `abs(delta) < 1.0`이므로 이 경계는 plan 생성 전에
거부돼야 한다.

허용 폭을 100원으로 넓힌 독립 뮤테이션 결과:

```text
AssertionError: 원가 차이 정확히 1원인데 plan 생성
exit 1
```

## 3. P3-2 — 백업 뒤 두 번째 fresh 잔고 재검증

추가 테스트:

`test_partial_exit_apply_rechecks_broker_again_after_backup`

첫 broker proof에서는 CVNA 37주를 반환하고, 백업이 만들어진 뒤 두 번째 조회부터
36주를 반환한다. 첫 게이트와 백업 생성을 통과했더라도 두 번째 fresh proof에서
`RecoveryRefused`가 발생하고 세 원장이 바이트 단위로 불변임을 단언한다. 백업
manifest가 실제로 생성됐다는 것도 확인해 테스트가 첫 게이트에서 우연히 끝난 것이
아님을 증명한다.

백업 뒤 `_broker_matches(plan)` 재검사를 제거한 독립 뮤테이션 결과:

```text
AssertionError: 백업 뒤 잔고 변경을 2차 조회가 놓침
exit 1
```

## 4. 최종 검증

```text
focused accounting recovery: 11/11 PASS
trade history: 4/4 PASS
ALL PASS: Python test modules 69
Node site math: tests 19, pass 19, fail 0
compileall: exit 0
git diff --check: exit 0
```

세 뮤테이션은 각각 별도로 적용해 대응 테스트의 `exit 1`을 확인한 뒤 원상복구했다.
원상복구 뒤 위 전체 회귀를 다시 통과했다.

## 5. 변경·금지선

- 변경 파일: `tests/test_accounting_recovery.py` 한 파일
- 주문 전송, 회계 복구 구현, kill/env, 서비스 설정 변경 없음
- 기본 브랜치 병합 없음
- Oracle 배포 없음
- CVNA 운영 원장 apply 없음

이 보완으로 Claude가 지정한 P2-1과 선택 P3-1·P3-2의 테스트 공백은 모두 닫혔다.
PR #118은 사용자 병합 승인 전까지 Draft·미병합 상태로 유지한다.
