# Claude 적대 검토 판정 — CVNA BUY 유실 + 절반익절 2건 복구 (v2)

검토일: 2026-08-20 · 대상: `codex/cvna-partial-exit-recovery` @ `c3bb337a`
(구현 `5c7cb79`, base `a5ced2f`) · 요청서: 반증 14문 + 권장 뮤테이션 6종

## 판정: **P0 0 · P1 0 · P2 1 · P3 2 — 병합 가능**

- **병합: 가능** (P2·P3 전부 테스트 공백 — 코드 동작 자체는 전 항목 정상 실측)
- **Oracle 코드 배포: 가능**
- **운영 apply: 별도 사용자 승인** (기존 절차 그대로)

## 전제 승인 — forensic 결론 수용

"SELL이 원가 없이 proceeds만 잡혔을 것"이라는 내 우려(지시서 T2b-2a)는
**기각이 맞다.** `kis_accounting`의 legacy seed 방어가 매도 직전 74주 lot을
평단 65.03으로 시딩한 뒤 37주를 정상 close했음을 코드·원장 양쪽에서 확인했다.
경제 장부는 이미 정확하고(잔여 37주 · 3,320,431.8원 · 실현 +228,748.8원),
**costbook을 다시 쓰지 않는 v2 설계가 유일하게 올바른 방향**이다. 74주 재기입은
이중계상이 됐을 것이다.

## 반증 14문 판정 — 전부 HOLDS (3건은 방어는 실존하나 테스트 미보호)

| 문항 | 판정 | 근거(코드/실측) |
|---|---|---|
| 1 중복·부재 event | HOLDS | `_raw_costbook_events` 중복 즉시 거부 — R2 뮤테이션 KILLED |
| 2 1원 허용치 세탁 | HOLDS* | seed_cost는 `price×qty×fx`와 **1e-6**으로 별도 앵커 — 1원 창은 운영자 정수 표기 허용일 뿐 경제값을 못 바꿈. *경계 테스트 부재(P3-1) |
| 3 수량 변경 거부 | HOLDS | `_validate_order`·`_validate_sell_order`·`_broker_matches` 3중 + Codex 뮤테이션 KILLED |
| 4 SELL 미확정 진행 | HOLDS* | 가드 실존(state·filled·accounted 3검사) — accounted 절 단독 제거가 미검출(P2-1) |
| 5 costbook 재기입 | HOLDS | apply의 costbook append **0** + 바이트 동일 단언 테스트 |
| 6 크래시 창 부활 | HOLDS | 절대수량 교정 + 멱등 재실행 테스트 · R4(74 부활) KILLED |
| 7 half_done·stop | HOLDS | repair가 half_done=True·stop 65.03·stop0 60.48 보존(fold 코드 확인) · Codex 뮤테이션 #2 KILLED |
| 8 pending 예약 | HOLDS | 직전 라운드 P2-1 테스트가 그대로 가드 |
| 9 fresh 잔고 우선 | HOLDS* | 디스패처의 1차 recheck는 **백업 전** 거부(코드 확인) · Codex #3 KILLED — 백업 후 2차 recheck는 미검(P3-2) |
| 10 정지 위조 | HOLDS | systemd is-active + masked-runtime + pgrep + heartbeat stale 실검사(직전 라운드 검증 유지) |
| 11 완료 재실행 | HOLDS | complete → 백업·append 전 조기 반환(코드 확인) |
| 12 증거 4종·키 노출 | HOLDS | R5(seed 증거 제거) KILLED · odno/order_key는 내부 조회 전용 — 출력 행 필드에 없음(diff 전수 확인) |
| 13 회귀 | HOLDS | run_all **69/69**·node 19/19·compileall·diff-check 전부 독립 재실행 PASS |
| 14 주문 경로 | HOLDS | place_/cancel/kill import·호출 0(grep) |

## 뮤테이션 — 권장 6종 재주입 결과

| | 결과 |
|---|---|
| R2 중복 event_id 허용 | KILLED |
| R4 잔여수량 74 부활 | KILLED |
| R5 seed 증거 없이 승격 | KILLED |
| **R1 원가 허용치 1→100원** | **생존** → P3-1 |
| **R3 SELL accounted 미검사** | **생존** → P2-1 |
| **R6 백업 후 2차 recheck 제거** | **생존** → P3-2 |

Codex 자체 보고 4종(재기입·half_done·1차 recheck·verified 연결)은 테스트명·
exit 1이 문서에 있고 해당 가드가 실존함을 코드로 확인해 수용한다.

### P2-1. SELL accounted 가드 단독 제거가 미검출

`_validate_sell_order`의 `accounted != qty` 절만 무력화해도 전 스위트 통과.
이 가드가 없으면 "SELL close는 있는데 ledger accounted가 안 된" 크래시 잔재
상태에서 apply가 **원장을 변조한 뒤** 최종 검증에서 실패하는(mutate-then-abort)
경로가 열린다. 최소 수정: accounted=0인 SELL 픽스처로 거부 테스트 1건.

### P3 (비차단)

1. 1원 허용치 경계 테스트 부재(Δ=1.0원에서 거부 단언). 경제 세탁은 1e-6
   앵커가 이미 차단하므로 위험 아님 — 문서화된 경계의 회귀 방어용.
2. 백업 후 2차 broker recheck 미검(1차는 검증됨) — 심층 방어층 회귀 방어용.

## 결론

세 생존 뮤턴트 전부 "방어는 있는데 테스트가 안 지키는" 부류로, 오늘 CVNA
apply의 정확성에는 영향이 없다(현 SELL은 accounted=37 확정·잔고 안정 실측).
**P2-1은 병합 전 처리 권장**(테스트 1건), P3 둘은 후속 무방.
