# Claude 적대 검토 판정 — ACK 단위 불일치·동결 직접증거·거절 사유

검토일: 2026-08-22 · 대상: `codex/ack-unit-mismatch` @ `8398e839` (base `b7d9e3c5`)
지시서: `docs/CODEX_SPEC_ACK_UNIT_MISMATCH.md` C1~C3

## 판정: **병합 차단 — P0 0 · P1 1 · P2 1 · P3 5**

C1·C2·C3의 **설계와 구현은 지시서대로**이고, 검토자 독립 프로브로 전부
재현했다. 차단 사유는 구현 오류가 아니라 **C1이 새로 만든 흡수 상태**다:
총보유 불신 중에 나간 보호 매도가 미체결로 끝나면 자동 4경로·운영자 CLI
어디로도 해소되지 않고, 그 종목 손절이 영구히 막힌다.

---

## 먼저, 원래 문제가 실제로 해결되는가 — 예

검토자가 발주부터 대사까지 독립 구동한 INGR 반례(실측):

| 상황 | total before | now | delta | 결과 |
|---|---:|---:|---:|---|
| 미체결 | 11 | 11 | 0 | 확정 0 · **동결 0** ✅ (이번 사고) |
| 정상 5주 체결 | 11 | 6 | 5 | 확정 1 · 회계 1 ✅ |
| 진짜 이상(전량 소멸) | 11 | 0 | 11 | **동결 유지** ✅ (놓치지 않음) |
| 총보유 불신 | None | 6 | - | 보류 · 동결 0 ✅ |

발주 측도 실측했다 — `total=11 · sellable=6`에서 `hldg_before=11`,
전송수량 `min(9,6)=6`으로 클램프 유지. 지시서의 "두 용도 분리"가 정확히
구현됐다.

`bot/kis.py:holding_quantities()`는 한 응답에서 두 단위를 파싱하고,
페이지네이션 미완은 **두 map을 함께** 불신한다(A3 실측: 전체 `None`).
한 단위만 손상되면 다른 단위는 살린다(A4·A5). 추가 KIS 호출 0건.

## C2 — 동결 경계는 지시서대로 정확하다

검토자 8종 프로브 전부 통과:

| 시나리오 | 결과 |
|---|---|
| 동결 + exact ODNO 체결 | 확정 1 · 회계 1 · **동결 유지**(자동해제 0) ✅ |
| 동결 + 잔고 delta 추론 | 보류 ✅ |
| 동결 + 부재 증명 | 보류 ✅ |
| 동결 + ODNO 미결속 합성후보 | 보류 ✅ |
| 사용자 baseline 기보유 | ODNO 있어도 차단 ✅ |
| ownership 미무장 | 차단 ✅ |
| 같은 심볼 SELL 2건 / BUY+SELL | 보류 ✅ |
| 0체결 행(CVNA 함정) | 확정 0 ✅ |

`kill.py`·`kill_self_heal.py`·`watchdog.py`·`kis_exits.py`·`risk_budget.py`
**diff 0**. `notify.py` 미변경 — 공개 ntfy category-only 계약 그대로.

## CLI — 안전장치 실측

원장·freeze 파일 **바이트 해시**로 확인했다:

| 검사 | 결과 |
|---|---|
| `--plan` 읽기 전용 | digest 불변 ✅ |
| 빈 ack 3종(`""`·공백·None) | 전부 `PermissionError` · digest 불변 ✅ |
| 거래소 1곳(NYSE) 조회 실패 | plan·apply 모두 `RuntimeError` · digest 불변 ✅ |
| 증거 불충분 | `kind=hold` · apply 거부 · digest 불변 ✅ |
| 정상 apply | 확정 + 동결해제 + 감사 2건(intent·result) ✅ |

## 검증 증거 재현

- Python **73/73 PASS**(검토자 독립 실행). Node **19/19 PASS**.
  compileall·`node --check`·`git diff --check` PASS.
  ※ 워크트리를 `/tmp` 아래 두면 `test_ownership_baseline`이 실패하는데,
  **base 커밋에서도 동일**하므로 이 브랜치의 회귀가 아니다(환경 산물).
- 검토자 독립 뮤테이션 10종: 6 KILLED, 1 무효(내 작성 오류 — 재작성 후
  KILLED), 3 SURVIVED(아래 P3).

