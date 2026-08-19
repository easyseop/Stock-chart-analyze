# Claude CVNA 복구 판정 P2-1 반영 증거

작성: Codex, 2026-08-20

대상 브랜치: `codex/riskcap-cvna-recovery`

Claude 판정: 기본 브랜치 `d88fcb9`,
`docs/CLAUDE_REVIEW_CVNA_RECOVERY_VERDICT.md`

## 판정 확인

Claude 최종 판정은 `P0 0 · P1 0 · P2 1 · P3 2`이며 다음 세 단계를
서로 분리해 승인했다.

- 병합 가능
- Oracle 코드 배포 가능(L1 유지·열린 주문 0 확인)
- CVNA apply 가능(런북 전제와 사용자 별도 승인 필요)

사용자 요청에 따라 유일한 P2-1만 병합 전에 반영했다. 이 작업으로 병합·배포·
CVNA apply를 실행하지 않았다.

## P2-1 수정

`tests/test_accounting_recovery.py`에
`test_accounting_recovery_pending_holds_budget_until_completion`을 추가했다.

검증하는 상태 전이는 다음과 같다.

1. zero-fill로 `rejected`된 CVNA 주문은 평소 예약액이 0원이다.
2. forensic apply가 `accounting_recovery_pending=true`를 기록하면 원래
   `reservation_cost_krw=6,641,190.384`원이 다시 합산된다.
3. 같은 총한도에서 1원짜리 다음 BUY도 전송 전 차단된다.
4. `pending=false`, `complete=true`가 영속되면 예약액은 0원으로 해제된다.
5. 그 뒤 같은 1원짜리 BUY는 통과한다.

따라서 apply 도중 costbook/position/accounted 사이에서 크래시가 나더라도 복구가
끝날 때까지 같은 돈을 다른 주문이 다시 쓰지 못하며, 완료 후에는 영구 예약 누수가
남지 않는다.

## 뮤테이션 증거

커밋 `5202fb7` 뒤 `bot/ledger.py`의 pending 분기를 단독 비활성화하고 테스트를
재실행했다.

```text
exit_code=1
File "tests/test_accounting_recovery.py", line 170
  assert during == (6641190.384, {"A": 6641190.384}), during
AssertionError: (0.0, {})
```

분기를 원상복구한 뒤 같은 테스트는 다음과 같이 통과했다.

```text
[PASS] recovery pending 동안 예약 유지 · 완료 뒤 해제
모든 forensic 회계 복구 테스트 통과.
```

## 전체 회귀

- `python -m tests.run_all`: Python 69모듈 ALL PASS, exit 0
- `python -m compileall -q bot scanner scripts tests`: exit 0
- Node 24 `--test tests/site_math.test.js`: 19/19 PASS, exit 0
- `git diff --check`: exit 0

## 최종 상태

요청된 P2-1 테스트 공백은 닫혔다. 코드 동작 변경은 없고 안전 계약을 고정하는
회귀 테스트만 추가했다. 현재 Codex 판정은 P0/P1/P2 0이며 병합 준비 완료다.
병합·Oracle 배포·CVNA apply는 모두 사용자 승인 전까지 보류한다.
