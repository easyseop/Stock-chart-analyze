# Codex 수정 지시서 — ACK 단위 불일치 P1-1 · P2-1

작성: 2026-08-22 · 대상 브랜치: `codex/ack-unit-mismatch` (기존 브랜치에 이어서)
선행 판정: `docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH_VERDICT.md` (P1 1 · P2 1 · P3 5)

C1~C3 구현·동결 경계·CLI 안전장치·정화기는 **검토 통과**했습니다. 아래 2건만
고치면 병합 가능합니다. 통과한 것은 건드리지 마세요.

---

## R1 (P1-1, 차단) — `hldg_before=None` 보호 매도가 영구 미해소가 된다

### 재현된 사실

총보유 불신 중 나간 보호 매도(`hldg_before=None`)가 미체결로 끝나고 브로커
행도 없으면 **네 경로 모두** 해소하지 못합니다(검토자 실측):

```
2순위 resolve_acks_by_balance   0건   (before is None → continue)
3순위 resolve_acks_by_absence   0건   (before_raw is None → continue)
1순위 resolve_acks_from_rows    0건   (체결행 없음)
운영자 scripts/kis_ack_resolve  kind=hold · apply 거부
→ 주문 상태 ack 고정
```

그리고 이게 보호를 끕니다:

```
bot/sentinel.py:644
    if (ledger.open_order_count(code, side="SELL") >= 1
            or ledger.open_order_count(code, side="CANCEL") >= 1):
        continue                      # ← 이 종목 손절 판단 자체를 건너뜀
```

실측 `open_order_count('AMD', side='SELL') = 1` → 그 포지션 손절이 다시는
나가지 않습니다. `resolve_acks_by_balance` 독스트링 ①이 말하는 '무보호'가
새 경로로 되살아난 겁니다.

**변경 전과 비교가 핵심입니다.** 예전엔 응답 손상 시 주문이 아예 안 나가고
다음 사이클에 재시도됐습니다(일시적·복구 가능). 지금은 주문이 나가고 결과를
영원히 알 수 없는 **흡수 상태**가 됩니다.

이건 지시서 빈틈이기도 합니다 — 요구사항 4·5가 "None으로 두되 매도는 막지
말 것"이었고, 그 None 주문의 종결 방법을 명세하지 않았습니다. 구현 과실이
아닙니다.

### R1-a (필수) — 운영자 CLI에 복구 경로를 연다

`scripts/kis_ack_resolve.py`의 `absence-reject`가 `hldg_before is None`도
받아들이게 합니다. **증거 요구는 지금 그대로 유지**하세요:

- nccs·ccnl 양쪽에서 그 ODNO 완전 부재
- `max(REJECT_ABSENCE_MIN_S, ACK_AGE_MIN_S)` 유예 경과
- 총보유 fresh 재조회 **성공**(불신이면 지금처럼 거부)
- 거래소 전 구간 신뢰(하나라도 실패면 쓰기 0건)

"총보유 불변" 비교만 불가능하므로, **그 자리만** 운영자 ack의 명시적 판단으로
대체합니다. 감사 이벤트(intent·result 양쪽)에 `before_unknown: true`를
남겨 나중에 이 종결이 어떤 근거였는지 구분되게 하세요.

**자동 경로는 절대 완화하지 마세요.** `resolve_acks_by_absence`와
`resolve_acks_by_balance`는 `before is None`에서 계속 보류입니다. 사람이
개입할 때만 열립니다.

### R1-b (필수) — 트리거 폭을 줄인다

`bot/kis.py:holding_quantities()`의 `total_ok`는 **시장 단위 전역 플래그**라
한 행만 이상해도 그 시장 전체 total이 `None`이 됩니다. 발주 시점에 필요한 건
**그 심볼 한 종목의 총보유**뿐입니다.

행 단위로 신뢰를 좁히세요. 단, **소비자 계약을 깨면 안 됩니다**:
`resolve_acks_by_balance`는 완전 스냅샷을 전제로 "부재 = 0주"를 신뢰합니다.
따라서 시장 전체 map은 지금처럼 완전-아니면-전무를 유지하고, **발주 기록용
단일 심볼 조회만** 그 심볼의 행이 정상 파싱됐는지로 판단하게 하세요.
두 용도를 섞지 마세요.

### 회귀 테스트 (필수)

1. `hldg_before=None` + 양쪽 완전 부재 + 유예 경과 + 총보유 신뢰
   → CLI `--apply --ack "…"`로 종결 가능, 감사에 `before_unknown: true`.
2. 같은 조건에서 **자동 3경로는 여전히 0건**.
3. `hldg_before=None` + 총보유 조회 실패 → CLI 거부 · 원장·freeze 바이트 동일.
4. `hldg_before=None` + ccnl에 그 ODNO 존재 → CLI 거부(부재 아님).
5. R1-b: 다른 심볼 행 하나가 손상돼도 **정상 파싱된 심볼**의 발주는
   `hldg_before`에 숫자를 기록한다.
6. R1-b: 그럼에도 `resolve_acks_by_balance`에 넘기는 시장 map은 여전히
   `None`(완전 스냅샷 계약 유지).

---

## R2 (P2-1) — 순수 숫자 `msg_cd`가 운영자 표면에서 `[REDACTED]`가 된다

C3의 목적은 "사유를 알 수 있게"인데 정작 KIS 오류코드가 숫자면 가려집니다.
원장 왕복 실측:

