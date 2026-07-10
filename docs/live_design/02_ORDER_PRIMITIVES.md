# 파일 02 — Stage 1.5: 주문 primitive (실측 게이트)

> 읽는 법: `##` 하나씩 끊어 판정. **이 파일 전체가 V2 실측(그림자 4종 확인) 통과
> 전엔 착수 금지** — 추정으로 주문 코드를 만들지 않는다는 불변식. 여기서 "만든다"의
> 상당수는 기존 골격(`ledger`·`classify_error`·`client_order_id`) **확장/연결**이다.

---

## O1 — 원장 확장: clientOrderId · body_hash · 10상태

- **무엇**: `bot/ledger.py`를 확장 — 주문키당 `clientOrderId`, `request_body_hash`,
  `first_submitted_at`, `idempotency_expires_at`(=first+9분30초)를 저장하고, 토스
  `OrderStatus` 10종을 원장 상태로 정확히 매핑.
- **왜**: (a) 내부키(콜론·36자 초과)는 토스 clientOrderId(≤36)에 못 넣어 해시 매핑 필요
  — 그 매핑을 원장이 보관해야 timeout 재시도·대사가 가능. (b) 단순 5상태로는 실주문의
  전이실패(`CANCEL_REJECTED` 등)를 오분류해 **이중주문**.
- **어떻게**:
  - `record_submit`에 `client_order_id`(=`toss.client_order_id(internal_key)`), `body_hash`
    (주문 body의 sha256), `first_submitted_at` 인자 추가·저장.
  - 상태 집합 확장: `submitted·pending_cancel·pending_replace·partial·filled·canceled·
    rejected·cancel_rejected·replace_rejected·replaced·unknown`.
  - `map_status(toss_status, filled_qty)` — 파일 05 검토표대로 매핑. **cancel_rejected/
    replace_rejected = 전이실패(원주문 복귀 → 재조회)**, canceled/rejected도 filledQty>0
    확인, pending_cancel/replace = 신규주문 금지.
  - `same_body(key, new_body_hash)` — 같은 clientOrderId에 다른 body면 **P0/버그**.
- **주의**: append-only 유지(상태는 이벤트로만). 10분 창은 `idempotency_expires_at`로
  판단하되 **네트워크·시계 오차 감안 9분30초**. 기존 `is_locked/reconcile/residual/
  filled_for`는 그대로 재사용(재작성 금지).
- **테스트**: 각 전이 매핑, body 불일치 감지, 9분30초 경계, 해시 매핑 왕복.
- **의존/Stage**: 기존 `ledger`·`toss.client_order_id` / Stage 1.5.

## O2 — toss_orders: 주문 생성·조회·취소

- **무엇**: 새 파일 `bot/toss_orders.py` — `place_order(...)`(POST /orders),
  `order_status(order_id 또는 client_order_id)`(GET /orders/{id}, ORDER_HISTORY),
  `cancel_order(order_id)`(POST /orders/{id}/cancel).
- **왜**: 실주문 집행·조회·취소의 최소 단위. **모든 UNKNOWN 대사의 출발점**이 order_status.
- **어떻게**:
  - `place_order(seq, symbol, side, qty, order_type, price=None, tif="DAY",
    client_order_id=..., confirm_high_value=False)` → body 구성, `X-Tossinvest-Account`.
    응답/예외를 **`toss.classify_error(status, code, is_order=True)`로 분기**:
    ok→filled/기록, duplicate(409)→이전 결과로 간주·대사, reject(422/400)→종료(손절이면 P0),
    unknown(5xx/타임아웃)→**원장 unknown·종목잠금**, refresh(401)→토큰갱신 1회.
  - `confirm_high_value`는 **항상 False**(1억+면 거부되게 — 사이징 버그 방어). 인자로 받되
    True 경로는 만들지 않음.
  - **매수 경로가 파수꾼(sentinel)엔 안 들어가게** — toss_orders는 buy/sell 다 되지만
    sentinel은 sell만 호출(grep 테스트로 영구 검증).
- **주의**: `client_order_id` 재사용은 **9분30초 이내 + 동일 body**에서만(O1과 연동). 10분
  초과 재POST 금지. 소수점 수량은 US 시장가 매도만(그 외 400) — 규칙 위반 사전 차단.
- **테스트**: 각 status→액션, 멱등 재요청(같은 body→중복 반환), confirm_high_value 강제 false,
  reject 손절→P0.
- **의존/Stage**: O1, `classify_error`, L1 / Stage 1.5(수동 1건)→2.

## O3 — 체결 폴링 루프 (place → settle)

- **무엇**: 주문 전송 후 **종결 상태(FILLED/REJECTED/CANCELED)까지 order_status 폴링**하며
  원장을 전이시키는 루프(loop B의 후속).
- **왜**: 주문은 비동기 — 접수(PENDING)와 체결 사이가 있다. 부분체결·정정·취소 경계를
  실시간 반영해야 잔여 계산·보호주문 수량이 맞는다.
- **어떻게**: `settle(order_id, client_order_id, deadline)` — 짧은 간격(1~3초, ORDER_HISTORY
  버킷) 폴링, `map_status`로 원장 갱신, PARTIAL_FILLED면 execution.filledQuantity 반영.
  deadline 초과·연속 실패면 **UNKNOWN 처리(종목 잠금 → O4 대사에 위임)**. DAY 주문은 장
  종료 시 자동취소 → 그 상태도 폴링으로 흡수.
- **주의**: 폴링을 무한 재시도하지 말 것(버킷·행). timeout은 실패가 아니라 UNKNOWN.
  이 루프가 죽어도 **부팅 대사(O4)가 잔여를 잡게** 설계(단일 실패점 금지).
- **테스트**: pending→partial→filled 전이, 부분체결 잔여, deadline→unknown.
- **의존/Stage**: O1·O2 / Stage 1.5→2.

## O4 — 부팅/크래시 대사 (open orders + holdings 스윕)

- **무엇**: 서버·루프 **시작 시 어떤 매매도 하기 전에**, 미체결 주문 목록 + 보유를 훑어
  원장과 대사하고 UNKNOWN/유령을 해소하는 부트스트랩.
- **왜**: 크래시·재시작 후 원장이 최신이 아닐 수 있다. 대사 없이 매매를 재개하면
  **이미 나간 주문을 또 내거나(초과매도)** 유령 포지션을 손절한다. UNKNOWN 잠금(원장)이
  있어도 **부팅 대사가 그걸 실제로 풀어주는 유일한 경로**.
- **어떻게**: `bootstrap_reconcile(seq)` —
  1) `GET /orders`(open) + 당일 order history 조회,
  2) 원장의 open/unknown 주문을 clientOrderId·symbol·기간으로 매칭 → 실제 상태로 확정,
  3) 매칭 실패한 원장 UNKNOWN → 해당 종목 **MANUAL_REVIEW_LOCK**(자동 해제 금지),
  4) holdings와 포지션 대사(L4 호출) → 불일치면 신규 금지.
  이 과정 **완료 전엔 루프A/B가 주문을 내지 않는다**(게이트).
- **주의**: 이 태스크가 없으면 O1~O3의 UNKNOWN 잠금이 **영구 잠금**이 된다(누가 풀지?).
  V2 실측 항목 "orderId 없이 대사 가능한가"가 여기서 검증됨 — 불가면 Stage 2 **No-Go**.
- **테스트**: 부팅 시 open 주문 매칭, 미매칭 UNKNOWN→수동잠금, holdings 불일치→신규금지.
- **의존/Stage**: O1·O2·L3·L4 / Stage 1.5(실측)→2(필수).
