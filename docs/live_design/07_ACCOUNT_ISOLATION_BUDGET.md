# 파일 07 — 계좌 격리 + 시드 봉투 (다중에이전트 분석 반영)

> 사용자 질문("봇이 기존 종목 건드리면? 공유 계좌에서 봇 시드를 어떻게 제한?")을
> 10-에이전트 워크플로우(설계·적대적·검증)로 분석한 확정 설계. 스펙·코드 교차검증됨.
> 읽는 법: `##` 하나씩. 이 파일은 X1/X2(실행·사이징)와 L4(대사)를 오버라이드한다.

---

## 0. 스펙 사실 (확정) — 왜 격리가 어려운가

- **단일 계좌에서 봇/사용자 포지션을 API로 구분 불가.** `HoldingsItem`·`sellable-quantity`는
  **종목 단위 합산**(lot·소유자·매입일 필드 없음). 같은 종목은 브로커가 **한 라인으로 병합**
  (가중평균 평단). `clientOrderId`는 **write-only**(조회 응답에 없음) → 지속 태그 불가.
- **물리적 격리는 별도 accountSeq로만.** `GET /accounts`는 List 반환·`X-Tossinvest-Account`로
  대상 지정 → accountSeq가 다르면 holdings/sellable/buying-power/주문이 **완전 분리**됨.
- **[실측 필요]** (Stage 2 전 VA에 추가): ① 토스에서 **봇 전용 BROKERAGE 2번째 계좌 개설
  가능**한가? ② **API 키 스코프** — `GET /accounts`가 사용자의 모든 계좌를 주나, 특정 계좌만?
  (봇 키가 사용자 손실 계좌까지 주문 가능하면 격리 무의미 → 위험.) 토스 CS/앱 실측.

---

## IS1 — 격리 방식 선택 (최선: 별도 계좌)

- **무엇**: 봇 자본을 사용자 자본에서 분리하는 최상위 결정.
- **왜**: §0 때문에 공유 계좌 격리는 소프트웨어 논리일 뿐(경쟁조건 취약). 별도 계좌면
  브로커가 강제하는 물리 격벽.
- **어떻게**:
  - **A. 별도 계좌(강력 권장)**: 봇 전용 BROKERAGE 계좌를 열고 시드만 입금 → `LIVE_ACCOUNT_SEQ`로
    지정. 기존 손실 미국주와 현금·포지션·손절 완전 분리. 같은 종목 충돌·sellable 오염·시드
    오염이 **구조적으로 불가능**. → **[실측]으로 개설·스코프 확인되면 이걸 택한다.**
  - **B. 공유 계좌(개설 불가 시)**: 아래 IS2~IS4의 논리 격리. 되지만 잔여 경쟁조건 위험.
- **주의**: A가 되면 이 파일의 IS2~IS4 복잡도 대부분이 불필요해진다(그래서 A를 먼저 실측).
- **의존/Stage**: VA 실측 / Stage 1.5 전 결정.

## IS2 — 심볼 비중첩 (공유 계좌의 1차·유일한 진짜 보장)

- **무엇**: 봇은 **사용자가 이미 보유한 종목을 영구 매수 금지**.
- **왜**: 같은 종목 매수 = 병합 = 소유 경계 소실(적대적 H6, CONFIRMED). 아예 안 사면 병합
  자체가 발생 안 함 — 이게 공유 계좌에서 유일하게 확실한 격리.
- **어떻게**: `bot/ownership.py` — arming 전 `capture_baseline(seq)`가 `GET /holdings`를 **typed
  Result(06-D)로** 1회 읽어 `USER_BASELINE = {symbol: {qty, avg, ts}}`를 fsync·원자쓰기로 고정.
  이 심볼들은 봇의 **영구 매수 denylist**. **fail-closed**: 조회가 typed-OK 아니거나(403/타임아웃)
  비면 arming 거부. 레지스트리는 자동으로 **줄지 않음**(새 외부 심볼로 늘기만) — 403→빈값이
  denylist를 비워 사고나는 것 차단. 매수 게이트(X1): `symbol ∉ USER_BASELINE ∧ broker_qty ≤
  bot_qty ∧ 외부 open order/조건주문 없음`. **주문 시점 fresh holdings 재조회**(캐시 금지).
- **주의**: 우리 유니버스 95%가 미국주고 사용자 보유도 미국주라 중첩 가능성 실재 → 이 배제가
  핵심. 배제로 봇 유니버스가 baseline과 disjoint가 되어야 함.
- **의존/Stage**: L3(typed), L4 / Stage 2.

## IS3 — 시드 봉투 (봇 예산을 계좌 현금에서 분리)

- **무엇**: 봇 자본을 **고정 SEED(예 1천만원) + 봇 원장 회계**로만 산출. 계좌 현금·매수여력은
  '예산'이 아니라 'feasibility 상한'으로만.
- **왜**: 공유 계좌 현금은 fungible → `cashBuyingPower`를 예산으로 쓰면 사용자가 기존 종목
  팔거나 입금·배당하면 봇이 그 현금까지 투입(적대적 H1·H2, CONFIRMED). 사용자 자산이 클수록
  봇 주문이 커지는 사고.
