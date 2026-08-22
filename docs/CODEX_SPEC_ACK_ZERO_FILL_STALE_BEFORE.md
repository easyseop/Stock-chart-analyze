# Codex 긴급 지시서 — 0체결 행 + 오염 hldg_before로 갇힌 ACK

작성: 2026-08-22 · 대상 브랜치: `codex/ack-zero-fill-stale-before`
(base = `claude/happy-gauss-cwoq21` @ `5473cf65`)
선행: `docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH_VERDICT_V2.md` (병합 완료)

앞선 라운드에서 `hldg_before=None` 케이스는 해결했습니다. 그런데 **운영 중인
실물 한 건이 그 변종에 걸려 여전히 갇혀 있습니다.** 검토자가 앞선 검토에서 이
변종을 보지 않았습니다.

---

## 0. 실물 상태 (2026-08-22 실측)

```
주문키   xe:INGR:half:2026-08-11#2   SELL  state=ack  hldg_before=6
브로커   NYSE ccnl 1행: ft_ccld_qty=0, ft_ccld_unpr3=0, prcs_stat_name=''
         NYSE nccs 0행  (미체결에는 없음)
CLI      --plan → kind=hold · exact_odno_matches=1 · resolvable=false
동결     INGR frozen=true · non-terminal 1건 · ownership armed · baseline 아님
```

`hldg_before=6`은 옛 버그가 남긴 **매도가능 수량**이고 당시 총보유는 11이었다.

---

## F1 (P1) — 운영자 CLI의 부재 판정이 자동 경로보다 좁다

### 증거

자동 경로 `bot/kis_reconcile.py:resolve_acks_by_absence`:

```python
zero_fill_row = _closed_zero_fill_row(ccnl_rows, odno)
ccnl_has_order = has_order(ccnl_rows)
if has_order(nccs_rows) or (ccnl_has_order and zero_fill_row is None):
    continue
```

→ ccnl에 그 ODNO의 **단일 0체결 행**이면 통과한다.

운영자 CLI `scripts/kis_ack_resolve.py:collect_plan`:

```python
c_has = any(... for row in evidence["ccnl"])
if (not n_has and not c_has and ...):
```

→ ccnl에 **뭐라도** 있으면 무조건 배제. zero-fill 예외가 없다.

결과: 자동은 처리할 수 있는 형태를 사람은 못 푼다. 그리고 그 자동 경로는
동결 때문에 막혀 있어 **완전 교착**이다.

### 요구사항

`collect_plan`이 `kis_reconcile._closed_zero_fill_row`를 **그대로 재사용**해
같은 판정을 쓴다. 판정 로직을 CLI에 복제하지 말 것 — 두 벌이 되면 다음에 또
어긋난다.

---

## F2 (P1) — `hldg_before`가 오염된 숫자면 어떤 분기도 못 탄다

### 증거

```python
known_unchanged      = before is not None and current == before   # 6 == 11 → False
operator_unknown_sell = side == "SELL" and before_raw is None      # 6이라 False
```

F1을 고쳐도 이 주문은 두 분기 모두 실패해 여전히 `hold`다. 앞선 라운드는
`before=None`만 다뤘고 **`before=틀린 숫자`** 는 다루지 않았다.

### 설계 — 잔고 비교를 빼고 브로커 진술을 쓴다

**0체결 행 자체가 "이 주문은 0주로 종결됐다"는 브로커의 직접 진술**이다.
잔고 delta 추론보다 강한 증거이므로, 이 경우에는 `hldg_before` 비교를
요구하지 않는다. 새 kind를 만든다:

```
kind = "operator-zero-fill"
```

**요구 조건(전부 충족해야 함):**

1. `side == "SELL"`
2. state가 `submitted`/`ack`, 기존 체결량 0
3. nccs 전 거래소에서 그 ODNO **완전 부재**
4. ccnl에 그 ODNO의 **단일 0체결 행**(`_closed_zero_fill_row` 판정)
5. `max(REJECT_ABSENCE_MIN_S, ACK_AGE_MIN_S)` 경과 — CVNA 실측대로 mock은
   체결 직후에도 0체결 행을 잠깐 노출하므로 유예를 반드시 유지
