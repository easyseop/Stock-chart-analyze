# 토스증권 Open API — 실매매 전 반드시 알아야/대비해야 할 것 (정독 정리)

> 2026-07-09. **OpenAPI 3.1 스펙(openapi.json) 전수 파싱 + 개발자 문서** 정독 결과를
> 실운영 관점으로 정리. 목적: 실주문 붙이기 전에 "이건 몰라서 사고난다"를 없앤다.
>
> **표기**: [확정]=스펙/문서에서 직접 확인 · [대조필요]=실제 응답·CS·약관으로
> 최종 확인 · ✅=우리 시스템에 이미 반영 · ⬜=미구현/실측 대기.
>
> 출처는 문서 맨 끝. 스펙의 모든 열거형은 "클라이언트는 unknown 값 허용" 설계이므로
> **모든 enum·code는 미지의 값을 안전 기본값으로 처리**해야 한다(스펙 명시).

---

## 1. 인증·토큰 [확정]

| 항목 | 내용 | 대응 |
|---|---|---|
| 발급 | `POST /oauth2/token` (form: grant_type=client_credentials, client_id, client_secret) | ✅ `bot/toss.py` |
| 만료 | `expires_in`(초)이 진실. 예시 86400=24h | ✅ 선제 갱신(만료 5분 전) |
| **단일 토큰** | client당 유효 토큰 **1개** — 재발급 시 이전 토큰 **즉시 무효화** | ⬜ 서버 상시운용 시 **단일 token_manager 필수**(루프별 개별 발급 금지) |
| 토큰 에러 | 400=요청오류 / **401=자격증명 오류(client_id/secret 틀림·비활성)** | 401은 재시도로 안 풀림 → **P0**(Stage1+). Stage0은 조용히 FDR 폴백 |
| OAuth 에러형 | `{error: invalid_client|invalid_grant|invalid_request|unsupported_grant_type, error_description}` (표준 OAuth) | code로 분기(§12) |
| 계좌 헤더 | 계좌/주문 호출에 `X-Tossinvest-Account` = **accountSeq**(계좌번호 아님!) | ⬜ `GET /accounts`로 accountSeq 먼저 취득 |

## 2. Rate Limit — **그룹별 독립 버킷** (설계 대전제 갱신) [확정 구조 / 수치 대조필요]

**중대 발견**: 버킷이 하나가 아니라 엔드포인트마다 **그룹**이 붙어 있고 서로 독립적이다.
우리 플랜의 "계좌 초당 1건 단일 버킷" 최악 가정은 **과도하게 보수적**이었다.

| 그룹 | 포함 엔드포인트 |
|---|---|
| `AUTH` | 토큰 발급 |
| `MARKET_DATA` | prices · orderbook · trades · price-limits |
| `MARKET_DATA_CHART` | candles |
| `STOCK` | stocks · warnings |
| `MARKET_INFO` | exchange-rate · market-calendar |
| `ACCOUNT` | accounts |
| `ASSET` | holdings |
| `ORDER` | 주문 생성·정정·취소 |
| `ORDER_HISTORY` | 주문 목록·상세 |
| `ORDER_INFO` | buying-power · sellable-quantity · commissions |
| `CONDITIONAL_ORDER` / `_HISTORY` | 조건주문 생성·수정 / 조회 |

→ **의미**: 파수꾼이 `sellable-quantity`(ORDER_INFO)를 조회해도 `ORDER`(주문)·`ASSET`
(보유) 버킷과 **경쟁하지 않는다**. "손절 직전 조회가 주문과 같은 1초에 몰려 깨진다"는
우려가 크게 완화. 단 **그룹별 실제 수치(초당/분당)는 스펙에 없음** → Stage 0/1에서
`429` 응답 헤더(있다면 `Retry-After`/`X-RateLimit-*`)로 실측. 여전히 **버스트 회피 +
지수 백오프**는 유지(수치 미확정이므로).

## 3. 시세 조회 [확정]

| 엔드포인트 | 핵심 |
|---|---|
| `GET /prices?symbols=A,B` (≤200) | `{symbol, timestamp(ISO8601\|null), lastPrice(str), currency}`. **timestamp=null=체결 미발생** → 나이 판정 불가 시 손절 판단 금지 |
| `GET /orderbook?symbol=` | `asks/bids`(가격순). **호가 공백 방어** — 시장가 손절 슬리피지 판단용 |
| `GET /price-limits?symbol=` | 상/하한가. **US는 null**(가격제한 없음) — 상한가 근처 주문 거부 방지(국내) |

✅ prices는 어댑터 구현·연동. ⬜ orderbook/price-limits는 실주문 단계에서 사용.

