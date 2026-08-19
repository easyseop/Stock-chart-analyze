# Claude 적대 재검토 요청 — CVNA zero-fill·forensic 회계 복구·총위험 원자 게이트

검토 대상: `codex/riskcap-cvna-recovery`

기준: `a09c8b2`

구현 설명/증거: `docs/CODEX_REVIEW_CVNA_ZERO_FILL_ACCOUNTING_RECOVERY.md`

## 판정 규칙

- P0/P1 하나라도 있으면 병합·Oracle 배포·CVNA apply 차단.
- 코드 검토만 하지 말고 아래 반례를 독립 임시 원장과 mock KIS로 재현.
- mutation은 한 번에 하나씩 적용하고, 대응 테스트 실패명과 exit code를 기록.
- 사용자 승인 전 실제 Oracle 세 원장과 kill/env/service를 변경하지 말 것.

## 1. T2a zero-fill 반례

1. CVNA 원문 순서: ACK → 98초 뒤 ccnl closed/filled=0 → 이후 실제 74주 체결.
   0주 종결·예약해제가 일어나지 않고 최종 74주 회계로 이어지는가.
2. 599초와 601초 경계에서 600초 하한이 정확한가. env 30/90도 하한을 낮추지
   못하는가.
3. 완전 잔고가 +74면 체결이 zero-fill보다 우선하고, +1~73·+75·조회 실패·형식
   불신이면 자동 귀속/거절 없이 UNKNOWN + P0인가.
4. 잔고 불변과 grace 경과가 모두 증명된 진짜 zero-fill만 rejected가 되는가.
5. 닫힌 0주 후보가 LOW/done으로 강등돼 다른 주문이 예산을 쓰는 경로가 없는가.
6. SELL R1~R5의 완전 부재증명, open_count 가드, 미국 거래소 union, 알림 래치가
   퇴행하지 않았는가.

## 2. T2b forensic plan/apply 반례

1. plan/apply 각각 KIS 체결·세 거래소 잔고를 fresh 조회하며 `None`·부분페이지·
   거래소 충돌·ODNO 부재를 0건으로 오독하지 않는가.
2. plan SHA 변조, 만료, 다른 cwd/원장 경로, old backup-dir, 서비스/heartbeat/
   수동 프로세스 활성 상태가 mutation 전에 모두 거부되는가.
3. costbook append 직후, position repair 직후, accounted 직후, 완료 meta 직후
   크래시를 각각 주입해 재실행 시 exact cost 6,640,863원·74주가 한 번만 남는가.
4. position repair가 기존 보호 74주에 더해 148주를 만들지 않고 stop 60.48,
   order key/pos key/슬리브/통화를 보존하는가.
5. recovery pending 중 normal `sync_fill`, reconcile, budget snapshot이 예약을
   풀거나 중복 lot을 쓰지 않는가.
6. 동일 심볼 기존 lot, baseline 사용자 보유, 손상 원장, KIS 수량 변화가 모두
   fail-closed인가.
7. apply 코드·전이 import를 포함해 주문 HTTP/취소/kill 하향이 0건인가.
8. lock 계층에 역순이나 네트워크 호출 중 flock이 없어 교착하지 않는가.

## 3. T2c 수동 adopt 반례

1. baseline 쓰기/rename/fsync 실패와 파일 손상 때 kpos close가 0건인가.
2. baseline 성공 직후 crash, close 직후 crash, 재실행에서 보호 공백·중복 변화가
   없는가.
3. adopt가 costbook·ledger·freeze·SEED·kill을 건드리지 않는가.
4. proven baseline만 sentinel/orphan에서 제외되고 baseline 부재·손상은 보호를
   끄는 허용 신호가 되지 않는가.
5. CVNA에 adopt를 적용하는 자동/문서 경로가 남아 있지 않은가.

## 4. T1 총위험 원자 게이트 반례

1. 현재 risk 8%, active BUY 예약 1%, 신규 1%가 경계에서 차단되는가.
2. 서로 다른 두 프로세스가 동시에 1%씩 제출할 때 ledger flock으로 하나만
   통과하는가.
3. 동일 buyloop 사이클 두 후보가 각각은 통과해도 합계 cap을 넘으면 두 번째가
   전송 전에 차단되는가.
4. half plan·parent/child·rejected/filled/accounted 전이에서 예약위험을 중복 또는
   누락하지 않는가.
5. qty/fx/entry/stop/risk meta의 bool·NaN·inf·음수·문자열·caller override가
   fail-closed인가.
6. kpos/costbook/ledger 손상·읽기 실패가 0으로 세탁되지 않는가.
7. cap 정확 경계, 래칫 stop>=entry, KRW/USD 변환이 맞는가.

## 5. 이력·섀도 회귀

1. A 252봉 미만은 후보만 제외되고 캐시/웹/B 수집은 유지되는가.
2. B1/B2 NaN/inf·불완전 frame이 신호/비표준 JSON이 되지 않는가.
3. B2와 ablation이 `now`/`shelf` 실행 후보에 합류할 경로가 정말 0인가.
4. B2가 미래 봉을 참조하지 않고 A ablation이 단일 게이트 실패만 기록하는가.

## 6. 제출 요청

- P0~P3 목록과 파일·줄·재현 조건·영향·최소 수정안.
- 위 반례별 HOLDS/BROKEN 표.
- 전체 Python 69모듈, Node 19, compileall, diff-check 결과.
- 최종 결론을 `병합 가능/차단`, `Oracle 코드 배포 가능/차단`,
  `CVNA apply 가능/차단`으로 **서로 분리**해 판정.
