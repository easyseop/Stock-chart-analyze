# 토스증권 Open API 실매매 준비도 검토 요청 (GPT용)

> **사용법**: 이 프롬프트와 함께 `docs/TOSS_API_READINESS.md`(전체)를 붙여넣어라.
> 필요하면 GPT가 아래 출처 URL을 직접 조회하도록 허용하라.

## 역할
너는 국내 증권 Open API로 **실계좌 자동매매**를 붙여본 시니어 트레이딩 인프라
엔지니어다. 아래 우리 정리(`TOSS_API_READINESS.md`)를 **비판적으로 검증**하라.
"원칙론"이 아니라 **"이 조합에서 이렇게 깨진다"는 구체적 실패 시나리오**가 유용하다.

## 배경 (이것만 알면 됨)
- 차트 기반 자동매매. 전략=52주 저점권 하락→상승 전환 초입 매수, ATR 손절, 손익비
  1:2, 일봉 스윙(초단타 아님). 유니버스 ~5,400(95% 미국주). 시드 1억(가상, 종목당 1/3 캡).
- 인프라: GitHub Actions 무료 배치 + Cloudflare Worker(발사·검증) + 매도전담 파수꾼
  (dry-run 완성). **주문 원장(UNKNOWN 잠금·대사·잔여만 재주문)·에러 분류기·kill switch·
  P0 ntfy 이중화**는 이미 구현. 현재 **시세는 읽기 전용(Stage 0)**, 주문은 가상.
- 지금 토스 계좌+앱키 발급 완료. 실매매로 넘어가기 전 준비도 점검 중.

## 우리가 스펙에서 확정했다고 보는 핵심 (검증 대상)
1. **Rate limit이 그룹별 독립 버킷**(AUTH/MARKET_DATA/STOCK/ACCOUNT/ASSET/ORDER/
   ORDER_HISTORY/ORDER_INFO/CONDITIONAL_ORDER 등)이라, 파수꾼의 sellable-quantity
   조회(ORDER_INFO)가 주문(ORDER)·보유(ASSET) 버킷과 경쟁하지 않는다.
2. **`clientOrderId`가 10분 유효 멱등키** — 동일 값 재요청 시 이전 주문 결과 재반환.
   → timeout 후 **10분 내**엔 같은 clientOrderId로 안전 재시도, **10분 초과 UNKNOWN은
   반드시 주문조회로 대사** 후 처리.
3. **주문 UNKNOWN(5xx·타임아웃)은 REJECT가 아니라 UNKNOWN**으로 분류 → 종목 잠금·
   대사·잔여만 재주문(초과매도 방지). 409=중복접수(멱등), 422=비즈니스 거부(재시도 무의미).
4. **서버측 조건주문(OCO STOP+PROFIT_RATE)**을 매수 직후 등록해 0차 손절 방어. expireDate
   자동만료라 보유기간 넘기면 갱신 필요.
5. **holdings.quantity ≠ sellable-quantity**(T+2) → 매도 직전 sellable-quantity 확인.
6. `X-Tossinvest-Account`에 **accountSeq**(계좌번호 아님) 사용.

## 검토 요청 (각 질문에 구체 실패 시나리오와 함께)
- **Q1.** 위 1~6 중 **사실 오류·과신·위험한 단순화**가 있나? 특히 rate limit 그룹 독립성을
  믿고 파수꾼이 매 폴링 sellable-quantity를 조회하면 실제로 안전한가?
- **Q2.** `clientOrderId` **10분 창** 설계에 구멍은? (예: 대사 지연으로 10분을 넘겨 같은
  키로 재요청 → 새 주문 생성 → 초과매도. 우리가 이걸 제대로 막고 있나?)
- **Q3.** 조건주문(OCO STOP)을 0차 방어로 쓸 때 함정 — 트리거가 시장가면 갭·호가공백
  슬리피지, expireDate 만료, 앱/API 이중 존재, 부분체결 후 잔여 조건 처리 등.
- **Q4.** `TOSS_API_READINESS.md` §14(실측 목록)에서 **빠진 항목**은? 실계좌 운영에서
  우리가 아직 모르는 함정(정산·미수·증거금·기업행위·점검시간·세금·환전)을 지적하라.
- **Q5.** 반대로 **모의/소액 단계엔 과한 것**(지금 만들 필요 없는 것)은?
- **Q6.** 우리 상태기계(토스 OrderStatus 10종 → 원장 submitted/partial/filled/rejected/
  unknown 매핑)에 누락·오매핑이 있나? PENDING_CANCEL·REPLACE_REJECTED 등 경계 상태 처리.

## 출처 (직접 조회 가능)
- OpenAPI 3.1 스펙(권위 원본): `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`
- API 레퍼런스(md): `https://openapi.tossinvest.com/openapi-docs/latest/api-reference/README.md`
- 개발자 문서: `https://developers.tossinvest.com/docs`
- 소개·약관: `https://corp.tossinvest.com/ko/open-api` · `https://corp.tossinvest.com/ko/terms`

— 구체적 실패 시나리오와 "이 순서로 이렇게 깨진다"로 답해주면 바로 반영한다.
