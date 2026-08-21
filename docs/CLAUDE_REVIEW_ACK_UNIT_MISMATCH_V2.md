# Claude 부분 재검토 요청 V2 — ACK before 미상 복구·숫자 msg_cd

작성: 2026-08-22 · 구현: Codex

대상 브랜치: `codex/ack-unit-mismatch`

수정 코드 커밋: `3e07c2d2` · 직전 검토 대상: `8398e839`

## 1. 판정 요청

`docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH_VERDICT.md`의 P1-1·P2-1과 수정 지시서
R1~R3을 반영했습니다. 앞서 HOLDS였던 C1~C3 전체를 재구현하지 않았고, 아래
수정 부위와 회귀만 부분 적대 재검토해 주세요. P0/P1이 하나라도 있으면 병합
차단입니다. 기본 브랜치 병합·Oracle 배포·운영 CLI apply는 사용자 별도 승인 전
금지입니다.

## 2. R1-a — `hldg_before=None`은 운영자 fresh 부재 증명으로만 종결

자동 대사 세 경로는 전혀 완화하지 않았습니다. `before=None`이면 계속 다음과
같습니다.

| 경로 | 수정 전 | 수정 후 |
|---|---:|---:|
| exact ODNO 직접 체결(행 없음) | 0건 | 0건 |
| 잔고 delta | 0건 | 0건 |
| 자동 부재+잔고 | 0건 | 0건 |
| 운영자 CLI | `kind=hold` | 조건 충족 시 `absence-reject` |

CLI의 before 미상 예외는 아래를 모두 요구합니다.

1. `SELL`이며 원장 `hldg_before is None`
2. 상태가 `submitted` 또는 `ack`, 기존 체결량 0
3. 동일 심볼 broker in-flight 정확히 1건
4. ownership armed, 사용자 baseline 아님
5. nccs·ccnl 전 거래소 완전 조회 성공 및 exact ODNO 완전 부재
6. fresh 총보유 조회 성공
7. `max(REJECT_ABSENCE_MIN_S, ACK_AGE_MIN_S)` 경과
8. `--apply --ack`의 비어 있지 않은 운영자 승인

조회 실패·ccnl ODNO 존재·partial·동일 심볼 두 주문은 모두 거부됩니다. 적용은
0체결 `ledger.reconcile(..., 0)`뿐이며 `kis_accounting.sync_fill`은 호출하지 않습니다.
intent와 result 감사 이벤트 양쪽에 `before_unknown: true`를 남기고, terminal 뒤에만
동결을 해제합니다.

## 3. R1-b — 시장 전체 map과 발주 단일심볼 total을 분리

`holding_quantities(..., symbol=...)`가 같은 한 번의 잔고 응답에서 다음 두 계약을
동시에 제공합니다.

- `total`: 다른 단 한 행이라도 total 손상이면 계속 `None`. 자동 잔고 대사의
  완전 스냅샷 계약은 불변입니다.
- `symbol_total`: 주문 대상 심볼 행만 정상 파싱되면 숫자. 파수꾼 최초 SELL과
  미국 chase의 `hldg_before` 기록에만 사용합니다.
- `sellable`: 기존처럼 시장 전체 매도가능 map이며, 안전 클램프는 계속
  `min(요청, sellable)`입니다.

실측 픽스처:

```text
INGR total=11/sellable=6, BROKEN total=missing/sellable=2
→ market total=None
→ symbol_total(INGR)=11
→ SELL hldg_before=11, 전송수량<=6
→ holdings()=None (완전 map 소비자는 부분 map을 받지 않음)
```

대상 INGR 행 자체의 total이 손상되면 `symbol_total=None`이고 기존 R1-a 운영자
복구 대상으로 남습니다. 페이지·행 구조·HTTP가 불신이면 `holding_quantities`
전체가 `None`이라 주문도 기존대로 차단됩니다. 추가 KIS 호출은 없습니다.

## 4. R2 — 숫자 KIS `msg_cd`만 보존, 본문은 계속 마스킹

- `/진단`의 `last/submit_msg_cd`만 `code=True`로 재정화합니다.
- 종결 사유는 code와 message/status를 각각 정화한 뒤 합칩니다. code만
  `code=True`, `msg1`·`last_status`는 `code=False`입니다.
