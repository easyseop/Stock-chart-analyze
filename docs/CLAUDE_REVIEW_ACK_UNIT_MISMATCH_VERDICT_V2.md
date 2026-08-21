# Claude 부분 재검토 판정 V2 — ACK before 미상 복구·숫자 msg_cd

검토일: 2026-08-22 · 대상: `codex/ack-unit-mismatch` @ `8c32dfd1`
(수정 코드 `3e07c2d2` · 직전 검토 `8398e839`)
요청서: `docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH_V2.md`
이전 판정: `docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH_VERDICT.md` (P1 1 · P2 1 · P3 5)

## 판정: **병합 가능 — P0 0 · P1 0 · P2 0 · P3 2**

이전 **P1-1 해소 · P2-1 해소**를 검토자 독립 프로브로 확인했다. 남은 P3 2건은
비차단이며 병합 후 처리해도 무방하다. 사용자 병합 승인이 별도로 필요하다.

---

## P1-1 해소 — before 미상 ACK에 복구 경로가 생겼다

### R1-a 운영자 예외 (지정 조건 8개 전부 실측)

정상 경로 실측 출력:

```
kind=absence-reject · before_unknown=true · result=rejected · unfrozen=true
감사 2건: [('ack-resolve-intent', True), ('ack-resolve', True)]
회계 sync_fill 호출 = 0건
```

`before_unknown`이 intent·result **양쪽**에 남고, 0체결 종결이라
`kis_accounting.sync_fill`은 **0회**다. 지시서 요구와 일치한다.

**자동 3경로는 그대로 보류**(before=None 기준): balance 0 · absence 0 ·
direct 0. 완화 흔적 없음.

거부 케이스 8종 — 전부 `apply` 거부 + 원장·freeze **바이트 해시 동일**:

| 케이스 | 결과 |
|---|---|
| 거래소 1곳 조회 실패 | `RuntimeError(US NYSE broker evidence untrusted)` |
| ccnl에 ODNO 존재 | `kind=hold` |
| partial 상태 | `kind=hold` |
| cancel_pending | `kind=hold` |
| BUY | `kind=hold` |
| 동일 심볼 2건 | `RuntimeError(소유 경계…다중주문)` |
| baseline 기보유 | `RuntimeError(소유 경계…)` |
| 유예 미달(방금 낸 주문) | `kind=hold` |

경계값도 확인: 요구 유예 600s 기준 **599s → hold · 601s → absence-reject**.

기존 `known_unchanged` 경로에는 `state in ("submitted","ack") and
filled_so_far == 0`이 **새로 추가**됐다. 이는 완화가 아니라 **강화**다
(반증 질문 1의 답). 다만 `unknown` 상태 주문은 이제 CLI 부재 종결 대상에서
빠진다 — 보수적 방향이고 `reconcile_unknowns`가 따로 담당한다.

### R1-b 단일 심볼 격리 (지정 픽스처 그대로)

```
INGR total=11/sellable=6, BROKEN total=손상/sellable=2
→ market total   = None                    ✅ 완전 스냅샷 계약 불변
→ symbol_total   = 11                      ✅
→ sellable       = {INGR:6, BROKEN:2}      ✅
→ SELL hldg_before = 11, 전송수량 = 6      ✅ (요청 9 → min(9,6))
→ holdings()     = None                    ✅ 완전 map 소비자는 부분 map 못 받음
```

숫자를 발명하지 않는 것도 확인했다:

- 대상 행 자체 손상(`symbol="BROKEN"`) → `symbol_total = None`
- 페이지 미완 / 행 구조 파손 / HTTP 실패 → `holding_quantities` 전체 `None`

추가 KIS 호출 **0건**(symbol 지정 호출 1회 → balance 1회).

---

## P2-1 해소 — 숫자 msg_cd가 운영자 표면에서 보인다

지정 픽스처(`last_msg_cd=40570000`, `last_msg1="주문가능금액 부족
account=12345678"`) 실측:

```
/진단          :  · OMCL BUY · 40570000 주문가능금액 부족 account=[REDACTED]
종결 알림      : 사유 미상(마지막 관측: 40570000 주문가능금액 부족 account=[REDACTED])
```

코드 보존 · 계좌 비노출. submit 경로(`40910000`)도 동일하게 확인했다.

**code=True 예외가 본문으로 새지 않는 것**을 별도 반례로 확인했다 —
`last_msg1="계좌 87654321 확인"` · `last_status="상태 11223344"`를 넣어도
두 숫자는 `/진단`과 종결 사유 양쪽에서 전부 `[REDACTED]`이고 코드만 남는다.

공개 ntfy는 category-only 그대로다(`bot/notify.py` diff 0). 종목·수량·코드·
계좌가 본문에 실리지 않음을 실측했다.