## 4. 계좌·자산 [확정]

| 엔드포인트 | 핵심 | 대응 |
|---|---|---|
| `GET /accounts` | accountNo, **accountSeq**(주문 헤더에 쓰는 키), accountType(현재 BROKERAGE만) | 시작 시 1회 취득·캐시 |
| `GET /holdings` | 종목별 quantity·averagePurchasePrice·lastPrice·marketValue·profitLoss·cost | 브로커가 진실(source of truth) |
| `GET /buying-power` | `cashBuyingPower`(**미수 미발생 기준 현금 매수가능액**) | 매수 전 확인 — 주문가능금액 ≠ 예수금 |
| `GET /sellable-quantity` | **판매가능수량**(KR 정수 / US 소수점 가능) | **매도 직전 필수** — T+2·미결제로 보유≠매도가능 |

→ **핵심 함정**: `holdings.quantity`(보유)와 `sellable-quantity`(매도가능)는 **다르다**
(방금 매수분은 결제 전이라 못 팜). 손절 매도 전 반드시 sellable-quantity로 상한 확인.

## 5. 주문 생성 `POST /orders` [확정]

**요청(OrderCreateRequest)** — 수량기반 / 금액기반(oneOf):
| 필드 | 값 | 주의 |
|---|---|---|
| `symbol` | KRX 6자리 / US 티커 | |
| `side` | `BUY` / `SELL` | 파수꾼은 SELL만 |
| `orderType` | `LIMIT` / `MARKET` | MARKET은 price 생략 |
| `quantity` | 문자열 decimal | **소수점 수량은 US 시장가 매도만 허용**. 그 외 소수점=`400 invalid-request`. 소수점 매수는 `orderAmount`(금액기반) |
| `timeInForce` | `DAY`(기본) / `CLS`(종가, **US LIMIT만**=LOC) | 미전달 시 DAY(정규장 종료 시 미체결 자동취소) |
| **`clientOrderId`** | ≤36자 `[a-zA-Z0-9-_]` | **멱등키! 10분 유효** — 동일 값 재요청 시 **이전 주문 결과 그대로 반환**(중복 생성 방지). 10분 후 동일값=새 주문 |
| `confirmHighValueOrder` | boolean | **1억원 이상 주문은 동의 필요** — 시드 1억이라 종목당 1/3(≈33M)이면 단건은 대개 미만이나, 확인 필요 |

→ **멱등 전략 확정**: `clientOrderId`에 우리 주문키(`{account}:{symbol}:{opened}:{seq}`)를
넣으면 **timeout 후 안전 재시도**가 가능(중복이면 이전 결과 반환). 단 **10분 창** 주의 —
10분 지나 재요청하면 새 주문이 되므로, 10분 넘긴 UNKNOWN은 반드시 **주문조회로 대사** 후 처리. ✅ 원장 UNKNOWN 잠금·대사 이미 구현, ⬜ clientOrderId 연결은 실주문 시.

## 6. 주문 상태기계 `OrderStatus` [확정] → 우리 원장 매핑

`PENDING · PENDING_CANCEL · PENDING_REPLACE · PARTIAL_FILLED · FILLED · CANCELED ·
REJECTED · CANCEL_REJECTED · REPLACE_REJECTED · REPLACED`

| 토스 status | 우리 ledger | 대응 |
|---|---|---|
| PENDING/PENDING_* | submitted | 대기 — 조회 지속 |
| PARTIAL_FILLED | partial | 잔여만 재주문 대상 |
| FILLED | filled | 종료 |
| REJECTED/CANCEL_REJECTED/REPLACE_REJECTED | rejected | **손절 거부면 P0**(보호 실패!) |
| CANCELED/REPLACED | (해당 없음/재평가) | 정정·취소 결과 반영 |
| **타임아웃·5xx·응답없음** | **unknown** | **종목 잠금 → 주문조회 대사 → 잔여만**(초과매도 방지) |

응답 `execution{filledQuantity, averageFilledPrice, filledAmount, commission, tax,
filledAt, settlementDate}`. **부분체결 시 execution으로 실체결 확인** → 잔여 계산.

## 7. 서버측 조건주문 = 0차 방어(B0) `POST /conditional-orders` [확정]

