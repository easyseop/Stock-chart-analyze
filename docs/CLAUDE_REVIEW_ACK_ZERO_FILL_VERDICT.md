# Claude 적대 재검토 판정 — 0체결 행 + stale hldg_before ACK

검토일: 2026-08-22 · 대상: `codex/ack-zero-fill-stale-before` @ `7da43f90`
(구현 `61dce915` · 회귀 `d5fd1b88` · base `9dd278e5`)
지시서: `docs/CODEX_SPEC_ACK_ZERO_FILL_STALE_BEFORE.md` F1~F4

## 판정: **병합 차단 — P0 0 · P1 1 · P2 0 · P3 2**

※ 2026-08-22 갱신: 서버 실측(PEP 총보유 16 · 매도가능 16)으로 P2-1을
P3-2로 강등했다. 아래 해당 절에 근거를 적었다.

F1·F2·F3는 지시서대로이고 독립 프로브로 전부 재현했다. 차단 사유는 **F4가
조회 6회를 파수꾼 핫루프 안에 넣은 것**이다 — 관측성 기능이 heartbeat SLA를
깨서 kill L1을 유발할 수 있는 구조다.

---

## F1·F2 — 갇힌 ACK가 풀린다 (해소 확인)

INGR 실물 픽스처(before=6 · 현재 11 · ccnl 0체결 1행 · nccs 0행 · 10분 경과):

```
kind=operator-zero-fill · result=rejected · unfrozen=true
감사 2건: [('ack-resolve-intent', zero_fill=True, before=6, now=11),
           ('ack-resolve',        zero_fill=True, before=6, now=11)]
회계 sync_fill = 0건
출력에 k#1(원장키)·7001(ODNO)·수량 메타 전부 비노출
```

**자동 3경로는 같은 입력에서 여전히 0건**(balance 0 · absence 0 · direct 0).
완화 흔적 없음.

새 분기는 기존 분기 **앞의 별도 `if`** 이고 기존 것은 `elif`로 밀렸다. 두
분기는 `c_has` 조건이 정반대(신규 `c_has` 참 / 기존 `not c_has`)라 상호
배타적이므로 케이스를 가로채지 않는다. 반증 질문 2의 답이다.

`_closed_zero_fill_row` 공유는 실질적이다 — CLI는 함수를 직접 호출하고 복제
판정이 없다(코드 검증).

유예 경계 실측: **599s → hold · 600s → operator-zero-fill · 601s →
operator-zero-fill**.

거부 케이스 9종 전부 `apply` 거부 + 원장·freeze **바이트 해시 동일**:

| 케이스 | kind |
|---|---|
| 0체결 2행 | hold |
| nccs 생존 | hold |
| BUY | hold |
| partial | hold |
| cancel_pending | hold |
| 동일심볼 2건 | RuntimeError |
| baseline 기보유 | RuntimeError |
| 총보유 조회 실패 | RuntimeError |
| 양수 체결행 | direct-fill(정상 경로) |

## F3 — 래치 전이 정확

| 검사 | 결과 |
|---|---|
| 오래된 열린 SELL | P0 1회 · 재호출 시 중복 0 |
| 짧은 in-flight(60s) | 침묵(임계 1800s) |
| **전송 실패** | 래치 안 함 · 다음 사이클 재시도(2회 확인) |
| 해소 | 회복 1회 · 모듈 재로드 후에도 중복 0 |
| 닫힌 시장 | 회복 판정에서 제외(래치 보존) |

## F4 판정식 — 산술은 정확

```
11/1/open0  → 경보(90.9%)      11/6/open5  → 침묵(설명됨)
11/1/open5  → 경보(45.5%)      11/1/open10 → 침묵
11/11/open0 → 침묵             11/1/open99 → 침묵(과대 설명)
```

불신 입력(snapshot None · total 결측 · sellable>total · 음수) 전부 보류.
`_remaining_sell`은 부분체결 잔량을 정확히 계산한다(5주 중 2체결 열림 +
3주 완료 닫힘 + BUY → `{INGR: 3}`), `filled>ordered`·비정상 타입은 `None`.
한 거래소 실패 시 전체 `None`으로 다른 시장 합계 세탁 없음.

