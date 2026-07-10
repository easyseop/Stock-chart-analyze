# 파일 06 — Codex 리뷰 반영 확정본 (v2 오버라이드)

> 이 파일은 `00~05` 설계에 대한 Codex 5부 리뷰를 검토·판정해 **확정된 변경**이다.
> 아래 항목은 원본 태스크를 **오버라이드**한다(충돌 시 이 문서 우선). 구현·재리뷰 시
> 각 원본 태스크 + 여기의 델타를 함께 본다.
>
> 판정 원칙: Codex 리뷰 품질 높음 → 대부분 채택. 2개만 강도 하향(수정채택).

---

## A. 스펙 재검증으로 드러난 신규 사실 (가장 중요)

OpenAPI 스펙 직접 재파싱 결과:

1. **조건주문 CRUD 전부 실재** — `POST/GET /conditional-orders`, `GET/DELETE
   /conditional-orders/{id}`, `POST /conditional-orders/{id}/modify`. 상세응답에
   `first/second` leg + `triggeredOrderId`. → **CO1 신설 타당·구현 가능.**

2. ★★ **`clientOrderId`가 읽기 응답에 없다.** `POST /orders` 응답(OrderResponse)에만
   존재. `GET /orders/{id}`·`GET /orders`(목록/히스토리)가 주는 `Order`엔 **orderId만.**
   → **clientOrderId는 사실상 write-only.** POST 응답을 잃으면(순수 timeout) 이후 어떤
   조회로도 "내 clientOrderId였다"를 **확인 불가** — `symbol+side+수량+시간창` **추정
   매칭**만 가능. **이것이 Stage 2 최대 리스크.**

### A-1. UNKNOWN 대사 전략 재정의 (O1·O2·O4 오버라이드)
- **1순위(유일하게 확실): 10분 내 같은 clientOrderId로 POST 재시도** → 멱등으로 이전
  결과(orderId 포함) 회수. **네트워크 복구되면 9분30초 이내 재시도가 정답.**
- **10분 초과 or 재시도 실패**: `GET /orders?status=CLOSED&symbol=&from=&to=`로 후보를
  긁어 (side·수량·시간창)으로 **추정 매칭**. 조건주문 발동분은 `triggeredOrderId`로 연결.
- **추정이 모호하면(같은 종목/날 복수 주문·앱 주문 개입) → `MANUAL_REVIEW_LOCK`**
  (자동 해제 금지, 사람 확인까지 그 종목 자동매매 정지).
- **Stage 2 hard No-Go 관문**: "orderId 없이 당일 주문을 **명확히** 식별 가능한가"를
  V2에서 실측 — **모호하면 Stage 2 금지**(추정 매칭만으로 실매도 재개 불가).

---

## B. 신규 태스크 — CO1 조건주문 primitive (P1 앞에 필수)

- **무엇**: 새 파일 `bot/toss_conditional_orders.py` — 조건주문 생성/조회/목록/취소/정정 +
  상태 대사 + `conditionalOrderId` 원장 저장 + `triggeredOrderId` 연결.
  - `create_conditional_order(seq, symbol, type, conditions, order_type, expire_date,
    client_order_id, confirm_high_value=False)`
  - `get_conditional_order(id)` · `list_conditional_orders(seq, status=OPEN|CLOSED, symbol)`
  - `cancel_conditional_order(id)` · `modify_conditional_order(id, ...)`
  - `map_conditional_status(status)` — WATCHING/HOLDING/ORDERING/ORDERED/COMPLETED/
    EXPIRED/CANCELED + 최상위 status. **미지 status = `unknown_locked`**(rejected 아님).
- **왜**: P1은 "등록"만 있고 **생명주기 관리(만료 갱신·고아 취소·발동주문 추적)**가 불가.
  등록 후 conditionalOrderId를 원장에 못 남기면 다음날 어떤 조건주문을 갱신할지 모름 →
  중복 등록 → 앱/API/파수꾼 충돌.
- **어떻게**: O2와 같은 골격(classify_error·account 헤더·멱등키). 등록 즉시
  conditionalOrderId를 원장에 저장(포지션 키와 연결). 발동 시 `triggeredOrderId`를 일반
  주문 원장과 이어 붙임.
- **주의**: `modify`가 취소후재생성 방식이면 **갱신 중 보호 공백** → P1의 D-2 갱신과 함께
  "갱신 실패=P0·신규금지". confirm_high_value 항상 false.