- **어떻게** (`bot/envelope.py`, 원장 fold 확장):
  - 원장 이벤트에 원가 축 추가: buy `{fill_price, fx_at_fill, commission}`→`cost_krw`,
    sell `{proceeds_krw}`, `envelope_topup/withdraw{amount, operator, ts}`(SEED 변경 유일 경로,
    서명·감사), `reconcile_adjust`.
  - `bot_cash = SEED + Σtopup − Σwithdraw − Σbuy_cost + Σsell_proceeds` (실현손익 반영)
  - `bot_open_cost = Σ(열린 lot cost_krw)` (봇의 시장 footprint)
  - **예산 공식**: `deployable = min(bot_cash, SEED − bot_open_cost)`
    - 실현손실 후 → bot_cash 바인딩(시드 줄어 덜 투입). **매도는 proceeds(실현액) 환입**
      (cost 반환이 아님 — 손실 후 과대계상 방지).
    - 실현이익 후 → `SEED − bot_open_cost` 바인딩 → **footprint를 SEED 초과로 안 키움**.
  - **불변식**: `bot_open_cost ≤ SEED` 항상.
- **주의**: 통화 — SEED는 KRW, 미국주 매수는 **fill 시점 fx로 cost_krw 고정**(이후 환율은
  평가액만 바꾸고 footprint 불변). 재시작 시 봉투를 **원장에서 재구성**(brokerholdings 아님) +
  fsync 영속 + 재구성 완료 전 매매 게이트 차단(적대적 H3).
- **의존/Stage**: 원장(O1)·L3 / Stage 2.

## IS4 — 사이징·매도를 SEED/원장 기준으로 (X2·X4 오버라이드 + 버그수정)

- **무엇**: 라이브 사이징의 분모를 계좌 equity → **고정 SEED**로, 봇 매도 상한을 브로커
  합산 → **min(봇claim, sellable)**로 교체. + 총량 게이트 신설.
- **왜 (CONFIRMED 버그 2개)**:
  1. **현행 사이징이 계좌 `equity`/`buying-power` 기준**(`autopaper.py:852`, X2 설계). 공유
     계좌면 equity≈사용자9천만+봇1천만=1억 → 봇이 **10배 크게** 주문. (모의는 격리 원장이라
     무해하나 **라이브 설계에 봉투가 아예 없음**.)
  2. **총량 게이트 부재** — `MAX_POSITIONS(5) × POS_CAP(1/3) = 시드의 1.67배`까지 투입 가능.
- **어떻게**:
  ```
  risk_notional = (RISK_PCT × SEED / per_share_risk) × price      # 분모 SEED
  symbol_cap    = POS_CAP × SEED − open_cost(symbol)              # 분모 SEED
  feasibility   = buying_power(seq)   # 계좌 현금 — 하향 클램프로만
  cap = max(0, min(deployable, risk_notional, symbol_cap, feasibility, stage_cap))
  qty = int(cap // price)            # whole-share
  ```
  - **총량 게이트**: 신규 매수 원가 ≤ `deployable = min(bot_cash, SEED − bot_open_cost)`.
  - **매도 상한**: 봇 손절 수량 ≤ `min(bot_qty claim, sellable_quantity)` — 봇 원장 수량은
    **봇 자기 체결에서만**(합산 broker값 금지). 사용자 몫을 봇이 안 팔게.
  - **조건주문(P1) 수량도 filledQuantity(봇 원장)** — holdings 합산 금지(적대적 H10: 병합
    라인 수량으로 조건주문 걸면 발동 시 사용자 주식까지 처분).
- **주의**: buying-power는 '상향 여력'이 아니라 '하향 클램프'로만(사용자 현금을 봇 실탄으로
  오인 방지). 주문 직전 재확인.
- **의존/Stage**: IS2·IS3, X1·X2·X4·P1 / Stage 2.

## IS5 — 비대칭 대사·동결 (잔여위험 봉쇄)

- **무엇**: 대사에서 `bot_qty(symbol) ≤ holdings_qty(symbol)`만 강제(claim 상향 절대 금지),
  공동보유/설명불가 수량변화 감지 시 그 종목 **close-only 동결·MANUAL_REVIEW_LOCK**.
- **왜**: 원장 claim은 '증명'이 아니라 '주장' — 두 대사 주기 사이 사용자가 앱에서 그 종목을
  거래하면 claim이 조용히 어긋남(residual risk). 피해를 봉쇄(창을 0으로 만들진 못함).
- **어떻게**: L4 대사 확장 — 초과 감지=claim 하향+동결. UNKNOWN 시드 회계는 **보수적**
  (submit 시 의도금액 즉시 예약 차감, 확인된 미체결만 사후 환급 — 과소지출 방향 실패).
  기업행위(분할·티커변경)로 봇 체결로 설명 안 되는 holdings 변화 → MANUAL_REVIEW_LOCK.
- **주의**: sellable<의도면 '청산됨' 표시 금지 — 가능한 만큼만 발화·잔여 잠금·재시도·P0.
- **의존/Stage**: L4·IS2·IS3 / Stage 2.

---

## 요약 — 권고 순서
1. **[실측 VA]** 토스에 봇 전용 계좌 개설 가능 + API 키 스코프 확인 → 되면 **IS1-A(별도 계좌)**,
   IS2~IS5 대부분 불필요해짐(가장 깨끗).
2. 안 되면 **공유 계좌 논리 격리**: IS2 심볼배제 + IS3 시드봉투 + IS4 SEED사이징 + IS5 동결.
3. **버그 2개(equity 사이징·총량 게이트)는 별도 계좌든 공유든 반드시 수정** — 라이브 사이징이
   계좌 전체값을 쓰면 어느 경우든 위험.
4. 잔여위험 1위 = 공유 계좌 심볼중첩 귀속 미결정성 → IS2(심볼배제)로 최소화, 별도 계좌로 소멸.