6. 동일 심볼 broker in-flight 정확히 1건
7. ownership armed, 사용자 baseline 아님
8. **fresh 총보유 조회 성공**(값 비교는 안 하지만 조회 실패면 거부)
9. `--apply --ack`의 비어 있지 않은 운영자 승인

**감사에 반드시 남길 것**(intent·result 양쪽):

```
zero_fill_proof: true
hldg_before_recorded: <원장에 있던 값>
hldg_now_observed:    <이번에 조회한 총보유>
```

나중에 "이 종결이 무슨 근거였나"를 재구성할 수 있어야 한다.

### 하지 말 것

- **자동 3경로는 손대지 말 것.** 동결 시 보류, `before=None` 보류 그대로.
  이 완화는 **운영자 ack 경로에만** 연다.
- `hldg_before`를 사후에 덮어쓰지 말 것. 원장은 append-only이고, 틀린 값도
  그때 무엇을 봤는지의 기록이다. 무시하되 지우지 않는다.
- `known_unchanged` 기존 분기의 기준을 낮추지 말 것.

### 회귀 테스트 (필수)

1. INGR 실물 재현(before=6·현재 11·ccnl 0체결 1행·nccs 0행·10분 경과)
   → `kind=operator-zero-fill`, `--apply --ack`로 종결·동결해제,
   `sync_fill` **0회**, 감사에 3개 필드.
2. 같은 조건에서 **자동 3경로는 여전히 0건**.
3. ccnl에 **양수 체결** 행 → `operator-zero-fill` 아님(`direct-fill`이어야).
4. ccnl에 0체결 행이 **2개 이상** → 거부(모호).
5. nccs에 그 ODNO 존재 → 거부(살아 있는 주문).
6. 유예 미달 → 거부. 경계값(599s/601s) 확인.
7. BUY → 거부.
8. 총보유 조회 실패 → 거부 · 원장·freeze **바이트 동일**.
9. 동일 심볼 2건·baseline 기보유·미무장 → 거부.

---

## F3 (P2) — 갇힌 SELL이 손절을 막는데 아무도 안 알려준다

`bot/sentinel.py:644`:

```python
if (ledger.open_order_count(code, side="SELL") >= 1
        or ledger.open_order_count(code, side="CANCEL") >= 1):
    continue                      # 이 종목 손절 판단 자체를 건너뜀
```

이번 건은 **우연히 발견**했다. 열린 SELL 때문에 손절이 스킵되는 상태가 몇
시간째 지속돼도 알림이 없다. `ops_status`의 `ACK 30분 초과`는 "대사가 필요하다"만
말하고 "그동안 이 포지션은 무보호"라는 사실은 말하지 않는다.

### 요구사항

보유 수량 > 0인 종목이 열린 SELL/CANCEL 때문에 손절 판단에서 제외된 상태가
임계(기본 30분, env로 조정 가능)를 넘으면 **P0 1회**를 보낸다. 같은 종목
반복 알림은 래치로 억제하고, 해소되면 회복 알림 1회.

문구에 종목과 경과 시간은 넣되 수량·금액·계좌는 넣지 말 것. 공개 ntfy는 기존
category-only 계약 유지.

**손절 자체의 스킵 규칙은 바꾸지 말 것** — 중복 매도 방지가 그 규칙의 목적이다.
이번 요구는 관측성만이다.

---

## F4 (P1) — 설명되지 않는 매도가능 고갈을 아무도 안 본다

### 증거 (2026-08-22 실측, INGR)

```
ovrs_cblc_qty = 11      (총보유)
ord_psbl_qty  = 1       (매도가능)
loan_dt = ""  expd_dt = ""      ← 대출·만기 아님
브로커 미체결(US nccs) = 0건    ← 묶어둘 주문이 없다
```

산수가 정확히 맞는다 — `11 − 5(#1) − 5(#2) = 1`. 어제 22:33·22:44 두 매도가
접수 때 5주씩 예약했고 **둘 다 0체결로 사라졌는데 예약이 안 풀렸다**. 어제
매도가능이 6이었던 것(11−5)과도 일치한다.

※ 해외 nccs는 모의에서 지원된다(`("mock","US","nccs"): "VTTS3018R"` — 모의
실측 확정). "모의 nccs 미지원"이라는 기존 기록은 **국내** 얘기이므로, 이
"미체결 0건"은 신뢰할 수 있는 값이다.

### 왜 P1인가