- **테스트**: 생성→conditionalOrderId 저장, 목록 대사, 만료 갱신, 발동 orderId 연결, 미지 status.
- **의존/Stage**: O1·O2, CO 스펙 실측 / Stage 1.5→2. **P1보다 먼저.**

---

## C. 순서·게이트 수정 (00·02 오버라이드)

- **"파일02 전체 V2 전 착수 금지" → "live 주문 **호출 경로**만 V2 후."** O1(원장 확장)과
  VH(mock 장애주입 하네스)는 순수 코드라 **V2 전 선행**(V2 결과를 안전히 기록하려면 이게
  먼저 있어야 함).
- **I3(token_manager+rate limiter)를 O2보다 먼저.** 주문 코드부터 만들고 리미터를 나중에
  붙이면 호출 경로가 샌다.
- **확정 순서**:
  1) V1 셀프테스트 → 2) L1~L4 계좌읽기·대사 → 3) L6 파수꾼 하드닝(관찰) → 4) L5 가격주입
  (관찰) → 5) **O1 원장확장** → 6) **VH mock 1차** → 7) I3 token/rate → 8) **VR V2 실측** →
  9) O2·O3·O4 live 연결 → 10) **CO1** → 11) I1·I2·I4~I7 → 12) X1·X2·X4·P1·P2 →
  13) VH 전체 통과 → 14) Stage 2a(KR) → 15) Stage 2b(US).

---

## D. 태스크별 확정 델타 (원본 오버라이드)

**L3** — 실패 시 `None` 금지. **typed Result**(`ok/value/error_class∈{auth_retryable,
auth_fatal,rate_limited,not_found,server_error,parse_error,disabled}/request_id/
retry_after`) 반환. 이유: 403(IP차단)→None→대사가 "보유 없음" 오인→포지션을
closed_external로 정리하는 실사고. **계좌·주문 경로는 typed, 시세 경로는 기존 None→FDR 유지.**

**L4** — holdings뿐 아니라 **open orders + open conditional orders**까지 대사. 외부
포지션/주문/조건주문 발견 시(Stage 2) 해당 종목 자동매매 금지. 세금·수수료·settlementDate
기록은 L4가 아니라 **주문 체결 원장**(신규 태스크 B-ledger, §F) 책임으로 분리.

**L5** — Close만 덮지 말고 **OHLC 무결성**: `high=max(high,last)`, `low=min(low,last)`,
volume 유지/incomplete 표시, `bar_source="toss_overlay"`. stale면 표시엔 FDR, **신규 진입엔
금지**.

**L6** — quote stale = 단순 보류 금지. **폴백 체인**: fresh prices → fresh orderbook best
bid → (가능하면) fresh trades → 전부 stale이면 조건주문 있으면 P1/감시, 없으면 P0 "손절
판단 불능" + 신규 전면금지. best bid ≤ stop이면 위험 신호.

**O1** — body_hash는 **canonical JSON**(키정렬·decimal 정규화·None 제거 규칙 고정,
accountSeq/endpoint/method/side/symbol/quantity/orderType 포함). 미지 status =
`unknown_status_locked`(rejected 아님). CANCEL_REJECTED/REPLACE_REJECTED = 전이실패(원주문
복귀→재조회).

**O2** — `order_status(order_id 또는 client_order_id)` 폐기 → **분리**:
`order_status_by_order_id(id)` + `find_order_by_heuristic(symbol, side, qty, window)`
(clientOrderId 조회 불가하므로 heuristic). **409 전역 duplicate 금지** → `error.code`로 분기
(idempotency-conflict=P0/버그 vs already-processing=대사 vs already-filled=holdings 기준).
→ `classify_error`가 **code를 실제로 사용**하도록 확장(409·422에서 code별 분기).

**O3** — BUY **부분체결 시 FILLED까지 기다리지 말고 체결분 즉시 보호**(P1 등록/파수꾼 감시
시작). SELL 부분체결은 잔여만 추적 + 커버리지 재계산. 폴링은 Retry-After 준수.

**O4** — 부팅 대사에 **조건주문 스윕 포함**(open conditional orders 조회·원장 대사). 대사
완료 전 신규 주문 금지, 기존 브로커 서버측 조건주문은 유지, 파수꾼 신규 매도는 대사 후,
매칭 실패는 MANUAL_REVIEW_LOCK.

**X1** — "롤백" 개념 폐기(체결은 롤백 불가). **보호 상태기계**: `BUY_FILLED_UNPROTECTED →
PROTECTION_REGISTERING → PROTECTED | PROTECTION_FAILED`. FAILED면 신규중지+P0+파수꾼
감시+재등록, SLA 초과 시 사전선택 프로토콜(§E).