---

## P1-1 (차단) — `hldg_before=None` 보호 매도는 영구 미해소 = 손절 영구 스킵

### 재현 (전 경로 실측)

총보유 불신 중 나간 보호 매도(`hldg_before=None`)가 미체결로 끝나고 브로커
행도 없을 때:

```
2순위 잔고대사   0건   (before is None → continue)
3순위 부재증명   0건   (before_raw is None → continue)
1순위 직접증거   0건   (체결행 없음)
운영자 CLI      plan kind=hold · apply 거부 "현재 증거로 자동/운영자 확정 불가"
→ 주문 상태 ack 고정 · 열린주문 1건
```

CLI의 `absence-reject`도 `before is not None`을 요구하므로 **사람도 풀 수
없다**(`scripts/kis_ack_resolve.py`의 absence 분기).

### 왜 P1인가 — 흡수 상태이고 보호가 꺼진다

```
bot/sentinel.py:644
    if (ledger.open_order_count(code, side="SELL") >= 1
            or ledger.open_order_count(code, side="CANCEL") >= 1):
        continue                      # ← 이 종목 손절 판단 자체를 건너뜀
```

실측: 갇힌 주문 1건에서 `open_order_count('AMD', side='SELL') = 1` →
**그 포지션의 손절이 다시는 나가지 않는다.**

이 서브시스템 전체가 막으려던 실패 모드(`resolve_acks_by_balance` 독스트링
①: "파수꾼이 그 종목 손절을 영원히 스킵 = 무보호")가 새 경로로 되살아났다.

**변경 전과의 비교가 핵심이다.** 예전에는 잔고 응답이 손상되면
`sellable_holdings`가 `None`이라 주문이 아예 안 나갔고, 다음 사이클에 응답이
회복되면 재시도됐다 — **일시적·복구 가능**. 지금은 주문이 나가고 그 결과를
영원히 알 수 없는 **흡수 상태**가 된다. 확률은 낮지만(총보유만 파싱 실패 +
매도가능은 성공) 방향이 나쁘다.

또 `total_ok`는 **시장 단위 전역 플래그**라 한 행만 이상해도 그 시장 전체의
total이 `None`이 된다(`bot/kis.py` 파싱 루프). 트리거 폭이 생각보다 넓다.

### 이건 지시서의 빈틈이기도 하다

내 지시서 요구사항 4·5가 "총보유 불신이면 `None`으로 두되 매도는 막지 말
것"이었고 Codex는 그대로 구현했다. **그 `None` 주문을 나중에 어떻게 종결할
것인가를 내가 안 썼다.** 구현자 과실이 아니라 명세 누락이다.

### 최소 수정 제안

운영자 CLI의 `absence-reject`가 `hldg_before is None`도 받아들이게 한다 —
단, 지금 요구하는 증거는 그대로 유지한다(nccs·ccnl 양쪽 완전 부재 +
10분 유예 + 총보유 fresh 재조회 성공). "총보유 불변" 비교만 불가능한
것이므로, 그 자리를 **운영자 ack의 명시적 판단**으로 대체하고 감사 이벤트에
`before_unknown: true`를 남긴다. 자동 경로는 지금처럼 계속 보류한다.

회귀 테스트 2건: ① `hldg_before=None` + 완전 부재 + 10분 → CLI가 ack로
종결 가능, ② 같은 조건에서 **자동** 경로는 여전히 0건.

---

## P2-1 — 순수 숫자 `msg_cd`가 운영자 표면에서 `[REDACTED]`가 된다

C3의 목적은 "사유를 알 수 있게"인데, 정작 KIS 오류코드가 숫자면 가려진다.

원장 왕복 실측:

```
원장 저장값 : {'last_msg_cd': '40570000', 'last_msg1': '주문가능금액 부족'}
/진단 출력  : '  · OMCL BUY · [REDACTED] 주문가능금액 부족'
```

원인은 재정화 시 `code` 인자 누락이다. 저장은 `code=True`(숫자 보존),
운영자 표면은 기본 `code=False` → `_LONG_NUMBER_RE = \d{8,}`에 걸린다.

- `bot/kis_telegram.py:_diag_order_details` — `sanitize_broker_text(meta.get(code_key), limit=40)`
- `bot/kis_reconcile.py:486-491` — `사유 미상(마지막 관측: [REDACTED])`

