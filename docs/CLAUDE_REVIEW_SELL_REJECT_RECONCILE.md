# Claude 적대 재검토 요청 — SELL 거절 대사 R0~R5

## 검토 대상

- 저장소: `easyseop/Stock-chart-analyze`
- base: `c3949ace642a04bf6bf6265099714c85a3d0c195`
- 구현 핵심: `940cd19` (후속 검토 문서/증거 커밋은 추가될 수 있음)
- 브랜치: `codex/sell-reject-reconcile`
- 원본 지시서: `docs/CODEX_SPEC_SELL_REJECT_RECONCILE.md`
- 금지: 이 검토는 읽기전용이다. 병합·Oracle 배포·kill 변경·원장 수정 금지.

## 실측 전제(R0)

Oracle KIS mock에서 ODNO `0000038291`을 시크릿 없이 조회했다.

- 2026-08-10 NYSE 당일 ccnl: `rt_cd=0`, 연속키 없음, 5행.
- 대상 행: TAP, 80주 주문, 0주 체결, 80주 미체결.
- 행의 상태/거절 사유 필드는 빈 값. 응답 상위 `msg_cd=20310000`은
  일반 조회 완료라 거절 사유로 과장하면 안 됨.
- NYSE nccs: 성공·0행. NYSE 잔고: TAP 80주 불변,
  `ord_psbl_qty=0`. 16종목 중 TAP만 매도가능 0.
- 기본 7일 ccnl은 15행을 반환한 뒤 연속키가 남아 대상을 놓쳤고,
  접수일 단일 완전 조회가 대상 행을 밝혔다.

## 반드시 독립 재현할 반증 질문

### R1 부재 증명

1. 601초 ACK + nccs/ccnl 완전부재 + 완전잔고 불변만
   `rejected`로 닫히는지. 근거 meta에 실측치와 `absence-proof`가
   durable한지.
2. nccs, ccnl, balance 각각을 `None`으로 바꾸거나 `rt_cd!=0`,
   output 부재/비-list/비-dict, 연속키 존재를 주입했을 때 원장
   event·알림이 0인지. `[]`(성공한 부재)와 `None`(실패)이 어떤
   함수에서도 합쳐지지 않는지.
3. 599초와 정확히 600초 경계를 확인하고, 90초 ACK 기존 게이트가
   부재 증명 기간을 실수로 낮추지 못하는지.
4. `0000038291` vs `38291` 정규화가 nccs·ccnl 모두에 적용되는지.
5. ccnl에 0보다 큰 부분체결 행이 있거나 nccs에 잔량이 있으면
   R1이 절대 발동하지 않고 기존 경로 A가 수량/회계를 유지하는지.
6. SELL 잔고가 before와 다른데 ODNO가 두 조회에 없을 때
   자동종결·잔고 delta 자동확정이 모두 금지되고 모순 경보만
   1회 나가는지.
7. 같은 심볼에 fresh/partial/unknown 다른 broker in-flight가 있는 경우
   오래된 ACK를 단독으로 귀속하지 않는지.
8. 조회 순서가 실제로 nccs→접수일 ccnl→balance인지. 다일
   불완전 페이지를 부재로 쓰는 잔여 경로가 없는지.
9. KR mock만 `domestic_unfilled_orders` 폴백을 쓰고, `IS_MOCK=False`
   에서 기본 조회가 실패해도 폴백 HTTP가 0회인지.

### R2/R3 실패·사유 가시화

10. 포괄 except가 예외 종류를 로그/공유 상태에 남기고 부팅은 깨지
    않는지. 연속 실패 6회에서만 critical 알림 1회, 7회에서 중복
    0, 성공 후 streak 0·래치 리셋인지.
11. 상태 저장이 flock·fsync·atomic replace·0600을 쓰며 telegram 프로세스가
    다른 프로세스의 최종 성공/실패를 볼 수 있는지.
12. 종결 행의 row-level 사유는 저장하되 상위 일반 조회완료 `msg1`을
    거절 사유로 과장하지 않는지. 부재 증명은 반드시 사유 미상인지.
