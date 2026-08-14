# Claude 적대 검토 요청 — KIS 잔고·주문 연속페이지 완전성

검토 브랜치: `codex/balance-pagination`

기준: `claude/happy-gauss-cwoq21@7ff6e9c4`

핵심 구현 체크포인트: `f12a047`

우선순위: **높음 — 실계좌 전환 선행조건**

## 판정 기준

- P0/P1 하나라도 있으면 병합·Oracle 배포를 차단한다.
- 페이지 중 하나라도 실패하거나 형식·연속키를 신뢰할 수 없으면 누적 행을 전부
  버리고 `None`이어야 한다. 부분 잔고·부분 주문 목록 반환은 허용하지 않는다.
- 주문 POST·취소·kill·사이징·세션·신선도·live/mock 폴백 계약은 바뀌면 안 된다.
- 코드 병합과 Oracle 배포는 Claude 검토 뒤에도 사용자 별도 승인이 필요하다.

## 구현 요약

### P1. 완전소진 페이지 루프

- `bot.kis._get_all_pages()`가 첫 페이지를 빈 컨텍스트로 조회하고, 다음 페이지부터
  직전 `CTX_AREA_FK100/200`·`CTX_AREA_NK100/200`의 공백만 제거해 그대로 전달한다.
- 다음 요청은 공식 KIS 연속조회 계약에 맞춰 `tr_cont: N` 헤더를 보낸다. 응답의
  FK/NK, `tr_cont` F/M, `msg_cd` 끝자리 `12000` 중 하나라도 다음 페이지를 뜻하면
  계속 조회한다.
- 각 페이지는 기존 `_get()`을 거치므로 프로세스 공유 `_LIMITER`를 매번 통과한다.
- 행 배열은 누적하고 `output2` 등 요약은 마지막 페이지 값을 쓴다. 완전히 소진한
  결과에서 컨텍스트와 연속 헤더를 제거하고 `_pagination_complete=True`,
  `_pagination_pages=N`을 내부 완전성 증거로 붙인다.
- 중도 `None`, `rt_cd!=0`, 행/컨텍스트 형식 불신, 연속키 반복, 다음 신호가 있으나
  키 없음, `KIS_MAX_PAGES` 초과는 모두 `None`이며 부분 결과는 반환하지 않는다.
- 해외 잔고·미체결·체결 3경로와 국내 잔고·미체결/체결 4경로, 총 7개 조회에 적용했다.

### P2. 성공 응답의 소비자 불신 원인

- `holdings`, `sellable_holdings`, `positions_detail`이 HTTP 성공 뒤 행/수량/연속성
  검증에서 거부할 때 `BalancePaginationIncomplete`, `BalanceRowsUntrusted`,
  `PositionsQuantityUntrusted` 등 구체 예외 라벨을 남긴다.
- 단일 전역 사유 대신 `ContextVar`를 사용해 sentinel·ops 등 서로 다른 스레드의
  마지막 실패 원인이 덮이지 않게 했다. 토큰·계좌·응답 원문·심볼은 저장하지 않는다.

### P3. SELL 거절 부재증명 완전성

- `trusted_response_rows()`는 `_pagination_complete=True`와 유효한 페이지 수가 있는
  완전 병합 응답을 인정한다. 따라서 15/100행을 넘는 병합도 거짓 불신하지 않는다.
- 명시적 미완 표시, 페이지 수 0/불신, 남은 컨텍스트·F/M·12000, 비배열 행은 계속
  거부한다. 완전성 마커가 없는 레거시 응답은 기존 15/100행 포화 휴리스틱을 유지한다.
- 국내 holdings 병합도 명시적 미완 표시와 F/M을 거부한다. 조회 실패를 부재로
  바꾸거나 열린 SELL ACK를 자동 종결하는 새 경로는 없다.

## 필수 8개 테스트

`tests/test_balance_pagination.py`는 아래 여덟 함수를 단독 실행한다.

1. `test_two_pages_merge_and_next_header_contract`: 두 페이지 행/마지막 요약 병합,
   컨텍스트 제거, 다음 요청 파라미터와 실제 `tr_cont=N` 헤더.
2. `test_middle_page_none_or_rt_failure_discards_everything`: 중간 `None`·`rt_cd=1`은
   전체 `None`, 호출 2회, 구체 Pagination 라벨.
3. `test_repeated_context_is_finite_and_untrusted`: 반복 키와 키 없는 F 헤더를 유한
   호출 뒤 거부.
4. `test_page_limit_discards_partial_rows`: 상한에서 다음 페이지가 남으면 거부하고,
   상한의 마지막 페이지에서 소진되면 정상 병합.
5. `test_single_page_keeps_data_and_calls_once`: 단일 페이지 1회 호출과 기존 데이터,
   최초 요청의 낡은 컨텍스트는 HTTP 전에 거부.
