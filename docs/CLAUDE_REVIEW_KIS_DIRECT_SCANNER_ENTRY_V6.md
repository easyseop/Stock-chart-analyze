# KIS 스캐너 직접진입 V6 — Codex V5 재검토(P1 3·P2 1) + 구조 개선 반영

- 직전 검토 대상: `a5425c1a` (Codex 판정: 병합 차단 — P1 3건·P2 1건)
- 이 수정의 코드 커밋: `54a792c4` + `49e59d2d`
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## 0. 구조 개선 — 개별 if 봉합 대신 단일 계약 검증기

V1~V5의 반복 반례 원인(경계 분산·`or 기본값`·`float()` 편의 동작·예시 기반
테스트)을 Codex 권고 구조로 교체했다.

- **`_finite_number(value, field)`** — 주문 경로 공용 숫자 파서.
  bool(`float(True)==1.0`)·NaN·inf·비숫자 전부 ValueError, 숫자 문자열 허용.
  entry/stop/pb/target/fx/현재가 전부 이 파서만 사용 — 주문 경로에서
  `value or default`·개별 `float()` 사용 제거.
- **`ValidatedCandidate`(frozen dataclass)** — 생성 경로는
  `_validate_candidate()` **한 곳**. run_once의 시장 상태 게이트와 실행기
  호출은 원본 신호 dict가 아니라 이 불변 객체만 소비한다.
- **`_validate_candidate()`가 한 번에 강제하는 계약**: 타입(boolean 거부) ·
  `0 < stop < entry < target` · 전술 허용집합 `{full, half, pullback}`(부재만
  legacy full, 명시 위반값 거부) · 전술별 `stop < pb < entry` ·
  통화 허용집합 `{USD, KRW}`+정규화 · 통화-심볼 시장 일치 · B target 필수.
- **시장 상태 게이트**(검증 후): 보유/쿨다운 → 시세(_finite_number) →
  **전술 무관 `cur <= stop` 신규 진입 금지** → tolerance → order_px/손절폭.
- 실행기(`kis_buy.execute_entry`)는 독립 이중방어 유지+강화: bool
  price/risk/fx/limit_price/order_meta.stop 거부 추가.

## 1. Codex V5 P1/P2 대조

### P1-1 — 손절선 이탈 상태의 pullback 주문 (수정)

`cur <= stop`이면 **전술 무관** 신규 진입 금지(`gate=tactic`) — pb>cur
시장성 지정가로 붕괴 종목을 사던 경로 차단. `stop < cur <= pb`(이미 눌림
도달)는 정상 유지.
회귀 `test_no_entry_when_price_at_or_below_stop`: `cur ∈ {stop+ε → 지정가
유지, stop → 0회, stop-ε → 0회}` × A/B + full에서 stop 근접 이탈 케이스.

### P1-2 — B target ≤ entry / 오염 target (수정)

B(shelf)는 `finite target > entry` **필수** — 부재·None·0·음수·NaN·inf·
비숫자·boolean·`≤ entry` 전부 `gate=input` 주문 0. A는 target 부재·0(legacy
'목표 없음')만 None으로 허용하고 명시 오염값은 동일 차단(저장 금지).
회귀 `test_b_requires_valid_target_above_entry` ·
`test_corrupt_target_is_rejected_not_persisted`.

**계약 해석 1건 명시**: Codex 필수 테스트의 `target=100.0001`은 계약
`stop < entry < target`을 **엄밀히 만족**하므로 통과로 구현했다. 이를
차단하려면 임의의 최소 간격 한도가 필요한데, 이는 구현지시서 금지 항목
("진입 허용치·한도 임의 조정")이다. 최소 간격이 전략 요구라면 수치는
사용자 승인 사항으로 별도 처리해야 한다.

### P1-3 — JSON boolean → 1.0 주문 (수정)

`_finite_number`가 모든 숫자 필드(entry/stop/pb/target/fx/현재가)에서 bool을
거부. 실행기도 bool price/risk/fx/limit_price/order_meta.stop 직접 주입을
input에서 차단(이중방어).
회귀 `test_boolean_numbers_rejected_everywhere` ·
`test_execute_entry_rejects_boolean_inputs_directly`.

### P2-1 — 통화/심볼 시장 불일치 (수정)

통화 정규화(strip/upper) + 허용집합 {USD, KRW} +
`market_of_ccy(ccy) == market_of_symbol(code)` 일치 검증 — 불일치·미지원·
None 전부 `gate=input`. 정규화된 ccy가 세션 판정·quote·주문 인자까지 일관
사용된다.
회귀 `test_ccy_market_mismatch_blocked`: Codex 표 5건 + 정상 3건(소문자
krw 정규화 포함).