## 회귀·안전 계약

Python **74/74** · Node **19/19** · compileall · `git diff --check` 재현.
안전 경로 **diff 0**: `kill.py`·`kill_self_heal.py`·`kill_cli.py`·
`ownership.py`·`notify.py`·`kis_orders.py`·`kis.py`·`kis_exits.py`·
`watchdog.py`.

`protection_observability` import graph에 주문·kill·unfreeze 변경 경로 없음
(`ledger`·`kis`·`kis_reconcile`·`notify`만, 금지 심볼 직접 참조 0) — 반증
질문 12의 답이다.

검토자 독립 뮤테이션 11종: **10 KILLED · 1 SURVIVED**(P3-1).

---

## P1-1 (차단) — F4의 블로킹 조회 6회가 파수꾼 핫루프 안에 있다

### 실측

감사 1회가 부르는 KIS 호출(US 개장 시):

```
balance NASD · nccs NASD · balance NYSE · nccs NYSE · balance AMEX · nccs AMEX
= 6회
```

호출당 3초만 가정해도 감사 소요 **18.0초**(실측). 그런데 `bot/kis.py:_get`은
`for attempt in range(3)`이고 각 attempt가 `유량대기 10s + HTTP 15s`이므로
**호출당 최악 75초 · 6회면 450초**다.

그리고 이 구간에 **heartbeat 갱신이 없다**. `protection_observability`는
heartbeat를 전혀 참조하지 않고(`grep` 0건), `sentinel.py`는 감사 **직전에**
`_beat(state, phase="before_protection_audit")`를 한 번 찍을 뿐이다. 감사
소요분이 heartbeat 나이에 그대로 더해진다.

```
AGE_OK_S = 30 · AGE_P0_S = 60 · AGE_HARD_S = 120 → HARD_DISABLE → kill L1 상향
```

### 왜 P1인가 — 하필 그때 터진다

호출당 3초면 18초로 `AGE_OK_S`를 넘고, 타임아웃 15초에 걸리면 90초로
`AGE_P0_S`를 넘어 watchdog P0·sentinel 재기동 판정에 들어간다. 재시도까지
겹치면 `AGE_HARD_S`를 넘겨 **kill L1이 올라가 신규 진입이 막힌다.**

이 지연이 커지는 조건은 **KIS 응답이 느려질 때**인데, 그건 heartbeat가 이미
눌려 있는 바로 그 순간이다. 어제 25분짜리 `TimeoutError` 장애가 정확히 그
상황이었고, 그 사고 때문에 만든 것이 지금의 watchdog 장애 분류다. 관측성
기능이 자기가 관측하려던 사고를 스스로 유발하는 구조는 방향이 틀렸다.

읽기 전용이고 예외 격리도 돼 있지만, **예외가 아니라 지연이 문제**라
`try/except`로는 막히지 않는다.

### 최소 수정 제안 (택1)

1. **데드라인 감싸기** — `ops_status._call_before_deadline`과 같은 방식으로
   감사 전체에 상한(예 10초)을 걸고, 초과하면 그 사이클을 버린다. 관측성이라
   한 번 걸러도 손해가 없다.
2. **핫루프 밖으로** — 텔레그램/ops 프로세스처럼 손절 판단과 무관한 곳에서
   돌린다. `ops_status`가 이미 주기 점검 루프를 갖고 있다.
3. 최소한 **거래소 사이마다 `_beat`** 를 찍어 나이 누적을 끊는다(1·2보다 약함
   — 호출 하나가 75초면 여전히 위험).

F3(원장만 읽음)은 이 문제가 없다. **F4만** 해당한다.

---

## P3-2 (강등) — 미결제·비주문 예약 오탐 가능성

```
총보유 16 · 매도가능 0 · 열린매도 0  (오늘 매수한 미결제 가정)
→ 🚨 PEP 설명되지 않는 매도가능 부족 100.0%
```