전부-죽어도 남는 최후 손절선. **지원 확인됨**.
| 필드 | 값 |
|---|---|
| `type` | `SINGLE`(단일) / **`OCO`**(손절+익절 동시, 하나 체결 시 나머지 취소) / `OTO`(부모→자식) |
| 조건(`ConditionRequest`) | `orderSide`(BUY/SELL) · **`triggerPrice`**(현재가 도달 시 발동) · `orderPrice`(LIMIT이면 지정가, MARKET이면 생략) |
| 조건 세부(`ConditionalOrderCondition`) | `type`: **`STOP`**(가격 트리거) / `PROFIT_RATE`(목표수익률 %) |
| `orderType` | LIMIT / MARKET (그룹 공통) |
| **`expireDate`** | **YYYY-MM-DD 필수** — 이날까지 미충족 시 자동만료 |
| `clientOrderId` | 멱등키(주문과 동일) |
| leg 상태 | WATCHING·HOLDING·PAUSED·ORDERING·ORDERED·COMPLETED·EXPIRED·CANCELED |

→ **전략 매핑**: 매수 직후 **OCO 조건주문(STOP=손절가 + PROFIT_RATE/지정가=목표가)**을
등록하면 손익비 1:2가 브로커 서버측에 박힌다 → 파수꾼·GitHub·CF 전부 죽어도 손절 집행.
⬜ 단 **stop 트리거가 시장가인지 지정가인지·미국주 지원·트리거 정확도**는 실측 필요
([대조필요]). expireDate 자동만료 → **스윙 보유기간 넘어가면 갱신** 로직 필요.

## 8. 종목 상태·유의사항 — 진입/매매 회피 [확정]

