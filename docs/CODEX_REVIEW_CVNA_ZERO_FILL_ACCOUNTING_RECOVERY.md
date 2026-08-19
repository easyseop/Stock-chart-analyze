# CVNA zero-fill 오판·회계 복구·수동귀속·총위험 적대 검토 결과

작성: Codex, 2026-08-19  
브랜치: `codex/riskcap-cvna-recovery`  
기준: `a09c8b2`  
상태: **코드·테스트 완료, 미병합·미배포·Oracle 원장 미적용**

## 1. 결론

- CVNA는 수동 보유가 아니라 2026-08-18 KIS 봇 pullback 매수다.
- 직접 원인은 `ccnl`의 지연된 `filled=0/open=false` 행을 ACK 98초 뒤 확정
  거절로 오독한 zero-fill 대사 분기다.
- T2a 재발 방지, T2b exact-cost forensic 복구 도구, T2c 미래 수동매수 adopt
  도구를 구현했다.
- T1 총위험 게이트를 적대 검토하다 **동일 사이클 신규 주문과 기존 미체결
  예약위험이 합계에서 빠지는 P1**을 발견했고, 최종 제출 직전 ledger flock 안에서
  `현재 포지션 + 기존 BUY 예약 + 신규 주문`을 원자 재검증하도록 고쳤다.
- 수정 후 현재 자체 판정은 **P0 0, P1 0**이다. Claude 독립 재검토와 사용자
  승인이 끝나기 전에는 병합·배포·CVNA apply를 하지 않는다.

## 2. T2a — 확정 사건과 zero-fill 수정

### 원장·브로커 원문

```text
22:30:37 submit  kb:CVNA:CVNA-2026-08-18-now
         pullback limit 65.0332 × 74, reservation 6,641,190원
22:30:38 bind    ODNO=0000040445, ack, filled=0
22:32:16 reconcile rejected, filled=0, open=false
         reason=broker-closed-zero-fill, source=ccnl
         msg_cd=20310000 "모의투자 조회가 완료되었습니다"
실제 브로커 체결: 74주 × $65.03, 현 잔고 74주
```

### 바뀐 계약

1. `ccnl`의 닫힌 0주 행은 곧바로 `rejected`로 확정하지 않는다.
2. ACK 뒤 zero-fill 최소 유예는 **600초**이며 환경값으로 더 짧게 만들 수 없다.
3. BUY zero-fill 종결 전 완전한 최신 잔고를 반드시 읽는다.
4. 잔고가 기존 수량과 같을 때만 `zero-fill-balance-proof`로 종결한다.
5. 증가량이 의도수량과 같으면 체결로 먼저 회계하며, 일부 증가·초과·형식 불신은
   자동 귀속하지 않고 UNKNOWN 잠금과 P0 경보를 유지한다.
6. 닫힌 0주 후보는 LOW가 아니라 UNKNOWN 보호 잠금으로 남는다.
7. SELL 거절 부재증명 R1~R5 경로는 그대로 유지한다.

관련 파일: `bot/kis_reconcile.py`, `bot/kis_boot.py`, `bot/ledger.py`,
`tests/test_kis_reconcile.py`, `tests/test_kis_boot.py`,
`tests/test_sell_reject_reconcile.py`.

## 3. T2b — CVNA exact-cost forensic 회계 복구

신규 `bot/accounting_recovery.py`는 주문을 전혀 내지 않는 `plan/apply` 도구다.
운영 Oracle에는 아직 실행하지 않았다.

### 고정 복구 사실

- order key: `kb:CVNA:CVNA-2026-08-18-now`
- ODNO: `0000040445`
- BUY 74주, 체결가 `$65.03`
- 환율 `1380`
- exact cost: **6,640,863원**
- 기존 보호선: `$60.48`
- SEED A: **37,000,000원 유지**

### 안전 계약

