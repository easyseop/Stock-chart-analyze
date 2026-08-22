# Claude 적대 재검토 요청 — 0체결 행 + stale hldg_before ACK

작성: Codex, 2026-08-22

브랜치: `codex/ack-zero-fill-stale-before`

기준: `origin/claude/happy-gauss-cwoq21 @ 9dd278e5`

구현 커밋: `61dce915`, 회귀 보강: `d5fd1b88`

## 1. 수정 목적

실물 `xe:INGR:half:2026-08-11#2`는 SELL ACK, `hldg_before=6`, 실제
총보유 11, NYSE nccs 0행, ccnl 단일 0체결 행이었다. 과거 매도가능 수량이
총보유 자리에 기록되어 잔고불변 경로가 영구 실패했고, 운영자 CLI는 ccnl 행이
있다는 이유만으로 자동 경로보다 더 좁게 거부했다.

동시에 열린 SELL이 손절 판단을 오래 막는 상태와, 열린 매도로 설명되지 않는
`총보유-매도가능` 차이가 무경보였던 문제를 읽기 전용 P0 관측으로 보완했다.

## 2. F1/F2 — 운영자 전용 `operator-zero-fill`

`scripts/kis_ack_resolve.py`가 자동 부재 대사와 동일한
`kis_reconcile._closed_zero_fill_row()`를 직접 호출한다. 판정 복제는 없다.

다음 조건을 전부 만족할 때만 새 kind가 열린다.

1. SELL, `submitted|ack`, 기존 filled 0
2. 전 거래소 nccs exact ODNO 완전 부재
3. ccnl exact ODNO 단일 0체결 행
4. 동일 심볼 broker in-flight 정확히 1건
5. ownership baseline 파일 정상, 사용자 baseline 심볼 아님
6. fresh 총보유 완전 조회 성공
7. `max(REJECT_ABSENCE_MIN_S, ACK_AGE_MIN_S)` 이상 경과
8. apply 때 비어 있지 않은 operator ack

이 경로는 recorded before와 fresh current를 비교하지 않는다. 대신 intent/result
감사 이벤트 양쪽에 아래를 남긴다.

```text
zero_fill_proof=true
hldg_before_recorded=<원장 원값>
hldg_now_observed=<fresh 총보유>
```

apply는 `ledger.reconcile(key, 0, open_order=False)`만 호출한다.
`kis_accounting.sync_fill`은 0회이며 rejected terminal 뒤에만 기존 operator
ack로 동결을 해제한다. `hldg_before` 원문은 덮어쓰지 않는다.

기존 `known_unchanged`, `operator_unknown_sell`, 자동 direct/balance/absence의
조건은 수정하지 않았다. INGR 픽스처에서 자동 3경로 결과가 모두 0임을 단언한다.

## 3. F3 — 열린 SELL/CANCEL 보호 스킵 가시화

신규 `bot/protection_observability.py`는 주문·kill·동결 변경 함수를 import하거나
호출하지 않는다. fresh KIS 총보유가 있는 열린 시장 범위에서만 다음을 관측한다.

- 보유수량 > 0
- 원장에 열린 SELL/CANCEL 존재
- 가장 오래된 관련 주문이 `PROTECTION_BLOCKED_ALERT_S`(기본 1800초) 이상

새 사고는 종목+경과분만 담아 P0 1회, 해소는 1회 발송한다. 수량·금액·계좌는
문구에 없다. 전송 성공 뒤에만 파일 래치를 원자/fsync 저장하며 재시작 뒤에도
중복을 억제한다. 기존 sentinel의 SELL/CANCEL 손절 skip 조건은 그대로다.

## 4. F4 — 설명되지 않는 매도가능 고갈

기본 10분(`SELLABLE_GAP_AUDIT_S`)마다 열린 시장의 완전 잔고와 완전 nccs를
KIS 기존 조회/페이지/유량 제한 경로로 읽는다. US는 NASD/NYSE/AMEX 전부,
KR mock은 기존 강한 미체결 폴백만 쓴다. 어느 잔고/미체결/행 수량이든 불신이면
부분 결과 없이 `None`으로 판정을 보류하고 기존 래치도 유지한다.

열린 SELL은 원주문 수량이 아니라 `ord_qty-filled` 잔여만 합산한다. 판정식은
지시서 그대로다.

