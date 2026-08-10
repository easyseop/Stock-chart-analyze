# SELL 거절 대사 V2 — Claude P2 2건 반영 증거

기준 판정: 기본 브랜치 `5503683e`,
`docs/CLAUDE_REVIEW_SELL_REJECT_RECONCILE_V2_VERDICT.md`

반영 커밋: `60b517c` (`codex/sell-reject-reconcile`)

## P2-1 — 동일 종목 다중 in-flight 방어 회귀 테스트

추가 테스트:

- `tests.test_sell_reject_reconcile::test_absence_reject_requires_single_symbol_inflight`

재현은 동일 TAP에 601초 된 ACK와 10초 된 fresh ACK를 동시에 만든 뒤, 오래된
주문에 완전 부재 증거를 주입한다. 기대값은 정산 0·모순 0·오래된 주문 `ack`
유지다. 따라서 `open_count.get(symbol) != 1` 가드가 사라지면 실패한다.

검토자 MA를 동일하게 재현해 가드를 `or False`로 무력화했을 때:

```text
AssertionError
test_absence_reject_requires_single_symbol_inflight, line 193
assert rs == contradictions == []
exit=1
```

## P2-2 — 경보 성공 뒤에만 래치

### `kis_boot`

변경 순서는 다음과 같다.

1. `_update_status(success=False)`는 streak와 오류만 저장하고
   `failure_alerted`를 미리 잠그지 않는다.
2. `_record_failure`가 `_notify(...)`의 bool 반환값을 확인한다.
3. `True`일 때만 `_mark_failure_alerted(expected_streak)`가 0600·flock·fsync·
   원자교체로 공유 래치를 저장한다.
4. 전송 중 다른 프로세스가 성공 대사를 기록해 streak가 리셋되면 과거 실패
   래치를 다시 잠그지 않는다.

추가 테스트:

- `test_reconcile_failure_alert_latches_only_after_delivery`
- `test_reconcile_success_during_alert_does_not_relock_old_failure`

전송 실패 후에는 `failure_alerted=false`이고 다음 실패 사이클이 재전송한다.
성공 후에만 true가 되며 이후 중복 전송은 없다.

기존 선잠금 코드를 되살린 뮤테이션은 다음에서 실패했다.

```text
AssertionError
test_reconcile_failure_alert_latches_only_after_delivery, line 362
assert json.load(fp)["failure_alerted"] is False
exit=1
```

### `ops_status`

기존 `_swap_stuck_latch(current)` 선저장을 읽기와 성공 반영으로 분리했다.

1. `_read_stuck_latch()`로 이전 성공 래치만 읽는다.
2. 신규 ACK 경보와 해소 경보 각각 `notify.send(...) is True`인 키만 모은다.
3. `_update_stuck_latch(add=..., remove=...)`로 성공분만 원자 반영한다.
4. 변화가 없으면 파일을 다시 쓰지 않는다(Claude P3-1도 함께 해소).

추가 테스트:

- `tests.test_ops_status::test_stuck_ack_alert_latches_only_after_delivery`

신규 경보 실패→성공과 회복 경보 실패→성공을 각각 재현한다. 실패 뒤에는 다음
사이클 재전송, 성공 뒤에는 중복 0을 단언한다.

전송 결과와 무관하게 신규 키를 래치하는 뮤테이션은 다음에서 실패했다.

```text
AssertionError
test_stuck_ack_alert_latches_only_after_delivery, line 212
assert ops_status.maybe_alert_stuck_acks()
exit=1
```

## 전체 무손상 증거

```text
ALL PASS: Python test modules 52
Node site_math: tests 19, pass 19, fail 0
python -m compileall -q bot tests scanner scripts: exit 0
node --check scanner/site_app/app.js: exit 0
git diff --check: exit 0
```

P2 두 건 외 주문·kill·배포 경로는 변경하지 않았다. 병합·Oracle 배포는 사용자
승인 전까지 하지 않는다.