- plan과 apply가 각각 KIS 체결과 세 거래소 잔고를 새로 읽는다.
- 조회 실패·행 부재·수량/체결가 충돌은 `None=0건`으로 오독하지 않고 거부한다.
- plan은 5분 만료, canonical SHA256, 운영 세 원장 절대경로를 포함한다.
- apply는 exact SHA ack, 미존재 backup-dir, sentinel/buyloop 정지·runtime mask,
  heartbeat 노후와 수동 프로세스 0을 요구하고 백업 뒤 다시 확인한다.
- 공통 event ID `fill:{order_key}:BUY:74`로 costbook·position·ledger를 append-only
  복구한다. costbook 뒤 크래시도 재실행 시 lot과 수량이 중복되지 않는다.
- 포지션 복구는 기존 74주에 74주를 더하지 않고 **절대수량 74주**로 교정한다.
- 복구 도중에는 `accounting_recovery_pending` 예약을 유지해 예산이 풀리지 않는다.
- normal `sync_fill`은 recovery pending을 건드리지 않는다.
- baseline 사용자 보유 종목에는 forensic 복구를 적용하지 않는다.

### 승인 후에만 실행할 런북 개요

```bash
python -m bot.accounting_recovery plan \
  --order-key kb:CVNA:CVNA-2026-08-18-now \
  --odno 0000040445 --symbol CVNA --qty 74 \
  --fill-price 65.03 --fx 1380 --cost-krw 6640863 \
  --trade-date 20260818 --output /tmp/cvna-accounting-recovery.json

# 출력된 exact SHA를 사람이 확인하고, 서비스 정지·mask·heartbeat 노후 뒤에만:
python -m bot.accounting_recovery apply \
  --plan /tmp/cvna-accounting-recovery.json \
  --ack "APPLY <EXACT_SHA256>" --services-stopped \
  --backup-dir /var/backups/stock/cvna-recovery-<NEW_DIR>
```

이 문서 작성 시점에는 위 apply를 실행하지 않았다.

## 4. T2c — 미래 수동매수 adopt

`python scripts/kis_arm.py --adopt SYMBOL "사람이 확인한 사유"`를 추가했다.
CVNA에는 사용하지 않는다.

- baseline을 0600 원자파일로 먼저 추가하고 재검증한다.
- 성공 뒤에만 `kis_positions`에서 해당 심볼을 close한다.
- baseline 저장 실패·손상 시 기존 보호 포지션을 제거하지 않는다.
- 두 번 실행해도 baseline·position bytes가 더 변하지 않는다.
- costbook에는 아무 이벤트도 쓰지 않는다.
- proven baseline 종목만 파수꾼 자동매도와 orphan 경보에서 제외한다.
- baseline 부재·손상은 보호를 끄는 근거가 되지 않는다.
- 동결·kill·SEED는 변경하지 않는다.

## 5. T1 — 총위험·이력·섀도 적대 검토

### 발견하고 수정한 P1

기존 구현은 후보 루프 시작 전에 현재 `kis_positions`의 위험만 한 번 계산했다.
따라서 다음 위험이 빠졌다.

- 아직 체결/회계되지 않은 active BUY의 예약 위험
- 한 buyloop 사이클 안에서 먼저 제출된 신규 주문의 위험
- 별도 프로세스가 거의 동시에 제출하는 신규 주문의 위험

각 주문이 개별 cap 미만이어도 합계가 cap을 넘을 수 있었다. 수정 후:

1. active/planned/unaccounted BUY에서 `reservation_risk_krw`를 재구성한다.
2. pre-gate가 `현재 위험 + 기존 예약 위험`을 본다.
3. `try_record_submit`이 ledger flock 안에서 현재 kpos를 다시 읽고
   `현재 + 기존 예약 + 새 주문`을 합산한다.
4. 상한과 정확히 같은 값도 차단한다.
5. 위험 메타는 호출자 `order_meta`로 덮어쓸 수 없다.
6. 수량·환율·손절·진입가가 bool/NaN/inf/음수/불명확하면 fail-closed다.

