# KIS 스캐너 직접진입 V8 — Codex V7 차단사항 수정 및 Claude 재검토 요청

- 직전 검토 대상: `d0e177ea` (핵심 `a6018f3d`)
- Codex 수정 코드: `3c035c36`
- 브랜치: `codex/kis-direct-scanner-entry-v8`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단
- 금지선: 이 검토와 별개로 병합·Oracle 배포·kill 하향·KIS live 전환은
  사용자 승인 없이는 수행하지 않는다.

## 1. V7 판정과 이번 수정 범위

Codex의 V7 적대검토 판정은 P0 없음, P1 1건, P2 6건, P3 2건이었다.
핵심 차단 사유는 B(매물대 반등)가 `target > order_px`만 검사해 매수 직후
즉시청산은 피하지만, 이미 승인된 전략 계약인 최소 1.5R와 최대 손절폭 15%를
KIS 실제 발주가에서 재검증하지 않는다는 점이었다.

이번 커밋은 아래를 수정했다.

1. B의 실제 발주가 기준 RR·손절폭 이중검증
2. 손상된 shelf 메타와 가격에서 계산한 RR 불일치 차단
3. 실행기 직접 호출의 숫자 부호·가격 관계·구조 입력 이중방어
4. E2E의 모든 외부 HTTP 차단과 B 허용 주문의 원장 메타 검증
5. 서로 다른 종목의 동일 signal id 원장키 충돌 제거
6. 신호 id/name/earnings 입력 계약 및 Telegram HTML escape
7. 누락됐던 M11·M14 의미를 직접 잡는 회귀 테스트 추가

## 2. P1 — 실제 발주가 기준 B 전략 계약

`bot/settings.py`에 scanner의 기존 승인값과 동일한 두 상수를 두고,
`tests/test_shelf.py`가 scanner 설정과의 드리프트를 실패시킨다.

```text
SHELF_MIN_RR = 1.5
SHELF_MAX_STOP = 0.15
```

KIS full/half 주문은 현재가 그대로가 아니라 `marketable_limit_price()`의
매수 상한(미국 +30bp, 국내 호가단위 반올림)으로 전송된다. 따라서 V8은
다음 `order_px`를 단일 진실로 사용한다.

- full/half: `marketable_limit_price(cur, BUY, market)`
- pullback: `pb_price`

그 뒤 아래 값을 실제 가격으로 다시 계산한다.

```text
risk = order_px - stop
reward = target - order_px
actual_rr = reward / risk
stop_pct = risk / order_px

허용: risk > 0
      actual_rr >= 1.5
      stop_pct <= 0.15
```

동일 계약을 `kis_buy.execute_entry()`도 `limit_price`·`order_meta`로 다시
검사한다. 호출부 검증이 실수로 약해져도 실행기 경계가 한 번 더 막는다.
사이징·원장 예약원가·실제 전송 지정가도 모두 같은 `order_px`를 쓴다.

경계 회귀는 `1.5R±ε`, `15%±ε`, 거짓 `shelf.rr`, 마켓터블 상한 때문에
신호 시점 RR은 충분하지만 실제 RR이 부족해지는 경우를 포함한다.

## 3. 손상 신호·표시·멱등키

`_validate_candidate()`는 B에서 다음을 강제한다.

- `shelf`는 dict, `shelf.rr`은 finite 양수 필수
- 가격으로 계산한 신호 RR과 메타 RR 차이가 0.05R 이내
  (scanner의 소수 둘째 자리 반올림 허용)
- 신호 시점에도 RR 1.5 이상, 손절폭 15% 이하
- `earnings_d`가 명시됐으면 finite 숫자, 손상값은 unknown으로 눙치지 않고 거부
- signal id는 `[A-Za-z0-9._:-]`, 1~128자
- name은 문자열, strip 후 최대 120자

원장 키는 이제 `prefix + code + signal_id`다. 서로 다른 종목이 같은 id를
사용해도 충돌하지 않는다. 주문 알림의 종목명과 코드는 `html.escape()` 후
Telegram HTML 본문에 들어간다.

내부 호출 계약도 `(A, now)` 또는 `(B, shelf)` 두 조합만 허용한다. 이는
누락됐던 M14(그룹/슬리브 약화)의 행동 회귀다.

## 4. 실행기 이중방어

`kis_buy.execute_entry()`는 네트워크·kill·원장 게이트 전에 다음을
`gate=input`으로 차단한다.

- order_meta 비-dict, sleeve A/B 외, 빈/비문자 symbol, KR/US 외 market
- price/risk/fx의 bool·비숫자·NaN·inf·0·음수
- seed/operating limit/risk_pct의 0·음수, 비용/보유원가의 음수
- limit/qty_fraction의 비유한·범위 위반
- open_positions/hldg_before/qty_cap의 음수·비정수·NaN·inf
- 보호 메타가 있는데 명시 발주 상한이 없는 호출
- `price_usd != limit_price`, `limit <= stop`,
  `per_share_risk != limit-stop`
