# KIS 스캐너 직접진입 V7 — Codex V6 재검토(P1 1·P2 3·P3 1) 반영

- 직전 검토 대상: `49e59d2d` (Codex 판정: 병합 차단)
- 이 수정의 코드 커밋: `a6018f3d` + `d0e177ea`
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## P1-1 — B target ≤ 실제 발주가 (수정)

Codex 판정 수용: `target > entry`는 생산자 계약일 뿐이고, 소비자
(`decide_b: price >= target → 전량매도`) 기준 불변식은 **`target > 이번
주문의 실제 발주가`** 다. run_once의 order_px 결정 직후 B(shelf)에서
`vc.target is None or vc.target <= order_px`면 `gate=input` 차단.

- full/half: `target > cur` 필요 — `entry=100, target=100.0001, cur=100.4`는
  이제 차단(직전 테스트가 잘못 고정했던 케이스).
- pullback: order_px=pb이고 검증기 계약 `stop < pb < entry < target`이
  `target > pb`를 자동 함의 — 발주가 규칙은 full/half에서만 추가 제약이 된다.
- A는 target이 즉시매도 소비자가 없으므로(A 청산은 +1R/트레일) 검증기
  `target > entry` 유지 — 문서로 명시(Codex 재승인 조건 1의 "명시" 요구).

회귀 `test_b_target_must_exceed_actual_order_price` — Codex 매트릭스
그대로: `target ∈ {100.0001, 100.4, 100.4001, 101} × cur ∈ {99.9, 100,
100.4}` full + pullback 경계 3종, **허용 조합마다
`kis_exits.decide_b(target, order_px, …) == []`(즉시 sell 0)까지 단언**.
직전의 "100.0001 무조건 통과" 단언은 삭제·교체했다.

## P2-1 — 검증 전 원본 dict 정렬 (수정)

`_now_signals`/`_shelf_cands`는 **필터만** 하고, 정렬은 검증 후 run_once가
`vc.stage/norm/rr`로 수행한다. stage/norm/shelf.rr은 검증기가 행 단위로
파싱(부재/None=0, 명시 오염 = `gate=input` 행 거부 — bool·구조값·비숫자·
비유한). `stage=[]`·`rr={"bad":1}`이 사이클 전체를 TypeError로 죽이고 정상
후보까지 처리 불가였던 경로 제거.
회귀 `test_corrupt_priority_fields_reject_row_not_cycle`: 오염 행 거부 +
**정상 형제 행은 같은 사이클에서 계속 발주** + 부재/None legacy 진행.

## P2-2 — 부작용 단언의 실체화 (수정)

- **e2e 하네스 신설**(`test_kis_buy_gates.test_run_once_end_to_end_side_effects`):
  실행기 mock 없이 신호→검증기→실제 `execute_entry` 게이트 체인→
  `place_order`→**HTTP 경계(`kis_orders._post`) spy**까지 연결.
  차단 입력(B target≤발주가)은 원장·원가장부·보호 포지션·kill 파일 바이트가
  전부 불변, 허용 입력은 `_post` 정확 1회 + 원장 선기록(BUY submit) 존재 +
  ack 단계 포지션 미기록 + kill 무변경을 단언한다.
- 속성 스윕 강화: A/B 그룹 × entry(7)×stop(6)×tactic(7)×target(6) 표본에
  **B target > 발주가 속성** 추가. 문서 주장도 하네스가 실제 관측하는
  범위("실행기 mock 하의 보호 포지션·원장 이벤트")로 정정하고, 전체 부작용은
  e2e가 본다고 명시.

## P2-3 — 실행기 잔여 경계 (수정)

`execute_entry` input 게이트에 추가(직접 호출 가정 이중방어):

- `limit_price`: 제공 시 finite·양수(비숫자·bool·0·음수 거부)
- `qty_fraction`: bool·NaN 거부, `0 < qf <= 1`
- `seed_krw`·open/total cost·`operating_limit_krw`·`risk_pct`: 명시값의
  bool·NaN·inf·비숫자 거부(None=미지정 허용)
- `order_meta.target`(M15): 제공 시 finite·양수·**stop 초과** 요구