**X2** — actual_risk 초과 시 **자동 규칙**(수정채택): `≤1.15` 정상 / `1.15~1.30` 신규중지+
P1+축소후보 / `>1.30` P0. **단 첫 주 whole-share 1~2주는 "축소"가 물리적으로 불가·일시
오류로 손실실현 위험** → **auto-reduce는 큰 사이즈에서만 opt-in, 기본은 halt+P0+수동.**

**X3** — 실측 전 US 매수 금지·KR 우선. **단 유니버스 95%가 미국주라 KR 파일럿은 주문
기계만 검증하고 전략은 검증 못 함** → **Stage 2b(US whole-share)를 별도 hard 관문**으로
못박음(환전·세션·조건주문 실측 후).

**X4** — 조건주문 sellable 선점 여부(V2)로 정책 분기: 선점 안 하면 파수꾼 MARKET SELL 허용 /
선점하면 파수꾼은 조건주문 상태확인→취소확정 후 매도(취소 UNKNOWN이면 재주문 금지·P0).

**P1** — **CO1 선행**(§B). Stage 2 초기 OCO profit leg 미룸(SINGLE STOP만), 트레일은 파수꾼
내부(서버측은 최초 구조손절만).

**P2** — 커버리지 정의 강화: `coverage_qty(보호수량 ≥ 브로커 보유)` ∧ `coverage_stop(마지막
확정 stop 이상·하향금지)` ∧ `coverage_freshness(조건주문 OPEN·expireDate>D-2·파수꾼
heartbeat fresh·quote fresh)` ∧ `coverage_conflict(앱 충돌·고아 없음)`. "조건주문 존재"만으론
green 금지("10주 보유 5주만 등록"→red).

**I1** — Stage 2는 warnings 전부 하드차단(DELISTED·LIQUIDATION_TRADING·STOCK_WARRANTS·
정지·비정규장·기업행위불일치 + OVERHEATED·WARNING/RISK·VI). Stage 3+는 로그 남기고 재평가.

**I2** — 무료 VM 재시작→egress IP 변경→IP allowlist 차단→주문불능 시나리오. 원장 디스크는
**fsync/atomic write**, secret은 평문파일 금지(OS secret/권한제한). IP 고정 필요.

**I3** — 단일 프로세스(asyncio)면 현 설계 충분. **루프를 systemd 별 프로세스로 띄우면
파일락 또는 로컬 token service 필요**(프로세스 단일 매니저로 부족). → **단일 프로세스 권장.**

**I6** — Level 4는 **자동 주문만 중지, 기존 브로커 서버측 조건주문은 유지**(정지가 보호를
죽이면 안 됨). kill-switch는 **latch**(자동 하향 금지, operator ack 필요, 누가/언제/왜 원장 기록).

---

## E. 보호 실패 SLA (핵심 불변식 보강)

기존 "강제청산 아님"에 **"무방비를 방치하지 않는다"**를 추가:
```
보호 미등록 0~30초 : 등록 재시도
보호 미등록 30초+  : 신규진입 중지 + P0 + 파수꾼 긴급 보호모드
보호 미등록 3분+   : 다음 신규 금지 유지 + (사전선택 시) 축소/청산/수동개입 프로토콜
보호 실패 ∧ 파수꾼 비정상 : Stage 2에서 자동 축소/청산 옵션을 **사전 선택**해 둠
```
자동 즉시 강제청산은 기본값 아님(내 반박 유지). 단 무방비 장기화는 사전 정의 프로토콜로 해소.

---

## F. Q1 추가 태스크 (Codex 지적 채택)

- **B-ledger 실현손익/정산 원장**: `execution.commission/tax/settlementDate` + 환율 →
  원화 환산 실현손익, 전략 R과 분리 기록(세금·정산 어긋나면 Stage 3 판단 왜곡).
- **order-history 검색능력 실측**(V2 최우선): clientOrderId 없이 CLOSED history·holdings
  변화·조건주문 triggeredOrderId로 주문을 식별 가능한가. **불가면 Stage 2 No-Go.**
- 앱/API 동시주문 정책 = L4 확장(외부 open/conditional order·position → 종목 잠금).

## G. 태스크 수: 26 → 27 (CO1 신설) + B-ledger(F). 위험 1순위 = P1↔X4 sellable 충돌,
2순위 = UNKNOWN 10분창+부팅대사(clientOrderId 읽기불가로 **승격**), 3순위 = X1 매수후 보호공백.
