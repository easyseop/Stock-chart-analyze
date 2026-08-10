# Claude 적대 재검토 판정 — SELL 거절 대사 R0~R5 (V2)

검토일: 2026-08-11 · 대상: `SELL_REJECT_RECONCILE_CLAUDE_REVIEW_V2.zip`
(base `c3949ace`, 구현 핵심 `940cd19`, 브랜치 `codex/sell-reject-reconcile` —
원격 미푸시라 ZIP 전체 트리를 기준으로 검증)

## 판정: **조건부 승인 — P0 0 · P1 0 · P2 2 · P3 3**

병합 차단 사유(P0/P1) 없음. P2 2건은 작은 수정(테스트 1개 + 래치 순서)이라
**병합 전 반영을 권장**하며, 반영 여부와 무관하게 병합·Oracle 배포는 별도
사용자 승인 사항이다.

## 독립 검증 결과 (검토자 직접 실행)

- 집중 9모듈(test_sell_reject_reconcile·kis_boot·kis_reconcile·kis_ack_resolve·
  kis_accounting·kis_exits·ops_status·kis_telegram·l1_readiness) **전부 PASS**.
- `tests.run_all`: 52모듈 중 51 PASS + `test_ownership_baseline` FAIL —
  검토 워크트리가 /tmp 아래라 영속경로 단정이 걸리는 **알려진 환경
  아티팩트**(V8~V10 검토와 동일), 코드 결함 아님.
- `compileall`, `node tests/site_math.test.js`(19/19), `node --check app.js`,
  `git diff --check` 통과.
- 전이 호출 검증: `ledger.orders_for(key_prefix=)`·`kis.fills(start,end)`·
  `domestic_fills(start,end)`·`resolve_acks_by_balance(only_keys=)` 시그니처
  실존 확인. `complete_snapshot=True` 제거는 약화가 아님 — `only_keys`가 동일
  활성화 가드이고 US 부분 스냅샷 차단은 호출부 merged=None 경로가 유지.
- 검토자 자체 뮤테이션 3종(Codex M1~M12와 비중복):
  - **MA** `open_count != 1` 단독 귀속 가드 제거 → 관련 6모듈 전부
    **SURVIVED** (→ P2-1)
  - **MB** 잔고 불변 판정을 `==`→`<=`로 약화 → `test_sell_reject_reconcile`
    **KILLED** (AssertionError, exit 1)
  - **MC** R5 durable_session(원장 기반 세션 집계) 무력화 → `test_kis_exits`
    **KILLED** (`assert key is None and capped and notice`, exit 1)

## P2 (병합 전 반영 권장)

**P2-1. 계약된 방어 "같은 심볼 in-flight 1건 제한"이 무시험 (MA 생존).**
`resolve_acks_by_absence`의 `open_count.get(symbol) != 1` 가드는 검토요청
질문 7("다른 broker in-flight가 있으면 오래된 ACK를 단독 귀속하지 않는지")로
계약된 방어인데, 제거해도 관련 6모듈이 전부 통과한다. ODNO 부재+잔고 불변이
여전히 요구되어 즉시 오정산 시나리오는 구성하지 못했지만(그래서 P1 아님),
무시험 방어는 다음 리팩터에서 소리 없이 사라진다. 같은 심볼에 fresh
in-flight가 공존하는 회귀 테스트 1개를 추가하라.

**P2-2. 경보 래치가 전송 성공 확인 전에 잠긴다.**
- `ops_status.maybe_alert_stuck_acks`: `_swap_stuck_latch(current)`로 래치
  파일을 **먼저** 교체한 뒤 `notify.send`를 호출하고 반환값을 버린다.
- `kis_boot._update_status`: `failure_alerted=True`를 상태 저장 시점에 잠근
  뒤 알림을 시도한다.
텔레그램 일시 장애(이번 주말 실측된 유형)와 겹치면 "행별/임계 1회" 경보가
**0회**가 된다. 전송이 True일 때만 래치하거나, 실패 시 다음 사이클 재시도로
바꿔라(중복 위험보다 유실 위험이 크다 — 둘 다 안전 경보다).

## P3 (비차단 관찰)

1. stuck-ack 래치 파일을 변화가 없어도 매 루프 flock+fsync+replace로
   재기록 — `current == previous`면 쓰기 생략 권장.
2. 해외 페이지 상한(15행) 도달 시 그날 ODNO 경로 전체 보류 — 방향은
   fail-safe지만 바쁜 날 체결 확정이 잔고 경로로만 이뤄져 fill_price
   정밀도가 낮아진다. 연속조회 전체 순회로 개선 여지.
3. ack 접수 직후 사이클에 "세션 재시도 상한 도달 · 다음 세션까지 대기"
   알림이 뜰 수 있다(주문은 정상 대기 중) — 문구가 실패로 오독될 소지.

## 확인된 것들 (질문 20 + V2 반례 9 기준 요지)

- 조회 실패 ≠ 부재: `trusted_response_rows`가 rt_cd·연속키·`tr_cont` 헤더·
  `msg_cd=…12000`·페이지 포화를 전부 불신 처리(None)하고, None/[]가 어떤
  경로에서도 합쳐지지 않음(M1·M8 + 코드 추적으로 확인).
- 미국 3거래소 union + 하나라도 실패 시 부재 보류(M7), KR mock 폴백의
  live 금지 유지(M5), 600s 게이트(M2), ODNO 정규화 양쪽 적용, 부분체결·
  잔고모순 시 자동정산 금지+모순 경보 1회(M3·M4·MB).
- ownership: baseline 미장전 시 부재 증명 전면 차단, 동결·baseline 종목은
  legacy 이관 SELL의 좁은 예외(pos_key·수량·costbook lot 전부 일치)만.
- R5: 과거 고정 키가 attempts에 포함돼 #2부터 이어짐, 세션 상한은 미국
  뉴욕 거래일 기준(M11), durable 원장 집계가 상태파일 리셋을 보완(MC),
  이중매도는 기존 원장 잠금이 우선.
- R2/R4: 대사 건강 상태 파일(0600·flock·fsync·원자교체) + `/진단` 표기,
  ops JSON은 `stuck_acks` 숫자만(시크릿·심볼·금액 없음).
- R3: 행 사유만 `broker_reason` 승격, 응답 상위 메시지는 `msg_source=response`
  로 구분, 부재 증명은 "사유 미상" 고정, meta whitelist·200자·제어문자 제거.
- 핫픽스(`_sell_fail_alert_due`)와의 합성 유지 — 상한 도달 알림과 이중
  발송 없음(cap_field 래치 교차 확인).

## 재검토 조건

P2 2건 반영 후: 신규 테스트 이름과 래치 순서 diff, 그리고 기존 스위트
무손상 증거만 제출하면 된다(전면 재검토 불필요 — 해당 부위만 확인).
