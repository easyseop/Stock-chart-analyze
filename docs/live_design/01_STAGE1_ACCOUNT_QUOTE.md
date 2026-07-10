# 파일 01 — Stage 1: 계좌 읽기 + 시세 정확도 (안전, 지금 착수 가능)

> 읽는 법: `##` 태스크 하나씩 끊어 읽고 판정 → 다음. 이 파일의 태스크는 **주문이
> 전혀 없어 리스크 0**. V1(셀프테스트)만 끝나면 착수 가능. L5는 매매결정 경로를
> 건드리므로 주의 표시.

---

## L1 — 계좌 헤더 인증 GET plumbing

- **무엇**: 기존 `bot/toss.py:_get(path, params)`에 `X-Tossinvest-Account: {accountSeq}`
  헤더를 선택적으로 붙이는 저수준 plumbing.
- **왜**: 계좌/자산 엔드포인트는 이 헤더가 필수. 시세 GET엔 불필요. 하나의 인증 GET
  경로(토큰 캐시·401 재발급·쿨다운)를 재사용하되 헤더만 확장해 중복 구현을 피한다.
- **어떻게**: `_get(path, params, *, account: str | None = None)` — account가 있으면
  헤더에 추가. 나머지 로직(토큰·401·타임아웃) 그대로. 계좌 **엔드포인트 함수는
  이 파일에 두지 않는다**(toss.py는 "시세 전용" 경계 유지) → L3는 별 파일.
- **주의**: `_get`은 조회 전용. 여기에 POST/주문을 절대 추가하지 말 것(경계 오염 금지).
- **테스트**: mock urlopen으로 account 지정 시 헤더 존재, 미지정 시 부재 확인.
- **의존/Stage**: 없음 / Stage 1.

## L2 — accounts() + accountSeq 결정 (자동선택 금지)

- **무엇**: `GET /accounts` → `[{accountNo, accountSeq, accountType}]`. 그중 **주문에
  쓸 accountSeq를 결정**하는 resolver.
- **왜**: 주문 헤더는 계좌번호가 아니라 **accountSeq**. 계좌가 여러 개면 `result[0]`
  자동선택은 **엉뚱한 계좌 오주문**을 부른다(GPT 지적).
- **어떻게**: `resolve_account_seq()` —
  1) env `LIVE_ACCOUNT_SEQ` 있으면 그 값 사용(+ accounts 목록에 존재하는지 검증),
  2) 없고 계좌가 정확히 1개면 그거,
  3) 그 외(0개 또는 2개+ & env 없음) → **None 반환 + "명시 필요" 로그**(진행 금지).
  시작 시 accountNo 끝 4자리·accountType을 로그(오계좌 조기 발견).
- **주의**: 실주문 단계에선 기대 accountSeq와 다르면 **프로세스 종료**(경보). BROKERAGE
  외 타입 거부.
- **테스트**: 계좌 0/1/2개 × env 유/무 → 각각 올바른 결정(2개+env없음=None).
- **의존/Stage**: L1 / Stage 1.

## L3 — holdings / buying-power / sellable-quantity 읽기

- **무엇**: 새 파일 `bot/toss_account.py` — 읽기 전용 계좌·자산 조회.
  `holdings(seq)`, `buying_power(seq)`, `sellable_quantity(seq, symbol)`.
- **왜**: 대사(브로커=진실)·매수여력·**매도가능수량(T+2)**의 기반. `holdings.quantity`
  (보유) ≠ `sellable-quantity`(매도가능)라 손절 전 반드시 후자 확인.
- **어떻게**: 각 함수가 `toss._get(path, params, account=seq)` 호출 → 실패 시 None(예외
  없음). `holdings`는 `HoldingsOverview{items[], overview}` 파싱. `sellable-quantity`는
  string→decimal(US 소수점). **주문·엔드포인트는 여기까지도 없음**(계좌 조회만).
- **주의**: 값은 문자열 decimal — float 변환 시 소수점 손실 주의(US). 조회 실패를 "매도
  금지"로 직결하지 말 것(파수꾼 규율은 L6에서). 키 미설정=전면 비활성(toss.enabled()).
- **테스트**: mock 응답 파싱 + 키 없을 때 None + 실패 시 예외 없음.
- **의존/Stage**: L1·L2 / Stage 1.

## L4 — 브로커 ↔ 내부 대사 (외부 포지션·주문 감지)