13. meta 키 whitelist·제어문자 제거·200자 상한이 실효적이고 계좌번호,
    token, app key, 내부 주문키가 meta로 유입되지 않는지.
14. `/진단`에 `대사: 마지막 성공 N분 전 · 연속 실패 M회`가 나오고,
    기록이 없거나 상태파일이 손상돼도 진단 명령이 살아남는지.

### R4 ACK 방치 경보

15. 1799/1800초 경계, submitted/ack만 대상, 행별 첫 경보 1회,
    미해소 반복 0, 해소 회복 1회, 그 후 0회인지.
16. ops JSON은 `stuck_acks` 숫자만 추가하고 심볼·수량·금액·주문번호를
    추가하지 않는지. `ops_status` 주문/kill mutation 경로 0인지.

### R5 재시도 가능 청산 키

17. 과거 고정 키 `xe:TAP:time:2026-07-20`가 rejected로 있으면 다음
    시도는 `#2`인지. 이미 있는 `#N`도 모두 attempts에 포함되는지.
18. time과 btgt 모두 기본 세션당 1회만 브로커에 시도하고, 즉시
    rejected·매도가능 0처럼 원장 전 거부 모두 같은 날 반복 0인지.
19. 상한 도달 알림이 규칙별 1회뿐이고, 다음 KST 세션에 카운터/
    래치가 리셋되어 다음 파생 키로 1회만 다시 시도하는지.
20. ack/unknown/open SELL이 있는 동안 기존 종목 잠금이 재시도보다 우선해
    이중/초과매도가 0인지. stall의 기존 고유 키 경로는 퇴행하지 않았는지.

## 요구 실행

```bash
python -m tests.test_sell_reject_reconcile
python -m tests.test_kis_boot
python -m tests.test_kis_reconcile
python -m tests.test_kis_ack_resolve
python -m tests.test_kis_accounting
python -m tests.test_kis_exits
python -m tests.test_ops_status
python -m tests.test_kis_telegram
python -m tests.test_l1_readiness
python -m tests.run_all
python -m compileall -q bot scanner tests scripts
node tests/site_math.test.js
node --check scanner/site_app/app.js
git diff --check c3949ace..HEAD
```

Codex 환경 실측: Python 모듈 `52/52`, Node `19/19`, compileall,
JS syntax, diff check 통과. 기본 `/usr/local` Node는 삭제된 ICU 71을
참조해 exit 134가 났고, 번들 Node v24.15.0으로 동일 테스트가
19/19로 통과했다(코드 실패와 구분).

## 뮤테이션 실측 증거

뮤테이션 전 구현을 커밋했고, 각 변이는 독립 적용→실패 확인→
`apply_patch`로 즉시 원복했다. 전부 **KILLED**.

| Mutation | 실패 테스트/라인 | exit |
|---|---|---:|
| M1 `rt_cd` 실패 검사 제거 | `test_raw_response_trust_contract`, `assert ... rt_cd=1 ... is None` | 1 |
| M2 600초 게이트를 90초로 완화 | `test_failure_is_never_absence_and_age_gate`, 599s assertion | 1 |
| M3 ccnl ODNO 존재 검사 제거 | `test_order_presence_partial_and_balance_contradiction_hold` | 1 |
| M4 잔고 불변 검사 제거 | 동일 테스트의 contradiction assertion | 1 |
| M5 live에서 KR mock 폴백 허용 | `test_kr_mock_fallback_and_live_prohibition`, `call_count == 0` | 1 |
| M6 R5 세션 상한 검사 제거 | `test_time_btgt_retry_keys_are_session_capped_and_legacy_compatible` | 1 |

## 판정 형식

- P0/P1/P2/P3로 분류.
- P0/P1이 하나라도 있으면 병합 차단.
- 각 지적은 파일·줄·재현 조건·영향·최소 수정안을 포함.
- 반례 20개를 각각 HOLDS/BROKEN으로 표시하고, 자동화 테스트만
  믿지 말고 코드·직접 프로브로 반증할 것.
