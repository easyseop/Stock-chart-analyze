# Claude 판정 기록 — L1 해제 조건 분리

## 1. 판정 출처와 상태

이 문서는 `docs/CLAUDE_REVIEW_L1_RELEASE_OPTIONS.md`에 대한 Claude의 회신을
인수인계용으로 정리한 기록이다. Claude는 13개 반례를 mock으로 실행했고 KIS
호출·주문은 0건이었다고 보고했다. 이 보고는 Oracle 현재 상태를 실측한 결과가
아니며, L1 하향 승인도 아니다.

최종 판정은 **선결조건 충족 후 제한적 L0 허용(대안 B)** 이다. 기존 Claude
승인 범위는 legacy 이관 v2 병합·Oracle apply와 PR #93 v2 병합까지였고,
L1 하향은 포함하지 않았다고 확인했다.

## 2. 기능별 게이트 분류

| 조건 | 판정 | 적용 범위 |
|---|---|---|
| KIS mock | 필수 | 모든 신규주문 |
| 주문 원장 정상·UNKNOWN 0 | 필수 | 모든 신규주문 |
| 열린 주문 0 | 조건부 필수 | L0 하향 직전 운영 위생 |
| 브로커·보호원장·costbook 수량 일치 | 필수 | L0 하향 전 점검기로 보완 |
| 운용원가 ≤ 운영한도 | 필수 | 모든 신규주문 |
| sentinel heartbeat 정상 | 필수 | 모든 신규주문 |
| 사용자 승인·operator ack | 필수 | kill-switch 하향 |
| 9종목 본전 래칫 | 별도 기능 조건 | 기존 보유 관리 |
| 정체청산 shadow 7일 | 별도 기능 조건 | stall `live` 전환 |
| Oracle 한·미 세션 | 별도 기능 조건 | fallback 1 |
| GitHub 60분 장애주입 | fallback 1의 필수 조건 | L0에서는 리허설 |
| 동결 6종목 결정 | 별도 기능 조건 | 해당 종목 재매수 |

## 3. 주요 발견사항

- P0/P1은 없었다. 신규매수 경로는 `execute_entry`의 단일 게이트 체인을
  경유하며 L0에서 새로 열리는 미게이트 경로가 없다고 보고했다.
- P2: Stage 1.5/2/2.5는 매수루프가 `risk_pct`를 전달하지 않아 기본 1%가
  각 Stage cap을 초과한다. 배선 전 해당 Stage를 카나리 용도로 사용하지 않는다.
- P2: 브로커·보호원장·costbook 수량 불일치는 매수 코드가 직접 차단하지 않는다.
  따라서 `kis_l1_readiness.py --broker --scope l0`를 하향 전 필수 절차로 둔다.
- P2: 기존 점검기는 16개 게이트를 하나의 `ready`로 과결합했다. `l0` 범위에서는
  별도 기능 관찰을 정보 항목으로 분리한다.
- P3: GitHub 신호는 20–45분 구간에서 입력으로 선택될 수 있다. mirror Stage도
  `ALLOWED_SYMBOLS`가 없으면 심볼 펜스가 없으므로 제한적 L0에서는 비어 있지
  않은 목록을 필수로 둔다.

## 4. 허용 범위

제한적 L0는 다음 조건을 계속 유지한다.

- `KIS_ENV=mock`
- `STALL_EXIT_MODE=shadow`
- `ORACLE_SIGNAL_FALLBACK_ENABLED=0`
- AQN, CAG, GPK, LW, SNN, VRSK close-only 동결
- 실전 주문 하드블록
- `TRADE_STAGE=mirror`
- 비어 있지 않은 `ALLOWED_SYMBOLS`

마지막 문서 기록대로 A 포지션이 mirror 상한 12개 이상이라면 A 신규매수는
구조적으로 차단되고 B만 열릴 가능성이 있다. 그러나 실제 허용 범위는 Oracle에서
점검기가 출력하는 `position_counts_by_sleeve`와 현재 예약 주문을 대조한 뒤
확정해야 한다. A가 12개 미만으로 줄면 같은 L0 승인 범위에서 A 매수가 다시
열릴 수 있다는 점도 승인 문구에 포함한다.

## 5. 실행 순서

1. PR #97의 `l0` scope 변경을 병합하고 Oracle에 L1 유지 상태로 배포한다.
2. 운영자가 `ALLOWED_SYMBOLS`를 명시하고 A/B 실제 개수를 확인한다.
3. Oracle에서 `python scripts/kis_l1_readiness.py --scope l0 --broker --json`을
   실행해 `ready_for_operator_review=true`와 차단 게이트 0을 확인한다.
4. 열린 주문 0과 최신 운용원가를 다시 확인한다.
5. 사용자가 아래 범위를 명시적으로 승인한 뒤에만 operator ack와 함께 L0으로
   내린다.
6. 첫 매수 1건의 submit→accounted, 보호선, 알림을 사람이 확인한다.
7. `UNKNOWN`, 원장 손상, heartbeat 120초 초과, 수량 불일치가 발생하면 즉시
   L1로 복귀한다.

## 6. 승인 문구 예시

> KIS mock 계좌에서 kill-switch를 L0으로 내려 GitHub 신호 기반 신규매수를
> 재개한다. 현 시점 실질 재개 범위는 B 슬리브(동시 최대 4종목, 시드
> 475만원)이며, A는 포지션 상한으로 차단되나 보유가 12개 미만으로 줄면 A
> 신규매수도 이 승인 범위에서 재개됨을 인지한다. `ALLOWED_SYMBOLS` 펜스를
> 설정하고, 정체청산 shadow·Oracle fallback 0·6종목 close-only 동결·실전
> 하드블록은 유지한다. stall live·fallback 1·동결 해제·실전 전환은 각각 별도
> 승인으로 둔다. `UNKNOWN`·원장 손상·heartbeat 120초 초과·수량 불일치 발생
> 시 즉시 L1로 복귀한다.

이 예시는 자동 승인이 아니다. Oracle의 최신 `l0 --broker` 결과를 확인한 뒤
사용자가 직접 승인해야 한다.