```text
total > 0
and sellable < total
and (total - sellable) > broker_open_sell_remaining
```

새 사고는 종목과 `((total-sellable)-open_sell)/total` 비율만 P0로 보내고,
반복/회복은 동일한 영속 래치로 1회씩 처리한다. 발주 clamp와 주문 코드는
수정하지 않았다. 공개 ntfy는 `category="trade"`의 기존 category-only 본문을
그대로 사용한다.

## 5. 핵심 재현 결과

- INGR `before=6/current=11/ccnl zero 1/nccs 0/601s`
  → `operator-zero-fill` → rejected → unfreeze, accounting 0
- 같은 입력의 자동 direct/balance/absence → 전부 0건
- 양수 ccnl → 기존 `direct-fill`
- zero 2행, nccs 생존, 599초, BUY, partial, cancel_pending, 동일심볼 2건,
  baseline, 미armed, 총보유 비숫자, 조회 예외 → 전부 거부
- F3: 60초+ 보호 스킵 P0 1회, 재시작 중복 0, 해소 1회
- F4: 11/1/open0 P0, 11/6/open5 침묵, 11/1/open5 P0
- 부분체결 SELL 5중 2체결은 열린 설명량 3으로 계산
- 잔고 또는 nccs 한 거래소 실패 → 전체 None, 경보/회복 0
- P0 전송 실패 → 래치하지 않고 다음 사이클 재시도

## 6. 검증 증거

```text
python -m tests.test_ack_unit_mismatch                 rc=0
python -m tests.test_protection_observability          rc=0
python -m tests.test_sentinel                          rc=0
python -m tests.test_sentinel_chase                    rc=0
python -m tests.test_sentinel_heartbeat_progress       rc=0
python -m tests.test_kis_boot                          rc=0
python -m tests.test_ops_status                        rc=0
python -m tests.run_all                                rc=0
ALL PASS: Python test modules 74
node --test tests/site_math.test.js                     rc=0 (19/19)
node --check scanner/site_app/app.js                    rc=0
python -m compileall -q bot scripts tests               rc=0
git diff --check                                        rc=0
```

소스 게이트를 고의 제거하는 mutation은 안전 실행기가 실제 브랜치와 detached
복제본 모두에서 “명시적 추가 승인 필요”로 차단했다. 우회하지 않았다. 대신 위
각 방어를 독립 입력 행렬과 호출 spy로 직접 고정했다. mutation 증거가 승인 조건이면
사용자가 별도로 허용한 뒤 detached 복제본에서만 수행해야 한다.

## 7. 적대 재검토 요청

1. `_closed_zero_fill_row` 공유가 실질적인가. CLI 안에 다른 zero 판정 복제가 있는가?
2. `known_unchanged`·`operator_unknown_sell`·자동 3경로가 느슨해진 diff가 있는가?
3. 599초/CVNA 직후 zero 행/양수 체결/zero 2행/nccs 생존이 잘못 terminal 되는가?
4. stale before 경로에서 `sync_fill`이 어떤 예외 경로로든 호출되는가?
5. 총보유 조회는 비교가 아니라 fresh 증거와 감사에만 쓰이며, 불신이면 거부하는가?
6. 사용자 baseline·미armed·동일심볼 2건이 새 운영자 경로를 우회하는가?
7. F3이 짧은 정상 in-flight를 경보하거나 기존 SELL/CANCEL skip을 바꾸는가?
8. F3/F4 래치가 전송 실패·재시작·닫힌 시장에서 잘못 전이하는가?
9. F4가 부분체결의 원수량을 빼 정상 예약을 오탐하는가?
10. US 거래소 하나 또는 KR fallback이 불신인데 다른 시장 합계로 세탁하는가?
11. 메시지나 공개 ntfy에 수량·금액·계좌·ODNO가 노출되는가?
12. 새 모듈 import graph에 주문·kill·unfreeze mutation 경로가 생겼는가?

P0~P3로 판정하고 P0/P1이 하나라도 있으면 병합 차단을 요청한다.

## 8. 금지선

기본 브랜치 병합, Oracle 배포, 운영 `kis_ack_resolve --apply`, kill/env 변경은
수행하지 않았다. INGR 운영 apply도 이 코드 검토·사용자 별도 승인 전에는 금지다.
