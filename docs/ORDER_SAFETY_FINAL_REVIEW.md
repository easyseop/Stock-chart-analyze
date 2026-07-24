# 최종 재검토 요청 — KIS 주문 경합·총시드·손절·성과

> 상태 기록: 이 문서에 대한 외부 재검토에서 P0 E와 P1 Q1이 추가 발견됐고 로컬에서
> 다시 수정했다. 최신 검토 기준은 `docs/ORDER_SAFETY_REREVIEW_2.md`이며, 이 문서는
> 1차 재검토 입력의 역사 기록으로 보존한다.

마지막 갱신: 2026-07-25
기준 커밋: `106065d2`
로컬 브랜치: `codex/p0-order-protection`

## 검토 목적

외부 보고서 `검토보고서_주문경합_총시드_손절.md`의 P0 4건, P1 12건,
P2 1건, P3 1건을 모두 수정한 로컬 diff를 적대적으로 재검토한다.

이 변경은 아직 커밋·push·Oracle 배포하지 않았다. Oracle은 기준 커밋을 실행하며
kill-switch L1(`buy_new=False`, `protect_sell=True`)을 유지한다. 검토 승인 전에는
신규매수를 열거나 L1을 내리지 않는다.

## 비협상 불변식

1. 주문 ACK는 체결이 아니다.
2. 미체결 BUY가 있어도 이미 체결된 수량의 보호선은 계속 관리한다.
3. SELL 전에는 BUY 취소 **확정**과 최신 KIS 매도가능수량을 확인한다.
4. 잔고·체결·주문번호가 불명확하면 추측 주문하지 않는다.
5. A·B 각각의 한도와 A+B 총시드−5% 완충을 모두 만족해야 한다.
6. 브로커 보유원가 스냅샷이 없으면 원자 총시드 게이트를 우회하지 못한다.
7. 모든 HTTP 재시도는 새 유량 슬롯을 사용하고 여러 프로세스가 같은 한도를 공유한다.
8. 원장 손상·전송 직후 크래시·동시 프로세스 경합은 모두 fail-closed다.
9. `SENTINEL_LIVE=0`은 KIS 매도를 실제로 전송하지 않으며, dry-run은 나중의
   live 손절을 영속 멱등키로 막지 않는다.
10. 계좌 성과와 종목 선택 품질을 섞지 않는다. 계좌는 현금흐름 중립 TWR,
    종목 선택은 장 시작 보유 동일가중 전일종가 대비로 분리한다.

## 보고서 항목별 수정 대응

| 원 항목 | 수정 내용 | 핵심 자동검사 |
|---|---|---|
| P0 E | BUY/SELL/CANCEL 상태를 분리. 손절 시 BUY 계획 종료, 제출 주문 취소 접수, 미체결 소멸 확인, 최신 잔고 재조회 후 SELL | `test_sentinel`, `test_kis_pending`, `test_kis_exits` |
| P0 F | 절반익절 ACK 0주는 상태 확정 안 함. 확인 체결 누적이 목표에 도달한 뒤에만 `half=True`와 본전 래칫 | `test_kis_exits` |
| P0 D | 잔고 선반영 시 BUY 원장의 유일한 stop을 임시 보호선으로 사용. 없음/충돌은 종목 동결 | `test_ledger`, `test_sentinel` |
| P0 J-b | KIS 잔고 실패 시 공개 feed 수량 사용 금지. 60초 이내 마지막 정상 잔고는 감시만, 주문 직전 sellable 재조회 | `test_sentinel`, `test_kis_brokertruth` |
| P1 A | 잔고에 먼저 나타난 B 보유도 원장 `sleeve/pos_key`로 B 귀속 | `test_kis_buyloop` |
| P1 B | `BOT_OPERATING_TOTAL_KRW`, 기본 5% 완충, A:B=30:5 비율 배분, 원장 flock 안에서 최종 교차게이트 | `test_envelope`, `test_kis_buy_gates`, `test_ledger` |
| P1 B.2 | sizing 0을 half가 1주로 승격하지 않음 | `test_kis_buy_gates` |
| P1 C | pending B도 같은 브로커 held+전체 예약 스냅샷과 B 시드를 사용 | `test_kis_pending` |
| P1 G | 빈 계좌 첫 매수도 신선한 파수꾼 heartbeat 필수 | `test_kis_buy_gates` |
| P1 H | HTTP 요청마다 슬롯 재획득, flock 공유 윈도우, 웹은 공유 캐시만 | `test_kis`, `test_kis_orders`, `test_kis_ratelimit`, `test_site_app` |
| P1 J-a | `SENTINEL_LIVE`를 KIS SELL과 chase의 최종 전송 게이트로 사용 | `test_sentinel`, `test_sentinel_chase` |
| P1 I1 | append 단일 write 뒤 파일 fsync, 최초 생성 시 디렉터리 fsync | `test_ledger` |
| P1 I2 | JSONL 손상 감지 시 모든 신규 주문 전면 차단 | `test_ledger`, `test_kis_boot` |
| P1 I3 | `try_record_submit`이 검사+예산+선기록을 하나의 flock 임계구역에서 처리 | `test_ledger`, `test_kis_orders` |
| P1 K1 | 시장×A/B 보유 NAV에서 매수·매도 현금흐름을 제거한 TWR로 교체 | `test_alpha` |
| P1 I4 | 90초 초과 `submitted`를 부팅 시 UNKNOWN으로 승격해 브로커 대사 전 잠금 | `test_ledger`, `test_kis_boot` |
| P2 A.2 | 동종목 예약을 리스트로 유지하고 부분체결 뒤 잔량도 합산 | `test_kis_buyloop`, `test_kis_pending` |
| P3 FP3 | 지수 전일종가를 별도 저장. carry 없는 첫날만 첫 표본 기준이라고 표시 | `test_alpha`, `test_site_app` |