- **무엇**: 토스 실보유/미체결과 우리 원장·포지션을 **주기적·부팅 시 대조**하는 reconcile.
- **왜**: 앱 수동주문·기업행위·부분체결로 "우리 기록 ≠ 실계좌"가 되면 자동매매가 유령
  포지션에 손절을 내거나 이중주문. 실매매 최상위 안전장치("브로커가 진실").
- **어떻게**: `reconcile_account(seq, internal_positions)` →
  - 우리엔 있는데 브로커에 없음 → `closed_external`(우리 원장 정리),
  - 브로커엔 있는데 우리엔 없음 → `external_position`(**해당 종목 자동매매 금지 + P0/P1**),
  - 수량 불일치(액면분할·부분체결) → **신규 주문 금지 + 수동 리뷰 경보**.
  Stage 1(그림자)에선 **불일치를 로그·경보만** 하고 실제 정리는 안 함(관찰).
- **주의**: 대사는 rate limit 그룹(ASSET) 소비 → 주기 30~60초 캐시. 실매매 손익/세금
  기록은 여기서 `execution.tax/commission/settlementDate`를 원장에 남긴다(정산·감사용).
- **테스트**: 4가지 불일치 케이스 각각 올바른 분류.
- **의존/Stage**: L3 / Stage 1(관찰)→Stage 2(강제).

## L5 — 토스 실시간가를 매매 결정에 주입 (Stage 0.5) ⚠️ 매매경로 건드림

- **무엇**: 현재 "현재가 = 일봉 마지막 종가(FDR)"인 매매 결정을, `fast-lane`/`autopaper`
  에서 **토스 실시간가로 마지막 봉만 덮어** 결정하게.
- **왜**: 지금 모의매매는 15분~수분 지연가로 진입/손절 판단 → F5 정체·"화면가≠매매가"
  불일치의 근원. 마지막 봉만 실시간가로 바꾸면 **게이트·사이징 로직 무변경**으로 실전급
  가격에 결정.
- **어떻게**: `scanner/fastlane.run()`에서 `analyze` 직전, `frames["D"]`의 마지막 행
  Close(필요 시 High/Low)를 `toss.price(sym)`의 fresh 값으로 치환. ts 나이 초과·미제공이면
  FDR 유지 + `is_stale_for_trading` 태깅. **`toss.enabled()` 없으면 완전 no-op**(현행 동일).
- **주의**: 매매 결정을 바꾸는 유일한 태스크 → 반드시 키게이트 뒤. 지표는 과거 봉으로
  이미 계산되므로 **마지막 봉 Close만** 신선화(과거 봉 조작 금지). 장중에만(장외엔 일봉 확정).
- **테스트**: fresh 토스가 → 마지막 봉 가격 변경 & 결정 반영 / stale → 무시 / 키없음 → no-op.
- **의존/Stage**: L3(가격), 기존 `bot/toss.price` / Stage 0.5(1과 병행 가능).

## L6 — 파수꾼 하드닝: orderbook·sellable 단계화·quote-age

- **무엇**: `bot/sentinel.py` 손절 판단에 (a) orderbook 호가공백 방어, (b) sellable-quantity
  단계적 조회, (c) quote 나이 가드를 추가.
- **왜**: `lastPrice`만 보면 **best bid ≤ stop인데 미이탈로 오판**(호가공백)·낡은 시세로
  손절 오발/지연. sellable을 매 폴링 조회하면 ORDER_INFO 버킷 소진 위험(GPT).
- **어떻게**:
  - quote-age: `toss.price()`의 `ts` 나이 > MAX_QUOTE_AGE_SEC(기본 30)면 손절 **판단 보류**
    (표시엔 FDR 폴백 가능, 발화엔 안 씀). `ts=null`이면 orderbook 시각으로 대체.
  - orderbook: best bid ≤ stop = 위험 신호. spread > 1~2%면 시장가 대신 보호 로직 전환.
  - sellable 단계화: 평상시 조회 안 함 → 현재가 ≤ stop+0.3ATR(근접) 시 갱신 → 이탈 시
    1회 즉시 조회(실패 시 Retry-After 1회, 그래도 실패면 조건주문에 맡기고 P0).
- **주의**: 이 태스크는 **판단만** 강화(실제 주문은 X4). stop 래칫(하향금지)은 유지.
  sellable 조회는 Stage 1에선 "관찰"(dry-run 발화 로그만).
- **테스트**: stale ts → 미발화 / bid≤stop → 위험표시 / sellable 근접·이탈 조회 타이밍.
- **의존/Stage**: L3, 기존 파수꾼 / Stage 1(관찰)→Stage 2(집행).