- 공개 ntfy 경로는 변경하지 않았고 category-only입니다.

동시 출력 단언:

```text
last_msg_cd=40570000
last_msg1="주문가능금액 부족 account=12345678"
→ /진단: 40570000 보존, 12345678 비노출
→ 종결 broker_reason: 40570000 보존, 12345678 비노출
```

submit 숫자 코드 `40910000`도 별도 회귀로 종결 사유 보존을 확인했습니다. 예외는
구조적으로 `msg_cd` 필드에만 한정되며 메시지 본문으로 전파되지 않습니다.

## 5. R3 비차단 공백도 함께 닫음

1. baseline 미무장 상태의 exact ODNO 확정 0건 테스트 추가
2. partial direct-fill 뒤 동결 유지(`terminal`일 때만 해제) 테스트 추가
3. 동일 broker observation 2회째 원장 byte 불변 테스트 추가
4. CLI `safe_plan`에서 수량 `filled`와 내부 `key` 제거, 문서와 일치
5. `record_reconcile_meta("")`는 `ValueError`로 거부

## 6. 검증 증거

- 집중: `python -m tests.test_ack_unit_mismatch` — PASS, rc=0
- KIS 파서: `python -m tests.test_kis` — PASS, rc=0
- 관련: `test_sentinel`, `test_sentinel_chase`, `test_kis_boot`,
  `test_kis_domestic` — PASS, rc=0
- 전체: `python -m tests.run_all` — **73/73 PASS**, rc=0
- Node: `node --test tests/site_math.test.js` — **19/19 PASS**, rc=0
- `compileall bot scanner scripts tests`, `node --check scanner/site_app/app.js`,
  `git diff --check` — PASS

독립 mutation(각 적용→실패 확인→즉시 원복):

| ID | 제거한 계약 | 잡은 테스트/종료코드 |
|---|---|---|
| M-R1a | `operator_unknown_sell` 경로 제거 | `test_r1_before_unknown_operator_absence_only`, rc=1 |
| M-R1b | symbol total을 시장 `total_ok`에 다시 결속 | `test_holding_quantities_share_one_trusted_response`, rc=1 |
| M-R2a | `/진단` msg_cd `code=True` 제거 | `test_r2_numeric_msg_code_visible_but_account_stays_redacted`, rc=1 |
| M-R2b | 종결 last_msg_cd `code=True` 제거 | 같은 R2 테스트, rc=1 |
| M-P3 | broker observation 중복제거 제거 | `test_p3_armed_terminal_and_observation_contracts`, rc=1 |

## 7. 집중 반증 질문

1. before가 있는 기존 주문의 자동/CLI 부재 기준이 조금이라도 완화됐는가?
2. before 미상 BUY·UNKNOWN·partial·cancel_pending도 잘못 종결되는가?
3. 같은 심볼 두 주문 또는 baseline 미무장에서 운영자 예외가 열리는가?
4. ccnl zero/positive 행이 있는데 absence로 0체결 종결되는가?
5. fresh 총보유 또는 한 거래소 조회 실패 뒤 intent라도 기록되는가?
6. before 미상 0체결 종결이 회계 lot/positions를 건드리는가?
7. 다른 심볼 손상 시 `symbol_total`은 살지만 `holdings()` 완전 map은 None인가?
8. 대상 심볼 자체 손상·페이지 미완에서도 숫자를 발명하는가?
9. 숫자 code 보존이 msg1·last_status의 계좌번호까지 보존시키는가?
10. CLI 화면에 key·filled·ODNO·계좌·가격이 남아 있는가?
11. partial 적용 후 동결이 풀리거나 자동 direct가 동결을 스스로 푸는가?
12. 공개 ntfy에 broker code/detail이 새로 노출되는가?

## 8. 변경하지 않은 것

- `safe_qty=min(요청, 매도가능)`
- 자동 `resolve_acks_by_balance`/`resolve_acks_by_absence`의 before=None 보류
- C2 exact ODNO·단일주문·armed·baseline 배제 경계
- kill-switch·주문 활성화·동결 자동해제 정책
- 공개 ntfy category-only 계약
- Oracle 운영 상태·환경·원장