```
원장 저장값 : {'last_msg_cd': '40570000', 'last_msg1': '주문가능금액 부족'}
/진단 출력  : '  · OMCL BUY · [REDACTED] 주문가능금액 부족'
```

저장은 `code=True`(숫자 보존)인데 운영자 표면이 기본 `code=False`로 재정화해
`_LONG_NUMBER_RE = \d{8,}`에 걸립니다. 표본:

```
40570000 ★ 마스킹    40910000 ★ 마스킹    40580000 ★ 마스킹
APBK0013  보존       IGW00002  보존
```

영문 접두 코드만 보이고 숫자 코드는 가려져 **일관성도 없습니다**.

두 지점에서 `msg_cd` 유래 필드만 `code=True`로 재정화하세요:

- `bot/kis_telegram.py` `_diag_order_details` — `sanitize_broker_text(meta.get(code_key), …)`
- `bot/kis_reconcile.py:486-491` — `사유 미상(마지막 관측: …)` / `(접수 응답: …)`
  조립 시 코드 유래 값

**메시지 본문(`msg1`·`last_status`)은 계속 `code=False`** 입니다. 거기엔 계좌
번호가 섞여 올 수 있습니다. 코드 필드만 예외입니다.

회귀 테스트: `last_msg_cd="40570000"` 한 건이 `/진단`과 종결 사유 양쪽에서
`40570000`으로 보이고, 같은 줄의 계좌번호 8자리는 여전히 `[REDACTED]`.

---

## R3 (P3, 같이 하면 좋지만 차단 아님)

1. 회귀 테스트 3건 추가 — 아래 뮤턴트가 검토자 실행에서 **SURVIVED**했습니다.
   기능은 정상이고 테스트만 없습니다.
   - `_direct_evidence_allowed`의 `baseline() is None → False` 제거
   - `apply_plan`의 `if terminal and ownership.is_frozen(...)`에서 `terminal` 제거
     (실측: 부분체결 `state=partial`에서도 동결이 풀림)
   - `_record_broker_observation`의 중복제거 `return` 제거
2. `scripts/kis_ack_resolve.py` 출력에 `"filled"`(수량)·`"key"`(원장키)가 있는데
   `docs/CLAUDE_REVIEW_ACK_UNIT_MISMATCH.md`는 "수량 메타를 넣지 않습니다"라고
   합니다. 코드를 바꾸든 문서를 바꾸든 **둘을 일치**시키세요. (ODNO·계좌·가격은
   실제로 없습니다.)
3. `ledger.record_reconcile_meta`가 `key=""`를 조용히 기록합니다. 실호출부
   2곳은 모두 `open_orders()`라 현재 결함은 없지만, `_fold()` 값을 넘기는
   호출이 생기면 관측이 어느 주문에도 붙지 않고 사라집니다. 빈 key는 거부하거나
   최소한 로그를 남기세요.

---

## 지켜야 할 불변식 (변경 금지)

1. 자동 경로는 `before is None`에서 계속 보류. 완화는 **운영자 ack 경로에만**.
2. 발주 안전 클램프 `safe_qty = min(요청, 매도가능)` 약화 0.
3. 사용자 baseline denylist 완화 0.
4. kill-switch·손절 발주 경로 diff 0.
5. `실패 ≠ 부재` — 조회 실패는 `None`, 성공·빈 결과는 `[]`/`0`.
6. 동결 자동 해제 0(코드가 스스로 풀지 않음). 해제는 운영자 ack만.
7. 공개 ntfy category-only 계약 유지.
8. C2의 동결 직접증거 경계(ODNO exact·단일 주문·armed·baseline 제외) 그대로.

## 제출물

- 커밋 메시지에 무엇을·왜.
- 신규/수정 테스트 목록과 실행 결과(모듈명 + rc), 기존 회귀 전 모듈 통과 증거.
- R1-a: 네 경로 표(자동 3 + CLI)가 수정 전후로 어떻게 바뀌는지 실측.
- R1-b: 한 행 손상 시 "발주 기록"과 "시장 map"이 각각 어떻게 되는지 실측.
- R2: 숫자 코드 보존 + 같은 줄 계좌번호 마스킹을 한 출력에서 동시에 보일 것.
- 변경하지 않은 것 명시.

## Claude가 물어볼 반증 질문

1. R1-a가 열리면 `hldg_before`가 있는 주문의 자동 종결 기준이 조금이라도
   느슨해지는가? (같은 함수를 공유한다면 어떻게 분리했는가)
2. R1-b가 `resolve_acks_by_balance`의 "부재 = 0주" 신뢰를 어디서도 깨지 않는가?
   시장 map과 단일 심볼 조회가 같은 코드 경로를 쓰는가?
3. R1-b 후에도 `hldg_before=None`이 나올 수 있는 경우는 무엇인가? 그때 R1-a로
   반드시 복구 가능한가?
4. CLI의 `before_unknown` 종결이 회계(`kis_accounting`)를 건드리는가?
   0체결 종결이므로 건드리면 안 된다 — 확인했는가?
5. R2의 `code=True` 확대가 `msg1`·`last_status` 경로로 새지 않는가?
   계좌번호가 코드 필드에 담겨 오면 어떻게 되는가?
6. R2 후 공개 ntfy 본문에 숫자 코드가 실리지는 않는가(category-only 유지)?
7. 부분체결(`partial`) 주문에서 R1-a가 실수로 0체결 종결을 허용하지 않는가?
8. 같은 심볼 non-terminal 주문이 2건일 때 R1-a가 거부하는가?
