# Claude 적대 재검토 판정 — CVNA zero-fill·forensic 복구·총위험 원자 게이트

검토일: 2026-08-19 · 대상: `codex/riskcap-cvna-recovery` @ `cdd165dd` (base `a09c8b2`)
요청서: Codex 반례표(§1~§6) · 방법: 독립 프로브 + 전체 회귀 + 뮤테이션(하나씩)

## 판정: **P0 0 · P1 0 · P2 1 · P3 2**

- **병합: 가능** (P2-1은 테스트 공백 — 비차단. 병합 전 처리 권장, 후속도 무방)
- **Oracle 코드 배포: 가능** (아래 배포 노트 준수 — L1 유지 상태·열린 주문 0 확인)
- **CVNA apply: 가능** (런북 전제 전부 충족 + 사용자 별도 승인 후)

## 핵심 검증 — CVNA 원문 사건이 재발하지 않는가

검토자 독립 프로브(임시 원장 + 모의 KIS, Codex 테스트와 별개) **10/10 성립**:

| # | 시나리오 | 결과 |
|---|---|---|
| A1 | ACK 98초 뒤 ccnl 0체결 행(원문 재현) | **종결 없음** — state=submitted 유지 ✅ |
| A2 | 유예 경과 + 잔고 +74 | **종결 0 · 모순 보고 1건**(자동 귀속 없음) ✅ |
| A3 | 지연 체결행(74) 도착 | filled=74 확정 + 회계 호출 1회 ✅ |
| B1 | 599초 | 종결 금지 ✅ |
| B2 | 601초 + 잔고 불변 + 0체결 행 | `zero-fill-balance-proof` 종결 ✅ |
| B3 | env 30·90 주입 | 하한 **600 유지**(reload 실측) ✅ |
| C1-2 | 위험 8% + 예약 1% + 신규 1% | 첫 주문 통과 · **두 번째 경계 차단** ✅ |
| C3 | 포지션 위험 계량 불가(None) | 제출 차단 ✅ |

원문 시퀀스(ACK → 98초 ccnl 0체결 → 실제 74주)가 이제 **어느 단계에서도 예약
해제·오판 종결로 이어지지 않고**, 최종적으로 74주 회계에 도달함을 확인했다.

## 반례표 판정 (요청서 §1~§5)

| 절 | 판정 | 근거 |
|---|---|---|
| §1 T2a zero-fill (6) | **HOLDS** | 프로브 A/B + 뮤테이션 M1·M3·M4 KILLED + sell R1~R5 회귀 PASS |
| §2 T2b forensic (8) | **HOLDS*** | 크래시 주입 스위트 PASS · M9(exact cost) KILLED · 락 순서 단방향(ledger→costbook/kpos, 역방향 호출 없음·flock 중 네트워크 없음) · *P2-1 예외 |
| §3 T2c adopt (5) | **HOLDS** | M7(순서 역전) KILLED · 원자쓰기 0600+dirsync 확인 |
| §4 T1 원자 게이트 (7) | **HOLDS** | 프로브 C + M5(예약 제외)·M6(경계 완화) KILLED · meta 덮어쓰기 불가(spread 순서 코드 확인) |
| §5 이력·섀도 (4) | **HOLDS** | 기존 검토(6708f7bc) + NaN/inf 신규 테스트 PASS |

## 뮤테이션 — 9회 중 8 KILLED · **1 생존(P2-1)**

M1 유예하한 제거·M2 잔고모순 무시·M3 0체결증명 제거·M4 원버그 재도입·
M5 원자합산 예약 제외·M6 경계 완화·M7 adopt 순서 역전·M9 exact cost 파생값
→ 전부 KILLED. 정직 기록: M2 1차 시도는 패턴 불일치로 **무위 치환**이었고
(거짓 생존), 실제 코드 문구(`unchanged = current == before`)로 재적용해 KILLED
확인했다.

### P2-1. `accounting_recovery_pending` 예약 유지에 테스트가 없다

`ledger._buy_reservation_costs`의 pending 분기를 통째로 꺼도(M8) 전 스위트가
통과한다 — `grep accounting_recovery_pending tests/` **0건**. 기능 자체는 검토자
프로브로 정상 실측(rejected 후 예약 0원 → pending 마킹 시 **6,641,190원 부활**).
이 분기는 apply 도중 크래시로 pending이 남은 창에서 예산 이중사용을 막는
안전선인데, 회귀 방어가 없다. **최소 수정: 테스트 1건**(rejected+pending 상태
에서 예약 합산 유지 + pending 해제 후 소멸).

## P3 (비차단)

1. **배포 노트**: stop 메타 없는 legacy 미체결 BUY가 배포 시점에 남아 있으면
   위험 역산이 None → 전 매수 차단(fail-closed 방향). 현재 열린 주문 0이라
   무해 — 배포 직전 `/진단`으로 열린 주문 0 재확인만 하면 된다.
2. `test_ownership_baseline`은 저장소가 tempdir 아래면 환경성 실패(검토자
   /tmp 워크트리 실측). 코드 결함 아님 — CI·서버 경로에서는 통과(69/69 확인).

## 회귀

본 경로 워크트리에서 `tests.run_all` **69모듈 ALL PASS**(exit 0) · 집중 10모듈
개별 PASS · compileall OK. Node 스위트는 Codex 증거(19/19)를 수용한다.

## 승인 절차(변경 없음)

병합 → 장외 Oracle 코드 배포(L1 유지) → 사용자 CVNA 표 재승인 → plan 신규
생성 → SHA ack·서비스 정지·신규 backup-dir로 apply → 세 원장 대조 → 서비스
복구 → readiness 별도.

---

## 부분 재검토 — P2-1 반영 (2026-08-20, `5202fb7`)

**P2-1 해소 — 최종 P0 0 · P1 0 · P2 0 · P3 2(배포 노트뿐).**

- 신규 테스트가 5단계 전이(평시 0원 → pending 시 6,641,190.384원 부활 →
  1원 BUY 차단 → 완료 시 0원 → 1원 BUY 통과)를 고정함을 확인.
- 검토자 뮤테이션 재주입(pending 분기 단독 비활성) → **KILLED** 재확인.
- diff는 테스트+문서 3파일 137줄 추가뿐 — 코드 동작 변경 0(diff 실측).
- 집중 5모듈 재실행 전부 PASS.

**병합 가능 — 사용자 승인 대기.** 배포·apply 절차는 본문 그대로.