손절 발주는 `safe_qty = min(요청, 매도가능)`이다. 지금 INGR은 11주 보유인데
**손절이 발화해도 1주만 팔린다.** 사실상 무보호다. 그런데 이 상태를 알리는
경보가 없어서 이번에도 다른 조사 중에 **우연히** 발견했다.

0체결로 끝나는 보호 매도가 쌓일수록 매도가능이 계속 깎이므로, 이건 INGR
한 종목의 문제가 아니라 **누적되는 구조적 위험**이다.

### 요구사항

주기 점검에서 다음을 만족하면 **P0 1회**를 보낸다:

```
보유수량 > 0
AND 매도가능 < 보유수량
AND (보유수량 − 매도가능) > 브로커 열린 매도주문 수량 합
```

즉 **설명되지 않는 예약**만 잡는다. 정상적인 in-flight 매도로 설명되는 차이는
경보하지 않는다. 잔고나 미체결 조회가 하나라도 불신이면 판단하지 않는다
(실패≠부재).

- 같은 종목 반복 알림은 래치로 억제하고, 해소되면 회복 알림 1회.
- 래치는 프로세스 재시작 후에도 유지(파일 영속).
- 문구에 종목과 부족 비율은 넣되 수량·금액·계좌는 넣지 않는다. 공개 ntfy는
  기존 category-only 계약 유지.
- **발주 클램프 자체는 바꾸지 말 것.** 매도가능을 넘겨 주문하면 브로커가
  거부한다. 이 요구는 관측성이다.

### 회귀 테스트

1. 보유 11·매도가능 1·열린 매도 0 → P0 1회.
2. 보유 11·매도가능 6·열린 매도 5주 → 경보 없음(설명됨).
3. 보유 11·매도가능 1·열린 매도 5주 → P0(5주로 설명 안 되는 5주 갭).
4. 잔고 조회 실패 / 미체결 조회 실패 → 경보 없음, 판단 보류.
5. 같은 상태 반복 → 알림 1회. 해소 → 회복 알림 1회. 재시작 후 래치 유지.

---

## 지켜야 할 불변식

1. 발주 클램프 `safe_qty = min(요청, 매도가능)` 약화 0.
2. 사용자 baseline denylist 완화 0.
3. kill-switch·손절 발주 경로 diff 0.
4. 동결 자동 해제 0 — 해제는 운영자 ack만.
5. `실패 ≠ 부재` — 조회 실패는 `None`.
6. 자동 경로는 애매한 증거에서 계속 보류.
7. 공개 ntfy category-only.

## 제출물

- 브랜치 `codex/ack-zero-fill-stale-before`, 커밋에 무엇을·왜.
- 신규/수정 테스트 목록과 rc, 기존 회귀 전 모듈 통과 증거.
- F1: 자동 경로와 CLI가 **같은 함수**로 판정함을 보일 것.
- F2: INGR 실물 픽스처로 수정 전 `hold` → 수정 후 `operator-zero-fill`을 실측.
- 변경하지 않은 것 명시.

## Claude가 물어볼 반증 질문

1. `operator-zero-fill`이 열리면서 `known_unchanged`·`operator_unknown_sell`
   기존 두 분기의 기준이 조금이라도 느슨해졌는가?
2. mock이 체결 직후 노출하는 0체결 행을 유예 전에 종결하지 않는가?
   (CVNA 74주 재현으로 보일 것)
3. 실제로는 체결됐는데 ccnl이 0체결 행만 주는 브로커 버그가 있다면, 이 경로가
   체결을 0으로 확정해 회계를 어긋나게 하는가? 그때 무엇이 막는가?
4. `sync_fill`이 정말 0회인가? 원장만 닫고 회계가 남으면 CVNA 재현이다.
5. 총보유를 비교하지 않는데 왜 조회 성공을 요구하는가 — 그 값을 어디에 쓰는가?
6. F3 알림이 정상 운영(짧은 in-flight)에서 오탐으로 울리지 않는가?
7. F3 래치가 프로세스 재시작 후에도 유지되는가?
8. 동일 심볼에 SELL 2건이면 F2가 거부하는가?
9. F4가 정상 in-flight 매도를 오탐하지 않는가? 부분체결 중인 주문은?
10. F4 판정이 잔고·미체결 중 하나만 불신일 때도 보류하는가?