### 나머지 판정

- 계량 불가 포지션 하나가 전 계좌 신규매수를 막는 것은 고아·장부 손상 실측을
  고려하면 안전한 방향이다. 해당 행만 빼면 위험을 과소계상하므로 유지한다.
- 기본 10%는 배포 전 read-only 실측과 첫 buyloop 로그 확인이 필요하다.
- A 252봉 이력 게이트는 후보만 제외하고 수집·B·웹은 유지한다.
- B2는 현재/과거 봉만 사용하고 주문 가능 그룹과 분리되어 look-ahead·주문 경로가
  없다. NaN/inf 관측값도 신호로 승격하지 않는다.
- A ablation은 단일 게이트 효과만 보는 의도된 실험이며 복합 완화 효과를 주장하지
  않는다.
- 섀도 group은 `now`/`shelf` 완전일치 필터 밖이고 `orderable=false`다.

## 6. 검증 증거

### 전체 회귀

- `python -m tests.run_all`: **ALL PASS, Python modules 69, exit 0**
- `python -m compileall -q bot scanner scripts tests`: **exit 0**
- Node 24 `--test tests/site_math.test.js`: **19/19, exit 0**
- `git diff --check`: **exit 0**

KST 자정 근처 실행 시 30분 자가복구 단위시험이 날짜 롤오버에 걸리는 기존
시간 의존성도 발견했다. 해당 테스트에서만 날짜를 고정했고, 실제 하루 1회 리셋
테스트는 그대로 유지했다.

### 뮤테이션 — 전부 KILLED

| 영역 | 제거/변조 | 잡은 테스트 | 종료코드 |
|---|---|---|---:|
| T2a | 잔고 교차검증 생략 | `test_cvna_balance_fill_precedes_zero_fill_rejection` | 1 |
| T2a | grace 600→90 | 599/601초 zero-fill 경계 테스트 | 1 |
| T2a | 닫힌 zero-fill을 LOW로 강등 | `test_unknown_closed_zero_fill_candidate_stays_low` | 1 |
| T2a | 잔고 모순을 불변으로 처리 | 잔고 모순 UNKNOWN/P0 테스트 | 1 |
| T2b | exact cost 대신 산술 파생값 사용 | exact-cost 복구 테스트 | 1 |
| T2b | 절대 교정 대신 BUY delta 적용 | 74주 절대수량 테스트(148주 반례) | 1 |
| T2c | baseline 실패 뒤에도 position close | adopt 실패 시 보호 유지 테스트 | 1 |
| T2c | sentinel baseline 제외 제거 | baseline 자동매도 0 테스트 | 1 |
| T1 | pre-gate 예약위험 제외 | `test_invalid_fx_fractional_qty_and_reserved_risk_fail_closed` | 1 |
| T1 | atomic 합계에서 기존 예약 제외 | `test_atomic_projected_risk_blocks_second_same_cycle_order` | 1 |
| T1 | cap 경계를 `>=`→`>` 완화 | 같은 atomic 경계 테스트 | 1 |
| shadow | NaN/inf 신호 허용 | `test_b2_and_b1_reject_nonfinite_observation_inputs` | 1 |

## 7. 남은 승인 단계

1. Claude가 아래 전용 요청서로 적대 재검토해 P0/P1=0을 확인한다.
2. 사용자가 병합과 장외 Oracle 코드 배포를 별도로 승인한다.
3. 코드만 L1 유지 상태로 배포한다.
4. 사용자가 CVNA 74주 표와 exact cost를 다시 승인한 뒤 plan을 새로 만든다.
5. exact SHA·신규 backup-dir로 apply하고 세 원장/KIS 수량·원가·보호선을 대조한다.
6. 서비스를 정상 복구하되 L1/L0 변경은 별도 readiness와 승인으로 처리한다.

