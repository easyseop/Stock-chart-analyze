# Claude 적대 재검토 요청 — ACK/F4 P3 테스트 공백 3건

작성: 2026-08-23  
브랜치: `codex/ack-p3-test-gaps`  
기준: `claude/happy-gauss-cwoq21 @ 1fd042d0`  
선행 판정: `docs/CLAUDE_REVIEW_ACK_ZERO_FILL_G1_VERDICT.md`

## 1. 범위와 금지선

이번 변경은 테스트 코드 두 파일뿐이다.

- `tests/test_ack_unit_mismatch.py`
- `tests/test_protection_observability.py`

`bot/`·`scripts/` 운영 코드 diff는 0이다. 발주 clamp, baseline denylist,
kill-switch, 동결, F1·F2·F3·F4 운영 로직을 바꾸지 않았다. 기본 브랜치 병합,
Oracle 배포, 운영 ACK apply, kill/env 변경도 하지 않았다.

## 2. H1 — BUY 픽스처가 SELL 게이트까지 도달하도록 수정

`_row()`에 테스트 전용 `side` 인자를 추가했고 기본값은 기존과 같은 `SELL`이다.
`_zero_fill_evidence()`도 같은 기본값을 전달하므로 기존 호출의 의미는 변하지
않는다. G2 테스트만 주문·ccnl 정규화 행을 모두 `BUY`로 맞췄다.

테스트는 다음을 함께 단언한다.

- `plan["side"] == "BUY"`
- `plan["exact_odno_matches"] == 1`
- `kind == "hold"`, `resolvable == false`, `zero_fill_proof == false`

따라서 ODNO/side 불일치로 먼저 걸러지는 기존 공허한 단언이 아니라 실제
`operator-zero-fill` SELL 제한을 밟는다.

### 독립 mutation 실측

`scripts/kis_ack_resolve.py`에서 아래 변경을 실제 주입했다.

```diff
- if (side == "SELL" and not terminal_review
+ if (not terminal_review
```

- `git diff --quiet -- scripts/kis_ack_resolve.py` → **rc=1**(주입 확인)
- `python -m py_compile scripts/kis_ack_resolve.py` → **rc=0**
- `test_operator_zero_fill_branch_is_sell_only` → **rc=1**
- 실패 단언: `plan["kind"] == "hold" and not plan["resolvable"]`
- 역패치 후 운영 파일 diff → **rc=0**

## 3. H2 — 갭 서명 변경 시 연속 카운터 리셋 고정

신규 `test_f4_signature_change_resets_counter_before_accumulating`은 같은 INGR에서
`11:1:0`을 한 번 본 뒤 `11:3:0`으로 서명을 바꾼다. 두 번째 관찰 뒤 래치 파일의
상태가 `{"signature": "11:3:0", "count": 1}`이고 알림이 0건임을 확인한다.
그 다음 같은 `11:3:0`을 한 번 더 관찰해야 비로소 알림 1건이 발생한다.

이 테스트는 갭 해소로 항목을 삭제하는 기존 테스트와 달리, 갭은 계속 존재하지만
서명이 바뀌어 카운터가 **새 1회차로 교체**되는 상태를 파일 내용으로 구분한다.

### 독립 mutation 실측

```diff
- count = (old_count + 1 if old_signature == signature else 1)
+ count = old_count + 1
```

- 대상 운영 파일 diff 주입 확인 → **rc=1**
- `python -m py_compile bot/protection_observability.py` → **rc=0**
- `test_f4_signature_change_resets_counter_before_accumulating` → **rc=1**
- 실패 위치: 바뀐 서명의 두 번째 전체 관찰에서 조기 경보 발생
- 역패치 후 운영 파일 diff → **rc=0**

## 4. H3 — 양 시장 장외 F4 호출 자체 0건 고정

신규 `test_f4_closed_markets_do_not_call_audit`은 KRW·USD
`settings.market_open()`을 모두 `False`로 주입한다. `kis_positions.load`와
`audit_sellable_gaps`를 정상 반환 spy로 두고 각각 **0회**임을 단언한다.
예외를 주입하지 않았기 때문에 `maybe_audit_sellable_gaps` 내부 `except`가 호출을
삼켜 테스트를 우연히 통과시키는 구조가 아니다.

### 독립 mutation 실측

```diff
  if not scope_markets:
-     return False
+     scope_markets = {"US"}
```

- 대상 운영 파일 diff 주입 확인 → **rc=1**
- `python -m py_compile bot/ops_status.py` → **rc=0**
- `test_f4_closed_markets_do_not_call_audit` → **rc=1**
- 실패 단언: `load.call_count == 0`
- 역패치 후 운영 파일 diff → **rc=0**

## 5. 세 mutation 동시 독립성

세 mutation을 동시에 주입하고 세 운영 파일이 모두 `git diff --name-only`에
나오는지 확인했다. 세 파일 `py_compile`은 모두 rc=0이었다. 이후 각 전용 테스트를
별도 프로세스로 실행한 결과 H1·H2·H3가 각각 **rc=1**이었다. 즉 한 테스트의
초기 실패가 나머지 회귀를 대신 가리는 구조가 아니다. 세 역패치를 적용한 뒤
`git diff --quiet -- bot scripts`는 rc=0이었다.

## 6. 전체 검증

- `python -m tests.test_ack_unit_mismatch` → rc=0
- `python -m tests.test_protection_observability` → rc=0
- `python -m tests.run_all` → **ALL PASS: Python test modules 74**
- Node 24.15 `node tests/site_math.test.js` → **19/19**
- `python -m compileall -q bot scanner scripts tests` → rc=0
- Node 24.15 `node --check scanner/site_app/app.js` → rc=0
- `git diff --check` → rc=0
- `git diff --quiet -- bot scripts` → rc=0

## 7. 재검토 요청

다음 다섯 가지를 반증해 달라.

1. H1이 `exact_odno_matches == 1`을 실제로 단언하고 BUY 행으로 SELL 게이트에
   도달하는가.
2. `_row(side=...)`와 `_zero_fill_evidence(side=...)`의 기본값 때문에 기존
   SELL 테스트 의미가 바뀐 곳이 없는가.
3. H2가 서명 변경에 따른 `count=1` 교체와 갭 해소에 따른 항목 삭제를 구분하는가.
4. H3 spy가 예외 경로에 삼켜지지 않고 양 시장 장외의 실제 호출 0회를 증명하는가.
5. 세 mutation을 동시에 주입해도 각기 다른 전용 테스트가 독립적으로 실패하는가.

판정은 P0~P3로 부탁한다. 기본 브랜치 병합·Oracle 배포는 사용자 별도 승인 후다.