추가 방어도 포함한다.

- 동일 취소 키를 프로세스 간 원자적으로 1회만 선기록해 중복 취소 전송을 막는다.
- 최종 BUY 전 브로커 A/B 원가가 누락되면 `budget` 게이트에서 차단한다.
- 체결회계와 KIS 잔고 반영 시차에는 둘 중 큰 원가를 held 바닥으로 사용한다.
- 열린 BUY가 있어도 손절선 래칫은 계속하고, 실제 익절·타임스탑 SELL만 취소 대사
  전까지 보류한다.
- dry-run 손절 판단을 `sentinel_sent.json` 완료로 쓰지 않는다.

## 장애 주입 10개

| # | 시나리오 | 기대 결과 |
|---|---|---|
| 1 | B BUY ACK→잔고 선반영→포지션 기록 지연 | B 귀속·B 원가·임시 stop 유지 |
| 2 | BUY 10 중 6 부분체결, 잔량 4, 손절 이탈 | 취소 확정 전 SELL 없음, 확정 뒤 최신 sellable만 SELL |
| 3 | SELL ACK 0→다음 주기→뒤늦은 체결 | half/본전 래칫은 확인 체결 뒤 1회 |
| 4 | 빈 계좌 첫 매수 직전 heartbeat 없음 | `sla` 차단 |
| 5 | 동종목 1차+눌림 계획 공존 | 두 예약과 부분 잔량 모두 합산 |
| 6 | A/B 각각 한도 내, 합계만 완충 한도 초과 | 원장 원자 총예산 게이트 차단 |
| 7 | EGW00201 1회 후 성공+두 프로세스 동시 | HTTP마다 슬롯, 프로세스 합산 한도 준수 |
| 8 | fsync 전후 크래시·손상 JSON·두 writer 경합 | 손상 전면 잠금, submit 정확히 1건 |
| 9 | KIS 잔고 실패+공개 feed 과거 보유 | feed로 KIS SELL 없음 |
| 10 | 급변해 실제 BUY 지정가가 stale quote보다 높음 | 마켓터블 BUY 지정가×전체 계획수량으로 예약 |

## 검토자가 집중할 반증 질문

1. filled BUY가 costbook에는 반영됐지만 KIS 잔고가 늦는 순간, 또는 그 반대
   순서에서도 총시드가 과소계상되는가?
2. half 1차가 전체 계획을 예약하고 2차 plan이 이어받는 전환점에서 이중예약 또는
   예약 소실이 있는가?
3. 원장 손상과 공용 레이트리미터 손상이 동시에 나도 주문이 열리는 경로가 있는가?
4. 두 프로세스가 같은 SELL 또는 같은 취소 키를 동시에 실행할 때 HTTP가 두 번
   나갈 수 있는가?
5. dry-run 뒤 프로세스를 재시작해 `SENTINEL_LIVE=1`로 바꾸면 기존 보유의 보호
   주문이 멱등키에 막히는가?
6. B 목표청산, A 절반익절, 손절이 열린 BUY와 겹칠 때 추가체결을 포함해
   초과매도가 가능한가?
7. TWR에서 매수·부분매도·전량매도·재진입·환율 변화가 의도대로 분리되는가?
8. 장중 수동 신규매수가 “장 시작 보유 동일가중”에 섞이는가?

## 검증 명령과 현재 결과

```bash
python -m tests.run_all
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --check scanner/site_app/app.js
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --check scanner/site_app/portfolio_math.js
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --test tests/site_math.test.js
python -m compileall -q bot scanner tests
git diff --check
```

- Python 독립 테스트 모듈: `41/41`
- 사이트 계산 Node 테스트: `5/5`
- 데스크톱 1280px, 모바일 390px·320px 실물 렌더 확인
- 종목 상세 준실시간/일봉 차트, 지수 비교, 6개 하단 메뉴 확인
- 가로 스크롤 0, 브라우저 warning/error 0

최종 승인 조건은 단순 테스트 통과가 아니라 위 불변식과 10개 장애 주입의
입력→상태→주문수량 assertion이 실제로 반례를 막는지 확인하는 것이다.