---

## 회귀·안전 계약

- Python **73/73 PASS** · Node **19/19 PASS** · compileall · `node --check` ·
  `git diff --check` — 검토자 독립 실행으로 전부 재현.
- 안전 경로 **diff 0**: `kill.py` · `kill_self_heal.py` · `kill_cli.py` ·
  `watchdog.py` · `ownership.py` · `notify.py` · `kis_exits.py` ·
  `kis_orders.py` · `risk_budget.py`.
- C2 경계 유지 실측: 동결+exact ODNO → 확정 1·회계 1·**동결 유지**,
  baseline 기보유 → 차단, baseline 미무장 → 차단, partial → 동결 유지.
- R3 보완 5건 전부 실측 통과(빈 key `ValueError` 3종, 관측 2회째 byte 불변,
  `safe_plan`에서 `key`·`filled` 제거).

검토자 독립 뮤테이션 10종: **9 KILLED · 1 SURVIVED**(아래 P3-1).

---

## P3 (비차단)

### P3-1. 운영자 예외의 `side == "SELL"` 제한이 테스트로 고정돼 있지 않다

`operator_unknown_sell`에서 `side == "SELL"`을 제거한 뮤턴트가 **SURVIVED**.
실측하면 BUY(before=None)도 `absence-reject`가 된다.

현재 코드는 정상이다(검토자 프로브: BUY → `kind=hold`). 다만 이 제한은 단순
보수성이 아니라 **비대칭 위험**을 막는다: BUY가 실제로는 체결됐는데 0체결로
종결되면 원장에 없는 보유가 생겨 손절 기록조차 없는 포지션이 된다. SELL의
오종결은 클램프가 흡수한다(있지도 않은 수량을 팔려다 거부). 방향이 다르므로
회귀로 고정하는 편이 좋다.

**최소 수정**: BUY(before=None)가 `kind=hold`임을 단언하는 테스트 1건.

### P3-2. 원장에 저장되는 `broker_reason`에서는 숫자 코드가 다시 가려진다

알림(메모리)과 원장(저장)이 갈린다 — 검토자 실측:

```
① 메모리 반환값(알림이 쓰는 값):
   사유 미상(마지막 관측: 40570000 주문가능금액 부족 account=[REDACTED])
② 원장 저장값(사후 포렌식이 보는 값):
   사유 미상(마지막 관측: [REDACTED] 주문가능금액 부족 account=[REDACTED]
```

원인은 `ledger.record_reconcile_meta`의 `safe_value`다 — `msg_cd`로 끝나는
필드만 `code=True`이고, 조립된 `broker_reason`은 `code=False`로 다시 정화돼
8자리 숫자가 계좌번호로 오인된다. 200자 상한에 걸려 끝이 잘리는 것도 같이
보인다(닫는 괄호 소실).

**사용자가 보는 알림은 정상**이고 구조화 필드 `last_msg_cd`에는 코드가
`code=True`로 온전히 남아 있으므로 정보 손실은 아니다. 과다 정화(안전 방향)라
P3로 둔다.

**최소 수정**: `broker_reason`을 whitelist에서 코드 보존 대상으로 분류하거나,
저장 전에 이미 정화된 값임을 표시해 이중 정화를 피한다.

---

## 반증 질문 12개 판정

1 ✅(오히려 강화) · 2 ✅(BUY·partial·cancel_pending 전부 hold) ·
3 ✅(둘 다 RuntimeError) · 4 ✅(ccnl ODNO 존재 → hold) ·
5 ✅(조회 실패 시 intent 포함 쓰기 0 — 바이트 해시 동일) ·
6 ✅(sync_fill 0회) · 7 ✅(symbol_total 11 · holdings None) ·
8 ✅(대상 행 손상·페이지 미완 모두 None) · 9 ✅(본문 숫자 전부 마스킹) ·
10 ✅(`safe_plan`에서 key·filled 제거, ODNO·계좌·가격 없음) ·
11 ✅(partial → 동결 유지, 자동 direct → 동결 유지) · 12 ✅(ntfy diff 0).

---

## 병합 후 반드시 할 일 (수정과 별개, 그대로 유효)

지금 갇힌 **INGR SELL은 배포만으로 풀리지 않는다.** 그 주문의 원장
`hldg_before`에는 옛 값 `6`(매도가능)이 박혀 있어 배포 후에도 `delta=-5`로
동결이 재현된다(직전 검토 C1-c 실측).

절차: `scripts/kis_ack_resolve.py --key <INGR SELL 키> --plan`으로 증거를 먼저
확인하고, 판단이 서면 `--apply --ack "<사유>"`. 운영 apply는 사용자 승인 사항이라
이번 검토에서 실행하지 않았다.