## 2. 속성 스윕 + 부작용 단언 (Codex §5·§7)

`test_property_sweep_only_validated_values_reach_executor` —
entry(7종)×stop(6종)×tactic(7종)×target(5종) 조합의 결정론적 표본
(490조합, sent 12 · blocked 478)에서 두 속성을 전 조합 단언:

1. **실행기에 도달한 인자는 전부 검증된 값**: bool 아님·finite·양수,
   `주문가 > stop`, target은 None 또는 finite `> stop`.
2. **미주문 조합은 부작용 0**: 보호 포지션 기록 0 · 원장 이벤트 0
   (gate 표시만 차단이고 딴 경로로 주문되는 거짓 통과 차단).

Hypothesis 도입은 의존성 추가라 이번 PR에 넣지 않았다(결정론적 product
표본으로 동일 커버리지 확보 — 도입 여부는 별도 승인).

## 3. Mutation 검증 (Codex §6) — 왕관보석 9건 전부 KILLED

각 불변식을 일부러 약화/제거하고 테스트가 실패하는지 실측했다
(적용→실행→복원, 커밋 `49e59d2d` 기준):

| # | mutation | 결과 |
|---|---|---|
| M1 | `stop >= entry` → `stop > entry` | KILLED |
| M2 | `cur <= stop` 가드 제거 | KILLED |
| M3 | `target <= entry` → `<` | KILLED |
| M4 | 공용 파서 bool 거부 제거 | KILLED |
| M5 | ccy/시장 일치 검사 제거 | KILLED |
| M6 | B target 필수 제거 | KILLED |
| M7 | `fresh is True` → truthy | **1차 SURVIVED** → truthy 비-bool fresh 회귀 추가(`test_non_boolean_fresh_is_rejected_for_a_too`) 후 KILLED |
| M8 | allowlist 빈 env 파일 폴백 부활 | KILLED |
| M9 | 전술 허용집합 제거 | KILLED |

M7이 실제로 테스트 공백을 드러냈고(예시 기반 테스트의 한계 그대로), 회귀
추가로 닫았다.

## 4. 구조 개선 항목 중 이번 PR 범위 밖(후속)

- `signal_feed._validated`의 행 단위 가격 계약 검증(문서 전체 fail-closed)
  — buyloop 검증기와 독립 이중방어로 추가할 가치가 있으나 `signal_feed.py`
  는 이번 브랜치에서 무변경 원칙이었다. 별도 커밋/PR로 제안.
- `validate_strategy_signal()` scanner 공용화(§8) · contract_version(§3) ·
  Hypothesis·CI mutation 자동화(§9) — 의존성/CI 변경이라 별도 승인 사항.
- 운영 단계 검증(§10: 과거 신호 재생·24h shadow·거부 분포)은 병합 후
  배포 단계에서 수행(§12와 통합).

## 5. 검증

```text
tests/test_*.py 49/49 통과 (신규 7건 포함)
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
mutation 9/9 KILLED (수동 실측, §3 표)
실제 주문 HTTP 0건(주문 전송 전부 mock · buyloop urlopen 트랩)
변경 파일: bot/kis_buyloop.py · bot/kis_buy.py · tests/test_kis_buyloop.py
안전 게이트 파일(kis_orders·ledger·envelope·ownership·kis_boot·
kis_accounting·kis_positions·signal_feed·kis_exits·sentinel·rollout·
l1_readiness) 무변경 · V1~V5 수정 전부 유지(해당 회귀 통과)
```

## 6. Codex V5 재승인 조건 6항 대조

| # | 조건 | 상태 |
|---|---|---|
| 1 | pullback cur<=stop 주문 0 | 수정(전술 무관 무효선) |
| 2 | B target 필수 finite>entry·오염값 즉시매도 차단 | 수정(+100.0001 계약 해석 §1) |
| 3 | bool 명시 거부 + 실행기 이중방어 | 수정(공용 파서) |
| 4 | ccy 허용집합·정규화·시장 일치 | 수정 |
| 5 | A/B·buyloop/실행기 회귀 | 7테스트 + 속성 스윕 + mutation 9종 |
| 6 | 전체 49 + 신규 + compileall + diff check | 통과 |

미해결 P0/P1/P2: 없음(단, §1 target 간격 해석은 리뷰어 확인 요청).