| API | 값 | 대응 |
|---|---|---|
| `StockInfo.status` | SCHEDULED / ACTIVE / **DELISTED** | 상장폐지·예정 종목 **신규 진입 금지** |
| `StockInfo` | market(KOSPI/KOSDAQ/NYSE/NASDAQ/AMEX...), securityType(STOCK/ETF/ETN/REIT/DR/**STOCK_WARRANTS**), isCommonShare, delistDate, sharesOutstanding | 우선주·ETN·워런트 취급 정책 |
| `GET /stocks/{sym}/warnings` | **LIQUIDATION_TRADING**(정리매매)·OVERHEATED(과열)·INVESTMENT_WARNING/RISK(투자경고/위험)·**VI**(변동성완화)·STOCK_WARRANTS | 유의종목 **신규 진입 금지 + 보유 시 경보** |

→ ⬜ 진입 게이트에 "warnings 없음 ∧ status=ACTIVE" 추가(실주문 단계). 티커변경·거래정지·
상장폐지 시 심볼 매핑 불일치 → **수량 불일치면 신규 주문 금지 + 수동 리뷰**(플랜 §7.5).

## 9. 시장 캘린더·세션 [확정]

| API | 핵심 |
|---|---|
| `GET /market-calendar/US` | today/previous/next 영업일 + 세션(preMarket·**dayMarket(주간거래)**·regularMarket·afterMarket). 휴장이면 각 null |
| `GET /market-calendar/KR` | 국내 영업일·세션 |
| US 정규장 | 예: 22:30~05:00 KST(서머타임 반영은 캘린더가 함) |

→ **대응**: 우리 `market_open` 하드코딩 대신 **캘린더 API로 휴장·조기폐장 정확 반영**(⬜
실주문 단계). **dayMarket(주간거래)·pre/after 세션에서 주문 처리 방식** 실측 필요([대조필요]).

## 10. 수수료·세금·결제 [확정]

| API/필드 | 내용 |
|---|---|
| `GET /commissions` | 국가별 수수료율(%). 예 0.015%. **해외주식은 startDate=null(무기한 적용)** |
| `execution.commission` / `.tax` | 체결별 실수수료·세금(native currency) |
| `execution.settlementDate` | **결제 예정일(T+2)** — 이 날 전엔 매도대금 재사용·매도가능수량 제약 |
| `Cost.tax` | 세금 없으면 null |

→ **대응**: 손익은 **수수료·세금 반영 후**로 계산(execution에서). 미국주 **양도소득세·환전
스프레드**는 API 밖 세무/환전 정책 → [대조필요, 토스 CS/약관]. sellable-quantity가
T+2를 이미 반영하므로 **매도가능수량을 신뢰**하되 buying-power로 매수여력 별도 확인.

## 11. 환전·외화 [확정 일부]

`GET /exchange-rate` → `rate`(매수환율) · `midRate`(매매기준율) · `basisPoint` ·
**`validFrom`/`validUntil`(환율 유효구간, 예시 1분)**.
→ US 매수 시 **원화 1% 리스크 ↔ 달러 체결금액** 환산에 이 rate 사용. **환율 1분 만료** →
주문 직전 재조회. ⬜ **자동환전 여부·시점·환전 실패 시 주문거부 형태**는 [대조필요].

## 12. 에러 대응 표 (HTTP status × 맥락) [확정 구조]

에러 응답: `{error: {requestId(=X-Request-Id 헤더), code(flat string), message, data}}`.
`data`엔 해결힌트(예: `field`=검증실패 필드). **code 미지값 허용 필수.**
1차 분기는 **HTTP status**, 2차는 알려진 code. **같은 status가 조회/주문에서 다른 의미**:

| status | 조회(GET) | 주문(POST) | 근거(스펙 설명) |
|---|---|---|---|
| 400 | reject(요청수정) | reject | 잘못된 요청(필수 누락·소수점 규칙 위반 등) |
| 401 | 토큰 재발급 1회 | 토큰 재발급→실패면 P0 | 인증 실패 |
| 403 | auth_fatal(P0) | auth_fatal(P0) | 권한 없음 — 재시도 무의미 |
| 404 | not_found | not_found | 종목/주문/계좌 없음 |
| 409 | reject | **duplicate=이미 접수**(멱등 충돌 → 재전송 말고 대사) | "중복 요청" |
| 422 | — | **reject(비즈니스 규칙 위반)** — 재시도 무의미 | 잔고부족·정지종목 등 |
| 429 | 백오프 재시도 | 백오프 재시도 | rate limit |
| 5xx/타임아웃 | 재시도/폴백 | **UNKNOWN → 종목 잠금·대사** | "주문 처리 중 일시 오류 또는 시스템 점검" |

✅ 이 표는 `bot/toss.py: classify_error()`로 코드화·테스트됨. 주문 경로는 **불확실하면
REJECT가 아니라 UNKNOWN**으로 분류(초과매도 방지). ⬜ 실주문 어댑터가 이 함수로 분기.

## 13. 우리 시스템 반영 현황

| 대비 항목 | 상태 |
|---|---|
| 주문 원장·UNKNOWN 잠금·대사·잔여만 재주문 | ✅ `bot/ledger.py` + `sentinel` |
| 멱등키(포지션 정체성) | ✅ 내부 / ⬜ `clientOrderId` 연결(10분 창 주의) |
| 에러 분류 | ✅ `classify_error()` |
| 긴급 정지(kill switch) | ✅ `KILL_NEW_ENTRIES` |
| P0 알림 이중화(ntfy) | ✅ |
| 시세 어댑터(읽기) | ✅ prices / ⬜ orderbook·price-limits |
| accountSeq 헤더·주문/계좌 API | ⬜ 실주문 단계 |
| 조건주문(OCO 서버측 stop) | ⬜ 실측 후 매수직후 등록 |
| 종목 status/warnings 진입 게이트 | ⬜ |
| 캘린더 기반 휴장·세션 | ⬜ |
| sellable-quantity 매도 상한 | ⬜ |
| 수수료·세금 반영 손익 | ⬜ |

## 14. 실주문(Stage 2) 전 반드시 실측/확정할 것

1. `429` 응답의 실제 헤더(`Retry-After`/한도 수치) — 그룹별 버킷 크기.
2. 조건주문 STOP: **시장가/지정가 트리거·미국주 지원·트리거-체결 지연**([대조필요]).
3. `clientOrderId` 10분 창 실동작 — timeout 재시도 시 중복 반환 확인.
4. 부분체결→정정/취소 상태 전이(`PARTIAL_FILLED`→cancel 시 잔여 처리).
5. US 세션별(pre/day/after) 주문 접수·체결 규칙.
6. 자동환전 여부·시점·실패 형태(외화 잔고 부족 시 주문거부 코드).
7. API 점검(maintenance) 시간대 — 미국장 중 점검 가능성.
8. 앱 수동주문 ↔ API 주문 동시 존재 시 source of truth(holdings 재조회로 대사).
9. `confirmHighValueOrder` 트리거 금액·동작.
10. 상장폐지·티커변경·액면분할 시 심볼 매핑·수량 불일치 처리.

## 15. 출처

- **OpenAPI 3.1 스펙(권위 원본, 본 문서 [확정]의 근거)**:
  `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` (2026-07-09 전수 파싱)
- **API 레퍼런스(마크다운)**: `https://openapi.tossinvest.com/openapi-docs/latest/api-reference/README.md`
- **개발자 문서**: `https://developers.tossinvest.com/docs`
- **Open API 소개·약관**: `https://corp.tossinvest.com/ko/open-api` · `https://corp.tossinvest.com/ko/terms`
- 관련 내부 문서: `docs/TOSS_API_PLAN.md`(설계) · `docs/RELIABILITY_PLAN.md` ·
  `bot/toss.py`(어댑터·classify_error) · `bot/ledger.py`(원장) · `bot/sentinel.py`.
