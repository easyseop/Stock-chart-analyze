# KIS 스캐너 직접진입 V5 — Codex V4 재검토(falsy tactic·stop>=entry) 반영

- 직전 검토 대상: `a4b2d8e1` (Codex 판정: 병합 차단 — P1 2건)
- 이 수정의 코드 커밋: `a5425c1a`
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## P1-1 — falsy 비문자열 전술값의 full 둔갑 (수정)

Codex 재현: `tactic={"mode": []}`(또는 `{}`·`0`·`False`, tactic 자체가
`[]`·`0`·`False`) + `cur=100.4` → `raw or "full"` 정규화가 위반값을 정상
full로 바꿔 실행기 1회·`gate=sent`.

수정(`bot/kis_buyloop.py`): Codex 권장 형태 그대로 — **부재와 명시를 구분**.

- 부재만 legacy full: tactic 필드 없음 · `tactic=None` · dict에 `mode` 키 없음.
- 명시된 값은 타입 검증: dict의 `mode`는 **문자열일 때만** strip/lower 정규화,
  비문자열이면 `gate=tactic`. tactic이 문자열이면 legacy 문자열 전술로 정규화.
  dict·str·None 외 타입(list/int/bool)은 `gate=tactic`.
- 빈 문자열·공백-only는 정규화 후 허용 집합(`full/half/pullback`) 검사에서
  자연 탈락 → `gate=tactic`(full 부활 없음).
- `s.get("tactic") or {}`도 제거 — 명시적 falsy 구조가 정상 dict로 둔갑하던
  같은 계열 경로.

회귀(`test_falsy_tactic_values_do_not_default_to_full`):

- `mode ∈ {[], {}, 0, False, None, "", "   "}` × A/now·B/shelf ×
  `cur=100.4`(tolerance 안 — 종전엔 sent) → 실행기 0회·`gate=tactic`
- tactic 자체 `∈ {[], 0, False}` → 동일 차단
- legacy 호환 확인: tactic 부재 · `tactic={}`(mode 키 없음) · `tactic="full"`
  (문자열 전술) → 종전대로 `sent`
- 기존 truthy 위반(`["x"]`·`{"weird":1}`·중첩 dict)은
  `test_unknown_tactic_never_bypasses_tolerance`가 계속 차단 확인

## P1-2 — stop >= entry의 현재가 의존 통과 (수정)

Codex 재현: `entry=100, stop∈{100, 100.2}` + `cur=100.4`(tolerance 안) →
`per_share = cur - stop > 0` → 실행기 1회·`gate=sent`. 기존 테스트는
`cur=100.0` 고정이라 per_share<=0으로 우연히 막혀 맹점이 됐다.

수정: 공통 input 게이트에 **신호 불변식 `stop >= entry` 차단** 추가 —
실시간 현재가와 무관하게 계약 위반은 주문 0. 사유 문구도
"진입/손절가 무효(NaN·inf·0·음수·역전)"로 갱신. `half`/`pullback`의
`stop < pb < entry` 계약과 마지막 `per_share <= 0` 방어는 그대로 유지.

회귀(`test_stop_at_or_above_entry_blocked_regardless_of_price`):

- `stop ∈ {100, 100.0001, 100.2, 120}` × `cur ∈ {100, 100.1, 100.4, 101.49}`
  × A/now·B/shelf — 전 조합 실행기 0회·`gate=input`
- 유효 경계 `stop=99.9 < entry` → 종전대로 `sent`

## 유지 확인

- V1~V4에서 닫은 수정 전부 무변경 유지: allowlist env-존재-확정 · B
  `fresh is True` · NaN 현재가/목표가/환율/실행기 · 음수 stop 이중방어 ·
  fx None-only 기본값 · unknown tactic 차단 · stop=0 gate=input 진단.
- 변경 파일: `bot/kis_buyloop.py` · `tests/test_kis_buyloop.py` 2개뿐.
  안전 게이트 파일 전부 무변경.

## 검증

```text
tests/test_*.py 49/49 통과 (신규 2건 포함)
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
실제 주문 HTTP 0건(주문 전송 전부 mock · buyloop urlopen 트랩)
```

## Codex V4 재승인 조건 4항 대조

| # | 조건 | 상태 |
|---|---|---|
| 1 | falsy 비문자열 mode의 full 기본화 제거(타입/존재 구분) | 수정 |
| 2 | 현재가 무관 `stop < entry` 불변식 추가 | 수정 |
| 3 | A/B 독립 반례 회귀 테스트 | 7 falsy×A/B + 4stop×4cur×A/B 추가 |
| 4 | 전체 49 + 신규 + compileall + diff check | 통과 |

미해결 P0/P1/P2: 없음.
