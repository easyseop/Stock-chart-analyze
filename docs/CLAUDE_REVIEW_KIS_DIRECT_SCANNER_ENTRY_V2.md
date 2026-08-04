# KIS 스캐너 직접진입 V2 — Codex 재검토(P1 2·P2 1·P3 2) 반영

- 이전 검토 대상: `0fb353f8` (Codex 판정: 병합 차단)
- 수정 커밋: `1e0b3204` (이 문서는 그 위 별도 커밋)
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## P1-1 — 공백 allowlist의 파일 부활 (수정)

- `bot/rollout.py allowed_symbols()`: **env 존재 여부**로 분기한다.
  `"ALLOWED_SYMBOLS" in os.environ`이면 빈 값·공백 값도 그 값이 확정 —
  파싱 결과가 비면 빈 `set()`(전 종목 거부)이고 파일로 폴백하지 않는다.
  파일 폴백은 env 키 자체가 없을 때만.
- 모듈 문서의 우선순위 서술도 같은 계약으로 정정.
- 회귀(요구 4건 전부):
  - `test_allowlist_file_fallback_and_env_precedence`(확장) — 파일에 심볼이
    있어도 env `""`·`"   "`·`" , ,"` → `set()` + `check_new_entry` 전 종목
    거부, env 제거 시에만 파일 폴백 복귀.
  - `test_empty_env_allowlist_blocks_even_with_stale_file`(신규,
    tests/test_l1_readiness.py) — `collect_runtime`과 같은 수집식으로 빈/공백
    env + 파일 `AAPL` → 수집 결과 `set()`이고 l0/strict 모두
    `limited_l0_fence` blocker.

## P1-2 — B 슬리브의 stale 행 통과 (수정)

- `bot/kis_buyloop.py _shelf_cands()`: `s.get("fresh") is True` 요구.
  `_now_signals()`도 truthy에서 `is True`로 통일(문자열 등 비-bool 거부).
- 프로덕션 영향 확인: `scanner/screener.py:549`에서 `group="shelf"` 행은
  항상 `"fresh": True`로 발행된다(`fresh: False`는 화면 전용
  `shelf_watch`뿐) — 이 수정은 B를 끄지 않고 stale/이물 행만 차단한다.
- 회귀(요구 3건 전부): `test_b_stale_row_in_fresh_document_is_rejected` —
  신선 문서 안 `fresh=False` 행·`fresh` 필드 없는 행은 실행기 호출 0회,
  `fresh=True` 행만 도달. `tests/test_shelf.py` 필터 픽스처에도 stale 행
  반례 추가.

## P2 — NaN 경계 (수정)

- `bot/kis_buyloop.py`:
  - 현재가: `float()` 정규화 + `math.isfinite` + 양수 — NaN/inf 시세는
    quote 게이트에서 예외 없이 skip(사이클 중단 없음).
  - 환율: run_once 진입 시 `isfinite(fx) and fx > 0` 아니면 전 후보
    fail-closed(`gate="input"`).
  - 목표가: finite·양수일 때만 `order_meta["target"]`에 저장, 아니면 None —
    NaN이 원장·`kis_positions`로 전파돼 목표가 청산(NaN 비교=False)을
    끄는 경로 차단.
- `bot/kis_buy.py execute_entry`: 호출부와 **독립**으로
  `price_usd`/`per_share_risk_usd`/`fx` isfinite 검사를 input 게이트에
  추가(이중방어, 게이트 순서·기존 동작 무변경).
- 회귀: `test_nan_quote_is_gated_without_crash`(NaN·inf),
  `test_nan_target_is_not_persisted`(NaN→None·유효값 보존),
  `test_nan_fx_fails_closed_for_all_candidates`,
  `test_execute_entry_rejects_nonfinite_inputs`(실행기 자체 차단 4케이스).

## P3-1 — 문서 정정 (수정)

- `bot/rollout.py` 상단 Stage 표: mirror 항목을 "KIS 스캐너 직접진입
  (legacy alias) · allowlist **필수**"로 정정, 공통 강제 항의 allowlist
  서술에 env-존재-확정 계약 명시.
- `bot/kis_buyloop.py run_once` docstring "미러 매수 시도" → "신선한 스캐너
  신호를 KIS 시세·게이트로 직접 집행 시도". 테스트 모듈 첫 줄·요약 문구도
  동일 정정.

## P3-2 — 보고 표현 (수용)

- 이전 보고의 "전 테스트 오프라인"은 부정확했다. 정확한 주장으로 제한한다:
  **실제 주문 HTTP 0건**(주문 전송 전부 mock)·buyloop 경로 urlopen 트랩.
  일부 테스트가 가짜 키로 KIS 토큰 발급을 시도하고 URLError로 복귀하는
  것은 사실이며, 인증·조회 호출까지 트랩하는 완전 오프라인화는 별도
  테스트-인프라 정비 항목으로 남긴다(주문 안전성과 무관).

## 검증

```text
tests/test_*.py 49/49 통과 (신규/확장 테스트 7건 포함)
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
```

변경 파일: `bot/rollout.py` · `bot/kis_buyloop.py` · `bot/kis_buy.py` ·
`tests/test_kis_buyloop.py` · `tests/test_kis_buy_gates.py` ·
`tests/test_l1_readiness.py` · `tests/test_shelf.py`.
`kis_orders`·`ledger`·`envelope`·`ownership`·`kis_boot`·`kis_accounting`·
`kis_positions`·`signal_feed`·`kis_exits`는 계속 무변경. `kis_buy.py`의
유일한 변경은 위 이중방어 input 게이트 추가(게이트 약화 없음 — 강화만).

## 재검토 승인 조건 대조 (Codex 7항)

| # | 조건 | 상태 |
|---|---|---|
| 1 | 빈·공백 env가 파일 폴백 없이 rollout/readiness 모두 차단 | 수정+테스트 |
| 2 | B도 fresh=True만 실행기 전달 | 수정+테스트 |
| 3 | NaN 현재가 → 예외 없이 주문 0 | 수정+테스트 |
| 4 | NaN 목표가 → 원장·포지션 메타 미전파 | 수정+테스트 |
| 5 | 위 반례 회귀 테스트 | 7건 추가 |
| 6 | rollout·buyloop 미러 서술 정정 | 수정 |
| 7 | 전체 49모듈+신규 통과 | 49/49 |

미해결 P0/P1/P2: 없음. P3 잔여: 완전 오프라인 테스트 인프라(§P3-2),
기존 후속 cleanup 목록(`TRADE_STAGE` rename 등)은 별도 PR 유지.