6. `test_holdings_and_positions_accept_exhausted_merge_and_label_drops`: 30행 완전 병합
   정상 처리, 미완/수량 오염의 비-unknown 라벨, 두 스레드 원인 격리.
7. `test_trusted_rows_accept_only_proven_complete_merge`: 완전 30행 인정, 명시 미완·
   페이지 수 0·남은 키·마커 없는 포화 응답 거부.
8. `test_all_balance_and_order_queries_use_page_helper`: 대상 7개 조회가 모두 헬퍼와
   올바른 100/200 컨텍스트 계약을 사용.

실행 결과: `KIS balance pagination 8/8 PASS`, 종료코드 0.

## 뮤테이션 증거

지시서대로 핵심 구현을 `f12a047`에 먼저 커밋한 뒤 별도 임시 worktree에서 한 번에
하나씩 변이하고 원복했다. 네 변이 모두 전용 테스트가 종료코드 1로 실패했다.

| 변이 | 잡은 테스트와 원문 핵심 |
|---|---|
| M1 다음 페이지 `tr_cont=N` 제거 | `test_two_pages_merge_and_next_header_contract`, `AssertionError`, exit 1 |
| M2 중간 `None`에서 누적 부분행 반환 | `test_middle_page_none_or_rt_failure_discards_everything`, `assert out is None`, exit 1 |
| M3 반복 컨텍스트 가드 제거 | `test_repeated_context_is_finite_and_untrusted`, 3번째 호출 `StopIteration`, exit 1 |
| M4 완전 병합도 15행 포화로 불신 | `test_trusted_rows_accept_only_proven_complete_merge`, 완전 30행 `None`, exit 1 |

원복 뒤 전용 8/8과 `git diff --check`를 다시 통과했다.

## 기존 회귀 증거

아래 변경 인접 모듈은 모두 종료코드 0이다.

- `test_kis`, `test_kis_domestic`, `test_sentinel`, `test_kis_boot`
- `test_sell_reject_reconcile`, `test_l1_readiness`, `test_ops_status`
- `test_kis_reconcile`, `test_kis_reconcile_kr`
- `python3 -m compileall -q bot tests`
- `git diff --check`

시스템 Python은 `pandas/numpy` 부재로 분석 모듈 9개를 import하지 못했으나,
Codex 번들 Python으로 같은 `python -m tests.run_all`을 재실행해 **57/57 ALL PASS**,
종료코드 0을 확인했다. Draft PR CI도 최종 원격 무손상 증거로 확인한다.

## Claude 필수 반례

1. 3페이지 중 2/3페이지의 HTTP·rate-limit·JSON·`rt_cd` 실패가 앞 행을 반환하는가?
2. FK만, NK만, 대소문자 키, 양쪽 키, 공백 키가 다음 요청에서 임의 변형되는가?
3. F/M 또는 12000인데 키가 없을 때 성공으로 세탁되는가?
4. 같은 키 반복, A→B→A 순환, `KIS_MAX_PAGES` 경계가 무한 호출/부분 반환을 만드는가?
5. 마지막 페이지 `output2`가 없는데 이전 페이지 요약이 잔존하는가?
6. 국내/해외 또는 잔고/주문별 행 키가 다른 경로에서 빈 목록으로 세탁되는가?
7. 각 추가 페이지가 공유 limiter를 우회하거나 첫 페이지인데 불필요한 추가 호출을 하는가?
8. `ContextVar` 때문에 기존 단일 스레드 경보 사유가 사라지거나 다른 스레드의
   HTTP 실패를 소비자 불신 라벨이 덮는가?
9. 수량 NaN/inf/문자열/음수 또는 행 구조 오염이 빈 보유로 바뀌는가?
10. 사용자가 만든 dict의 내부 완전성 마커 없이 15/100행 포화 응답이 부재 증명되는가?
11. 완전 병합 마커가 있어도 남은 컨텍스트·F/M·12000이 있으면 거부되는가?
12. 상한/중도실패에서 완전성 마커가 생성되는 크래시 창이 있는가?
13. SELL 거절 대사가 부분 주문 목록으로 “주문 없음”을 증명해 ACK를 종결하는가?
14. mock 국내 미체결 대체조회와 live 폴백 금지 계약이 페이지 루프 뒤에도 유지되는가?
15. diff에 주문 POST·kill·사이징·매매 단계 허용 조건 변경이 0인가?

추가 반례를 mutation/fault injection으로 계속 탐색하고 P0~P3로 판정해 달라.
P0/P1이면 병합 차단이며, P2/P3도 파일·줄·재현 입력·영향·최소 수정안을 명시해
달라. 이 브랜치는 서버와 주문 상태를 변경하지 않았고 병합·Oracle 배포도 하지 않았다.
