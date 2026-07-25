# Oracle 독립 소형 분석기 설계

작성일: 2026-07-25
상태: 구현·로컬 검증 완료, 외부 검토 전, 병합·push·Oracle 배포 전
운영 게이트: KIS 모의계좌, kill-switch L1 유지

## 1. 문제

현재 예약 실행, GitHub 내부 freshness watchdog, Cloudflare Worker는 발사 주체만
다르다. 셋 모두 마지막에는 같은 GitHub Actions `daily.yml`을 실행하므로 Actions
대기열이 지연되면 함께 지연된다. 이는 계산 실행기의 이중화가 아니라 트리거의
다중화다.

[GitHub 공식 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)도
`schedule` 이벤트가 고부하 때 지연될 수 있고 일부 대기 작업은 드랍될 수 있다고
명시한다. 따라서 분 단위 신선도가 필요한 매수 신호의 유일한 실행기로 사용하지
않는다.

2026-07-25 Oracle 실측:

- x86_64, 2 CPU
- RAM 956MiB, available 약 513MiB
- swap 0
- 루트 디스크 45GiB 중 약 41GiB 가용
- 기존 `data_cache` 3파일, 약 120KiB

이 사양에 5,400종목·약 900MB 웹 렌더를 옮기면 파수꾼과 메모리 경합할 위험이
있다. 전체 스캔과 Pages는 GitHub에 남기고, Oracle에는 주문과 격리한 소형 신호
차선만 둔다.

## 2. 목표 구조

```text
GitHub 전체 스캔 ─┐
                  ├─ 검증된 신호 선택기 ─ KIS buyloop
Oracle 소형 분석 ─┘

KIS 잔고/현재가 ─ sentinel(손절)  ← 신호 선택기와 계속 독립
GitHub 전체 스캔 ─ GitHub Pages   ← 매매 경로와 분리
```

### 역할

- GitHub Actions: 전체 후보 발굴, 무거운 상세페이지 렌더, 공개 Pages
- Oracle `oracle-brain`: 최근 후보 최대 40개 재분석 + 유효한 후보 basis가 있을
  때만 매회 4개 독립 순환 탐색으로 분석 대상을 보완
- `signal_feed`: 신호 두 개를 합치지 않고 하나만 선택
- KIS `buyloop`: 선택된 신호에 기존 잔고·시드·원장·세션·가격 괴리 게이트 적용
- KIS `sentinel`: GitHub/Oracle 신호 장애와 무관하게 기존 포지션 보호

## 3. 신호 선택 규칙

| 조건 | 선택 | 신규매수 |
|---|---|---|
| GitHub ≤20분 | GitHub | 기존 게이트 통과 시 가능 |
| GitHub >20분, Oracle ≤12분, fallback=0 | GitHub(≤45분) | 그림자 비교만 |
| GitHub >20분, Oracle ≤12분, fallback=1 | Oracle | 기존 게이트 통과 시 가능 |
| GitHub ≤45분, Oracle 불가 | GitHub | 가능 |
| GitHub >45분, Oracle >12분/손상 | 없음 | 전면 차단 |
| Oracle 후보 기준 >24시간 | Oracle 무효 | 전면 차단 |

두 소스를 union하지 않는다. 같은 종목을 두 차선에서 중복 제출하는 문제를 원천
차단하고, 원장의 동일종목 in-flight·멱등키 가드는 그대로 2차 방어로 남긴다.

시각이 없거나 파싱 불가, 미래 5분 초과, 신호 배열/종목 형식 오류, 로컬 계약명
불일치도 모두 무효다. 예전처럼 시각 파싱 실패를 허용하지 않는다.

## 4. Oracle 분석기 안전 경계

- `/etc/stock/kis.env`를 systemd에 주입하지 않는다.
- `bot.kis`, `kis_orders`, `kis_buy`, ledger, autopaper를 import하지 않는다.
- 출력은 공개 가능한 종목 신호뿐이며 계좌·수량·잔고가 없다.
- 분석 결과는 임시파일 `fsync` 후 `os.replace`로 원자 교체한다.
- 단일 non-blocking flock으로 수동 실행과 timer 중복을 막는다.
- 5분 간격은 `OnUnitInactiveSec`라 이전 실행이 길어져도 겹치지 않는다.
- `MemoryMax=420M`, `MemoryHigh=360M`, 낮은 CPUWeight/Nice로 파수꾼 우선.
- 장외에는 실행하지 않는다.
- 후보 원격 사본은 24시간까지만 쓴다. 이후에는 순환 탐색·고정 관찰군이 있어도
  새 basis를 만들거나 실행시각으로 신선화하지 않고 출력 자체를 중단한다.
- `StateDirectory=stock-oracle-brain`과
  `CacheDirectory=stock-oracle-brain`을 사용해 깨끗한 호스트에서도 systemd가
  `/var/lib`·`/var/cache` 쓰기 경로를 먼저 만들며, 저장소 홈 경로에는 쓰지 않는다.
- 기본 유닛은 `/opt/stock`·`bot` 표준 구성을 쓰고 실제 Oracle의
  `/home/ubuntu/Stock-chart-analyze`·`.venv` 차이는 별도 drop-in으로만 보정한다.

## 5. 단계 배포

1. 로컬 테스트와 외부 리뷰 완료. 병합 전에는 Oracle 무변경.
2. 누락된 `watchdog.service` 설치·복구. 파수꾼 heartbeat 재시작 테스트.
3. `oracle-brain.timer`만 설치. `ORACLE_SIGNAL_FALLBACK_ENABLED=0` 그림자 운전.
4. 최소 한국장 1회 + 미국장 1회 동안 GitHub/Oracle 판정 차이, 실행시간,
   최대 RSS, 실패율 기록.
5. GitHub 60분 차단 fault injection. Oracle 파일은 갱신되되 L1 때문에 주문은
   발생하지 않는지 확인.
6. 외부 승인 후에만 fallback을 1로 전환. 이 단계도 모의계좌 유지.
7. L1 해제는 별도 사용자 승인과 별도 Go/No-Go다.

## 6. 합격 조건

- 주문접수(ACK)가 체결이력에 표시되지 않음
- 부분매수 누적분이 실제 증가수량만 한 번씩 표시됨
- GitHub 지연 시 로컬 신호 선택, 신선한 GitHub 복귀 시 자동 원복
- 두 소스 손상/노후 시 신규매수 0
- 로컬 분석기 프로세스가 KIS env·주문 원장에 접근 불가
- 분석기 OOM/timeout에도 sentinel·buyloop·portfolio-web 계속 active
- watchdog가 heartbeat 90초 초과 sentinel 재시작, 120초 초과 L1 유지
- 한/미 각 한 장 동안 중복 주문 0, 손절 지연 0

## 7. 이번 범위에서 하지 않는 것

- GitHub Pages를 Oracle로 이전
- 5,400종목 전체를 5분마다 Oracle에서 분석
- fallback 자동 활성화
- L1 해제
- live 계좌 전환
- 기본 브랜치 병합·Oracle 배포
