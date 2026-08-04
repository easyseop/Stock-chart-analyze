# 적대검토 요청 V2 — 미러 패리티 (Codex P1 3건·P2 3건 대응)

- 기준 브랜치: `claude/happy-gauss-cwoq21`
- 검토 대상 커밋: **`cad209d1`** (직전 검토 기준 `05e60f5a`)
- 직전 검토 문서: `docs/CLAUDE_REVIEW_MIRROR_PARITY.md`
- 범위: **매수 미러 경로만**. 지수/TWR 수정은 `CLAUDE_REVIEW_TWR_QUARANTINE_V2.md`

`P0/P1`이 하나라도 남으면 unrestricted L0를 열지 않는다.

---

## 1. 직전 판정 수용

Codex의 정리가 정확했다.

> 이것은 아직 "autopaper의 실제 진입 이벤트를 미러"하는 기능이 아니라
> "**현재 보유 종목 코드와 일치하는 신호를 허용**"하는 기능이다.

Claude가 직전 문서에서 "autopaper 한도가 **자동으로** 따라온다"고 쓴 것은
거짓이었다. 현재 보유 집합에는 시간축이 없으므로 하루 3건 상한이 전이될 수
없다. 12건 동시 전송 반례가 그대로 재현됐고, 기존 테스트는 `len(bought) <= 12`
라는 느슨한 assertion 때문에 이를 통과시키고 있었다.

이것으로 **Claude가 같은 문장("미러는 autopaper 진입 종목만 산다")을 근거로
울타리를 제거한 것이 두 번 연속 부정확했다.** 첫 번째는 아예 거짓이었고
(`e17c3ef3`), 두 번째는 절반만 참이었다(`05e60f5a`).

---

## 2. 수정 (`cad209d1`)

### P1-1 → 진입일 기준 "이번 세션 진입"만 미러

- `autopaper_entries()`가 보유 코드 집합이 아니라 **진입일(`opened`)이 이번
  세션에 속하는 종목**만 돌려준다.
- 옛 진입은 자동 추격하지 않는다(Codex 권고 그대로).
- 미 정규장 한 세션이 KST 자정을 넘으므로, 자정 이후 같은 세션이 진행 중이면
  그 세션이 시작된 전날 진입도 인정한다(`_mirror_window`).
- 결과: 하루 신규가 autopaper `DAY_ENTRY_MAX`와 **실제로** 일치한다.
  Codex 반례(보유 12·오늘 진입 3)에서 전송 3건으로 확인.

> **한계 명시**: 이것은 여전히 이벤트 ID 소비가 아니라 **날짜 기반 근사**다.
> 같은 날 autopaper가 진입→청산→재진입한 경우, 또는 같은 날짜에 여러 진입
> 이벤트가 있는 경우를 구분하지 못한다. §4-1 참조.

### P1-2 → 발행 시각·신선도 검사

- `scanner/autopaper.py`가 공개 피드에 **timezone 포함 `generated_at`**을
  발행한다(종전 `updated`는 KST 날짜 문자열뿐이라 나이를 못 쟀다).
- 미러는 나이가 `PAPER_FEED_MAX_AGE_MIN`(기본 45분, 15분 주기 + 배포 여유)를
  넘으면 그 소스를 거부하고 다음 소스로 간다. 전부 무효면 `None`(매수 0건).
- 시각 필드 없음·naive 시각·미래 시각도 거부.

> **전환기 주의**: 배포 직후 첫 autopaper 발행 전까지는 `generated_at`이 없어
> **슬리브 A 신규매수가 일시 정지**한다(약 15분). 의도된 fail-closed다.

### P1-3 → readiness 강제 게이트

- 수집에 `mirror_requires_autopaper`를 싣고, **allowlist 없이 여는 경우**
  값이 정확히 `True`가 아니면 `mirror_parity_enforced`로 차단한다.
- 필드 자체가 없는 구버전 수집도 차단(fail-closed).
- allowlist가 있으면 그 울타리가 근거이므로 차단하지 않는다.

> Codex는 "우회 환경변수 제거"를 더 안전한 안으로 제시했다. 제거하지 않고
> readiness 강제를 택한 이유는 장애 시 디버그 수단을 남기기 위함이다.
> **이 판단이 옳은지 검토 바란다.**

### P2-2 → 엄격 파서

`_parse_paper_feed()`를 순수 함수로 분리했다. 루트 비dict · `positions` 비list ·
scalar 행 · `code` 결손 중 하나라도 있으면 **부분 채택 없이 소스 전체를 거부**
한다. 종전에는 `positions`가 list이기만 하면 scalar 행도 매수 권한이 됐다.

### P2-1 → 조용한 차단 방지

미러 피드가 연속 `_MIRROR_FEED_ALERT_AFTER`(기본 10사이클) 무효면 경보하고,
복구되면 해제 경보를 낸다. **kill L1이 5일간 아무도 모르게 매수를 막았던
실패 양상을 새 게이트로 재생산하지 않기 위한 것**이다.

---

## 3. 검증

전체 `49/49` 통과 · `compileall` · `git diff --check` 클린.
Codex 반례를 그대로 테스트로 이식했다.