회귀 `test_execute_entry_rejects_invalid_optional_numeric_args`(15무효 조합 +
target 오염 6종 + 정상 통과 3종). `EntryRequest` 타입화까지의 전면 개편은
후속 구조 PR로 남긴다(이번 커밋은 경계 보강 — 게이트 순서·의미 무변경).

## P3-1 — 검증 증거 실측치 (정정)

속성 스윕 실제 출력(이 커밋 기준):

```text
[PASS] 속성 스윕 sent 5 · blocked 701 (총 706조합, A/B 포함)
```

직전 문서의 `sent 12 · blocked 478`은 수정 전 코드의 수치를 옮겨 적은
오류였다. 이후 문서는 테스트 출력 문자열을 그대로 인용한다.

## Mutation 재실행 (커밋 `d0e177ea` 기준, 적용→실행→복원 실측)

| # | mutation | 결과 |
|---|---|---|
| M1 | `stop>=entry` → `>` | KILLED |
| M2 | `cur<=stop` 가드 제거 | KILLED |
| M3 | `target<=entry` → `<` | **1차 SURVIVED**(B는 runtime 발주가 게이트가 마스킹, A에 entry-equal 케이스 부재) → A 경로에 `target=100.0` 회귀 추가(`d0e177ea`) 후 KILLED |
| M4 | 공용 파서 bool 거부 제거 | KILLED |
| M5 | ccy/시장 일치 제거 | KILLED |
| M6 | 검증기 B target 필수 제거 | 단독으로는 runtime 계층(발주가 게이트의 None 검사)이 마스킹 — 불변식 검증을 위해 **M6b 복합 mutation(양층 동시 제거)** 실행 → KILLED |
| M7 | `fresh is True` → truthy | KILLED |
| M8 | allowlist 빈 env 파일 폴백 부활 | KILLED |
| M9 | 전술 허용집합 제거 | KILLED |
| M10 | B `target>order_px` 가드 제거 | KILLED |
| M12 | 우선순위 오염 행 거부 제거 | KILLED |
| M13 | `limit_price` 검증 제거 | KILLED |
| M15 | `order_meta.target` 검증 제거 | KILLED |

M14(group/sleeve 불일치)는 미구현 항목으로 남긴다 — `run_once(sleeve, group)`
은 서버 루프가 고정 쌍('A'/'now', 'B'/'shelf')으로만 호출하는 내부 계약이며,
외부 입력이 아니다. 호출 계약 assert 추가는 후속 정리 항목.

## 검증

```text
tests/test_*.py 49/49 통과
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
mutation 13종 + M6b 복합: 전부 KILLED(위 표 — 이번 라운드 실측)
실제 주문 HTTP 0건(e2e도 _post spy — 실제 전송 없음)
변경 파일: bot/kis_buyloop.py · bot/kis_buy.py · tests/test_kis_buyloop.py ·
tests/test_kis_buy_gates.py · tests/test_shelf.py(필터-계약 픽스처 갱신)
안전 게이트 파일 무변경 · V1~V6 수정 유지(해당 회귀 통과)
```

## Codex V6 재승인 조건 7항 대조

| # | 조건 | 상태 |
|---|---|---|
| 1 | B `target > order_px` 런타임 계약(+A 처리 명시) | 수정(§P1-1) |
| 2 | 100.0001 매트릭스 교체 + decide_b sell 0 단언 | 수정 |
| 3 | 정렬값 안전 파싱/검증 후 정렬 | 수정(§P2-1) |
| 4 | 스윕 부작용 확장 + `target>order_px` 속성 | 수정(§P2-2) |
| 5 | 실행기 잔여 bare float/limit/meta 경계 | 수정(§P2-3) |
| 6 | M10~M13 KILLED | M10·M12·M13·M15 KILLED, M11은 스윕 속성 강화로 대응 |
| 7 | 전체 49 + 집중 + compileall + diff check | 통과 |

미해결 P0/P1/P2: 없음. 후속(비차단): `EntryRequest` 타입화, M14 호출 계약
assert, signal_feed 행 계약 검증, Hypothesis/CI mutation 자동화.