- B의 target 부재/발주가 이하, 실제 RR 미달, 실제 손절폭 초과

이 검증은 주문 성공 여부와 무관한 순수 입력 경계다. 잘못된 직접 호출은
예외나 kill 변경이 아니라 input 차단으로 끝난다.

## 5. E2E와 부작용 검증

`test_run_once_end_to_end_side_effects()`는 실행기 mock을 쓰지 않는다.
검증기→잔고/시장 게이트→사이징→`kis_orders.place_order`→원장 선기록→
HTTP 경계 `_post`까지 실제 경로를 탄다.

- `urllib.request.urlopen`은 무조건 예외 trap
- `positions_detail`, `last_price`, `us_excg_of`, `buying_power_of`를 결정론적으로 격리
- 차단 B: `_post` 0, ledger/costbook/kpos/kill byte 불변
- 허용 B: `_post` 정확히 1, ledger에 `sleeve=B`, `target=110` 선기록,
  ACK 단계 kpos 0, kill 불변

테스트 실행 중 실제 주문 HTTP는 0건이다.

## 6. 회귀 결과

```text
python -m tests.test_kis_buyloop: 통과
python -m tests.test_kis_buy_gates: 통과
python -m tests.test_shelf: 통과
python -m tests.run_all: ALL PASS — Python test modules 49
python -m compileall -q bot scanner tests: 통과
git diff --check: 통과
```

V1~V7의 allowlist fail-closed, strict freshness, 숫자 bool/NaN, stop 관계,
tactic 허용집합, ccy/시장 일치, autopaper 런타임 의존 0, 기존 주문·원장·
ownership·예산·heartbeat·daily loss 게이트는 전체 회귀에서 유지됐다.

## 7. Claude 적대 재검토 요청

아래를 문서의 주장으로 믿지 말고 exact branch를 받아 독립 재현해 달라.

1. **실제 발주가 계약**
   - entry=100, stop=95, cur=100, B/full에서 미국 order_px가 100.3인지
   - target을 actual RR `1.5±ε`로 만들었을 때 미달만 input인지
   - stop을 order_px 대비 `15%±ε`로 만들었을 때 초과만 input인지
   - KR 호가단위와 US +30bp 모두 동일 규칙인지
   - half와 pullback도 각각 실제 1차 발주가를 사용하는지
2. **메타 불일치**
   - shelf 비-dict, rr 부재/NaN/음수/구조값, 가격 RR과 0.05R 초과 불일치가
     실행기 호출 0인지
   - scanner의 실제 반올림 출력이 과잉 차단되지 않는지
3. **실행기 직접 주입**
   - limit<=stop, price/limit 불일치, risk 불일치, B 저RR/과대손절,
     음수 비용/한도, inf 정수, 비문자 symbol, 잘못된 market이 모두
     예외·파일변경·HTTP 없이 input인지
4. **멱등성과 표시**
   - 서로 다른 종목의 동일 signal id가 서로 다른 원장 키인지
   - 동일 종목/동일 id 재시도는 기존 원장 멱등성이 계속 막는지
   - name=`AT&T <bad>`가 알림에서 escape되고 order 사실 알림을 깨지 않는지
5. **E2E**
   - 차단 입력의 ledger/costbook/kpos/kill byte 불변
   - 허용 B의 `_post` 1회, sleeve/target 메타, ACK≠체결
   - 예상하지 않은 모든 HTTP가 trap에 걸리는지
6. **mutation**
   - M11: 실제 RR 또는 15% 게이트를 제거하면 신규 경계 테스트가 실패하는지
   - M14: sleeve/group 계약을 제거하면 신규 호출 계약 테스트가 실패하는지
   - 기존 M1~M10·M12·M13·M15도 독립 mutation으로 퇴행하지 않았는지
   - 각 mutation patch·실패 테스트명·exit code를 결과에 남길 것
7. **회귀 범위**
   - 코드 기준 `d0e177ea..3c035c36` 6파일을 먼저 확인하고 전체 49모듈,
     compileall, diff check를 독립 실행
   - 주문 안전 게이트의 우선순위나 fail-closed 방향이 약해진 줄이 없는지

추가 반례도 계속 탐색해 달라. 특히 시장별 호가 반올림 경계, 매우 작은 risk,
엄청 큰 finite 숫자, numeric string, 중복 신호, Unicode 이름/ID, `-0.0`,
부분 체결 계획과 변경된 pos_key의 상호작용을 확인한다.

## 8. 판정 요청

P0~P3로 판정한다. P0/P1이 하나라도 있으면 병합 차단한다. P2도 운영 전
가능하면 함께 수정한다. 승인되더라도 이 브랜치를 자동 병합·배포하거나
kill-switch를 낮추지 않는다. 현재 단계는 코드와 검토자료를 원격에 올려
Claude 재검토를 받는 것뿐이다.