| 테스트 | 대응 |
|---|---|
| `test_old_holdings_are_not_backfilled_in_one_day` | P1-1 (보유 12·오늘 3 → 전송 3) |
| `test_us_session_across_kst_midnight_keeps_same_session_entries` | P1-1 경계 |
| `test_stale_or_malformed_feed_is_rejected_not_trusted` | P1-2·P2-2 (9종 반례) |
| `test_mirror_feed_outage_alerts_instead_of_silent_block` | P2-1 |
| `test_mirror_parity_must_be_on_when_allowlist_is_absent` | P1-3 |

느슨했던 `len(bought) <= 12` assertion은 정확한 집합 일치로 교체했다.

---

## 4. 남은 격차 — Claude가 스스로 인정하는 부분

1. **이벤트 ID 기반 미러가 아니다.** Codex가 요구한 `entry_events`
   (`event_id`·`entered_at`·`signal_id`·`entry`·`stop`·`tactic`·`generation`)와
   원장의 정확히-한-번 소비는 **구현하지 않았다.** 현재는 날짜 근사이며,
   같은 날 재진입·복수 이벤트를 구분하지 못한다. 이것이 unrestricted L0를
   막을 수준인지 판정 바란다.
2. **진입계획 패리티는 여전히 없다.** KIS는 자신의 신호에서 나온
   `entry`/`stop`/`tactic`으로 산다. autopaper의 계획과 대조하지 않는다.
   피드에 `avg`·`stop`이 있으므로 대조는 가능하나 이번에 넣지 않았다.
3. **청산 패리티 없음.** autopaper가 팔아도 KIS는 자체 규칙(손절·정체청산)으로
   움직인다. Codex P2-3 지적대로 "계좌 전체 미러"라는 표현은 여전히 부정확하다.
4. **성과 화면 분리 미구현.** `autopaper vs KIS-A`, `KIS-B`, `KIS 전체`를
   분리 표시해야 한다는 P2-3 권고를 이번에 반영하지 않았다.
5. **날짜 경계 판정이 `settings.market_open("USD")`에 의존한다.** 그 함수가
   틀리면 전날 진입을 잘못 인정하거나 놓친다. 서머타임·휴장일 처리 검토 요망.
6. **피드 나이 45분이 임의값이다.** autopaper는 15분 주기이나 GitHub Actions
   지연·CDN 캐시를 감안한 값이며 근거는 없다.

---

## 5. 현 시점 권고 (Claude 의견) — 초판에서 수정함

**초판에서는** Codex 최종 권고를 그대로 옮겨 "제한 allowlist 유지 또는 L1
유지"를 적었다. **그 권고를 철회한다.** Codex의 판정은 `05e60f5a` 기준이고,
그 판정의 구체적 근거였던 **12건 일괄 백필은 `cad209d1`에서 닫혔기 때문이다.**
이전 커밋에 대한 판정을 현 커밋에 기계적으로 적용한 것은 Claude의 오류였다.

현 시점 판단:

| 항목 | 성격 | 상태 |
|---|---|---|
| 옛 보유 12건 일괄 매수 | **안전 위험** | 닫힘(오늘 진입만, 하루 ≤3건) |
| 낡은 피드 fail-open | **안전 위험** | 닫힘(신선도·엄격 파서) |
| 패리티 우회 미탐지 | **안전 위험** | 닫힘(readiness 강제 게이트) |
| 이벤트 ID 미사용 | 성과 비교 **정밀도** | 남음 |
| 진입계획·청산 패리티 | 성과 비교 **정밀도** | 남음 |
| 성과 화면 분리 | 표시 | 남음 |

남은 격차는 "autopaper와 KIS 성적을 정밀 비교하기 어렵다"이지 "계좌가
위험하다"가 아니다. 대상은 KIS mock이며 실전은 코드 하드블록으로 계속 막혀
있다. 하루 신규는 autopaper `DAY_ENTRY_MAX`(3건) 이내로 실제 상계된다.

따라서 **현 상태(제한 allowlist 없이, 미러 게이트 활성)로 mock 운용을 계속하며
검토를 받는 것**을 권고한다. 멈춰서 얻는 것(정밀도)보다 첫 체결 관찰로 얻는
것(원장·총시드·대사 실증)이 크고, 잘못될 경우의 비용이 모의 손익뿐이다.

**단, 이 판단 자체가 검토 대상이다.** Claude는 이번 라운드에서 같은 사안에
대해 두 번(`e17c3ef3` 거짓 전제, `05e60f5a` 절반만 참) 잘못 판단했다.
검토자가 §4의 격차 중 **차단 수준인 것이 있다고 보면 그 판정을 따른다.**

## 6. 검토자가 확인해 줄 것

- §4의 6개 격차 각각이 차단 수준인지.
- `_mirror_window`가 서머타임 전환일·미국 휴장일·KR 단독 장중에 오작동하는지.
- `generated_at` 도입 전환기(구 피드)에 매수가 멈추는 것이 허용 가능한지.
- `MIRROR_REQUIRES_AUTOPAPER`를 남긴 판단이 옳은지, 제거해야 하는지.
- 이 변경이 **손절·청산 경로에 영향을 주지 않는지**(신규매수 전용이어야 함).

## 7. 운영 금지선 (변경 없음)

- 실거래 하드블록(`bot/kis_orders.py`) 제거·완화 금지.
- kill-switch 하향 operator ack 필수 유지.
- 환경변수·시크릿·계좌번호를 코드/커밋/로그에 기록 금지.
- 공개 발행물에 금액·계좌·보유수량 노출 금지(퍼센트만).