실측 표본:

```
40570000 ★ 마스킹    40910000 ★ 마스킹    40580000 ★ 마스킹
APBK0013  보존       IGW00002  보존
```

영문 접두 코드만 보이고 숫자 코드는 가려져 **일관성도 없다**. 안전 방향의
과다 정화라 P1은 아니고, 값은 원장에 남아 있어 복구 가능하다.

**최소 수정**: 두 지점에서 `msg_cd` 유래 필드만 `code=True`로 재정화.
(저장 시 이미 whitelist·정화를 통과한 값이다.)

---

## P3 (비차단)

1. **armed 게이트 미테스트** — `_direct_evidence_allowed`의
   `baseline() is None → False`를 제거한 뮤턴트가 **SURVIVED**. 검토자
   프로브로는 정상 동작 확인(미무장 시 확정 0). 코드는 맞고 테스트만 없다.
   C2 주장 3번이 회귀로 고정돼 있지 않다.
2. **CLI terminal-only 해제 미테스트** — `if terminal and ...`에서 `terminal`을
   뺀 뮤턴트가 **SURVIVED**. 실측하면 부분체결(`state=partial`)에서도 동결이
   풀린다. 현재 코드는 정상.
3. **관측 중복제거 미테스트** — 뮤턴트 SURVIVED. 기능 자체는 정상이다
   (실제 경로 `open_orders()`로 동일 관측 10회 → 이벤트 1건 실측). Codex의
   "원장 증가 0건" 주장은 **참**이며, 테스트만 없다.
   ※ 검토자가 처음 `_fold()` 값으로 프로브해 "중복제거 실패"로 오판했다가
   실제 호출부로 재확인해 정정했다. `_fold()` 값에는 `key`가 없다.
4. **CLI 출력에 수량·내부키 포함** — 문서는 "ODNO·계좌·가격·수량 메타를 넣지
   않습니다"라고 하는데 실제 출력에 `"filled": 5`(수량)와 `"key"`(원장키)가
   있다. ODNO·계좌·가격은 없다. 운영자 로컬 터미널 출력이고 `key`는 본인이
   입력한 값이라 위험은 낮지만, 문서와 실제가 다르다.
5. **빈 key 무방비 수용** — `record_reconcile_meta`가 `key=""`를 조용히
   기록한다. 실호출부 2곳은 모두 `open_orders()`(key 포함)라 현재 결함은
   없지만, `_fold()` 값을 넘기는 호출이 하나라도 생기면 관측이 어느 주문에도
   붙지 않고 사라진다.

---

## 반증 질문 10개에 대한 판정

1 ✅(A3 실측) · 2 ⚠️ **P1-1**(보호 SELL은 나가고 추론은 멈추나, CLI도 못
푼다) · 3 ✅(D1: 회계 1건 동반) · 4 ✅(D4·D5 보류) · 5 ✅(D2·CLI baseline
분기 차단) · 6 ✅(F3 바이트 동일) · 7 ✅(G3: 시크릿·계좌·ODNO·토큰 전부
비노출, 15개 적대 입력 누출 0) · 8 ✅(우선순위 실측 ①~④ 정상) · 9 ✅(D1:
동결 유지) · 10 ✅(KR 시장가·US chase 모두 단위 분리 확인).

---

## 병합 조건

**P1-1 해소 후 재검토.** P2-1은 같은 라운드에 함께 고치기를 권한다(2줄).
P3는 후속으로 충분하다.

## 배포 시 반드시 알아야 할 것 (수정과 별개)

지금 갇혀 있는 **INGR SELL은 이 브랜치를 배포해도 저절로 안 풀린다.**
그 주문의 원장 `hldg_before`에는 옛 값 `6`(매도가능)이 이미 박혀 있고,
소급 정정 기능은 없다. 검토자 실측(C1-c): `before=6 now=11`이면 배포 후에도
`delta=-5`로 동결이 재현된다.

배포 후 절차는 `scripts/kis_ack_resolve.py --key <INGR SELL 키> --plan`으로
증거를 먼저 확인하고, `--apply --ack "<사유>"`로 종결·해제하는 것이다.
이 경로는 F4에서 정상 동작을 실측했다(확정 + 동결해제 + 감사 2건).