판정식이 "열린 매도로 설명되지 않으면 이상"인데, 매도가능을 줄이는 사유는
미체결 매도만이 아니다. 결제 미도래·대차·증거금 등 **정상적인 비주문 예약**이
전부 미설명으로 잡힌다. 신규 매수마다 P0가 울리면 손절 경보가 다니는 채널이
무뎌진다.

지시서가 "열린 매도로 설명되는 차이"만 예외로 적었고 Codex는 그대로 구현했다.
**명세 누락이지 구현 과실이 아니다.**

다만 이게 실제로 울리는지는 미확인이다 — KIS 모의에서 당일 매수분이 즉시
매도가능인지 확인이 필요하다. 서버에서 8/21 매수한 PEP·NKE를 보면 바로 갈린다:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from bot import kis
for s in ('PEP','NKE'):
    q = kis.holding_quantities('US', excg='NASD', symbol=s)
    print(s, q and q.get('symbol_total'), (q or {}).get('sellable',{}).get(s))"
```

### 실측 결과 (2026-08-22) — 오탐 미관측

```
PEP  총보유 16 · 매도가능 16      (8/21 매수, 조회 시점 다음 날)
```

**같다.** 이 브로커에서 매수분이 결제 미도래로 매도가능에서 빠지지 않는다.
가장 그럴듯했던 오탐 경로가 실측으로 배제됐으므로 **P2 → P3 강등**한다.

다만 완전 배제는 아니다. 표본이 1건이고, 확인된 것은 *매수 다음 날*이다.
**매수 당일 장중**은 관측되지 않았는데 F4는 장중 10분마다 돌므로 그 창은
남아 있다. 대차·증거금 등 다른 비주문 예약도 미확인이다.

**최소 수정(권장, 비차단)**: 연속 N회(기본 2~3회) 같은 갭이 유지될 때만
알린다. 일시 예약은 다음 사이클에 풀리고 INGR 같은 영구 누수만 남는다.
표본 1건에 기대는 대신 값싼 보험을 두는 쪽이다.

---

## P3-1 — `side == "SELL"` 제한이 또 미테스트

`operator-zero-fill`에서 `side == "SELL"`을 제거한 뮤턴트가 **SURVIVED**.
실측하면 BUY(0체결행)도 `operator-zero-fill`이 된다. 현재 코드는 정상이다
(BUY → hold 확인).

**직전 라운드 P3-1과 같은 항목이 새 분기에 그대로 재발했다.** 그때 지적한
비대칭도 그대로다 — BUY 오종결은 원장에 없는 보유를 만들어 손절 기록조차 없는
포지션이 되고, SELL 오종결은 클램프가 흡수한다.

**최소 수정**: BUY(0체결행·나머지 조건 충족)가 `hold`임을 단언하는 테스트 1건.

---

## 반증 질문 12개 판정

1 ✅(함수 직접 호출·복제 0) · 2 ✅(상호 배타 `if`/`elif`, 기존 기준 불변) ·
3 ✅(599s·2행·nccs 생존·양수체결 전부 정확) · 4 ✅(sync_fill 0회 실측) ·
5 ✅(비교 안 하고 감사에만, 실패 시 거부) · 6 ✅(전부 RuntimeError) ·
7 ✅(60s 침묵·skip 규칙 diff 0) · 8 ✅(전송 실패·재로드·닫힌 시장 전부 정확) ·
9 ✅(잔량 계산 정확) · 10 ✅(한 거래소 실패 → 전체 None) ·
11 ✅(수량·계좌·ODNO 비노출) · 12 ✅(import graph 청결).

**12개 모두 통과했다.** P1-1은 반증 질문이 겨냥하지 않은 축 — *정확성이 아니라
지연* — 에서 나왔다.

---

## 병합 조건

**P1-1 해소 후 재검토.** P3-1(테스트 1건)·P3-2(연속 유지 가드)는 비차단이지만
같은 라운드에 넣으면 왕복이 준다.

F1·F2·F3는 이미 검증됐으므로 재구현할 필요가 없다. **F4의 실행 위치만**
바꾸면 된다.
