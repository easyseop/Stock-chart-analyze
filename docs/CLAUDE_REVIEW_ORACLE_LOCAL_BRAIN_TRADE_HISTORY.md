# Claude 검토 요청 — 매수 거래이력 + GitHub Actions 독립 차선

작성일: 2026-07-25
기준: `claude/happy-gauss-cwoq21` `97f4662`
작업 브랜치: `codex/oracle-local-brain-trade-history`
상태: 1차 검토 P1 1건·P2 2건 수정 후 재검토 요청, commit/push/merge/Oracle 배포 전
운영 상태: KIS mock, kill-switch L1 유지

## 검토 판정 요청

아래 변경을 P0/P1/P2/P3으로 검토해 주세요. 특히 “정상 경로가 된다”보다 반례와
장애주입을 우선해 주세요. P0/P1이 하나라도 있으면 병합·Oracle 그림자 배포를
차단해 주세요.

이번 변경은 두 문제를 함께 다룹니다.

1. 개인 웹 거래이력이 매도만 보여 실제 확정 매수 체결이 보이지 않던 문제
2. GitHub cron·watchdog·Cloudflare가 모두 결국 같은 GitHub Actions 실행기에
   의존해, Actions 지연 시 세 경로가 함께 늦어지는 구조

## 변경 1 — 실제 매수·매도 거래이력

관련 파일:

- `bot/trade_history.py`
- `scanner/site_app/app.js`
- `scanner/site_app/app.css`
- `tests/test_trade_history.py`
- `tests/test_site_app.py`
- `tests/site_preview.py`

의도:

- 주문 `ack`는 체결이 아니므로 거래이력에 절대 표시하지 않는다.
- `kis_accounting`이 브로커 체결 확인 뒤 남긴 `buy_fill`만 매수로 표시한다.
- 부분매수는 누적수량이 아니라 이번에 새로 체결된 delta 수량으로 각각 표시한다.
- 공통 `event_id` 중복은 한 번만 표시한다.
- 매수마다 실제 체결가·수량·체결금액·체결 후 평단가·체결 후 보유수량을 표시한다.
- 매도는 기존처럼 직전 평단·실제 매도가·실현손익·수익률·잔량을 표시한다.
- 주문번호, 내부 원장키, pos_key, 계좌번호, 시크릿, 파일경로는 내보내지 않는다.
- 과거 `buy_fill` 원장이 없는 주문은 ACK/현재 잔고로 추정해 만들지 않는다.

반드시 확인할 반례:

1. ACK 후 브로커 미체결인데 매수 이력이 생기는가?
2. 4주 부분체결 후 총 10주 완전체결이면 4주+6주인가, 4주+10주로 이중계상되는가?
3. 회계 직전/직후 크래시로 같은 event_id가 반복돼도 한 줄인가?
4. 기존 `open` 이벤트와 새 `buy_fill`이 함께 있을 때 평단·수량이 이중계상되는가?
5. 같은 종목 A/B 포지션이 동시에 있을 때 pos_key가 응답으로 새거나 서로 섞이는가?
6. costbook 또는 position journal 일부 손상 시 “확정”으로 오표시되는가?
7. 주문 원장 손상 시 전체 숨김(fail-closed)이 유지되는가?
8. 사용자가 입력할 수 있는 종목명·사유가 DOM XSS로 이어지는가?

알려진 의도적 제약:

- 원장 도입 전 과거 매수는 정확한 체결가·부분체결 근거가 없으므로 소급 생성하지
  않는다. 누락처럼 보여도 임의 추정보다 안전을 택한 것이다.

## 변경 2 — GitHub Actions와 다른 Oracle 분석 실행기

관련 파일:

- `scanner/oracle_brain.py`
- `scanner/cache.py`
- `bot/signal_feed.py`
- `bot/kis_buyloop.py`
- `infra/server/oracle-brain.service`
- `infra/server/oracle-brain.timer`
- `infra/server/oracle-brain.oracle-ubuntu.conf`
- `infra/server/buyloop.service`
- `infra/server/autodeploy.sh`
- `infra/server/health_beacon.sh`
- `infra/server/watchdog.oracle-ubuntu.conf`
- `tests/test_oracle_brain.py`
- `tests/test_signal_feed.py`
- `tests/test_runtime_cadence.py`
- `docs/ORACLE_LOCAL_BRAIN_DESIGN.md`

의도:

- GitHub는 5,400종목 전체 스캔과 Pages 생성을 계속 담당한다.
- Oracle은 최근 후보 최대 40개와 매회 4개 순환 종목만 5분 간격으로 분석하되,
  순환·고정 관찰군은 24시간 이내 유효한 후보 basis가 있을 때만 보완한다.
- Oracle 분석기는 KIS env를 받지 않고 KIS·주문·원장을 import하지 않는다.
- 신호 선택기는 두 피드를 합치지 않고 한 소스만 선택한다.
- GitHub 20분 이내면 항상 GitHub를 쓴다.
- GitHub가 20분보다 늦고 Oracle이 12분 이내여도
  `ORACLE_SIGNAL_FALLBACK_ENABLED=1`을 명시하기 전에는 Oracle을 주문 입력으로
  쓰지 않는다. 저장소 기본값은 0이다.
- GitHub 45분 초과 + Oracle 비활성/노후/손상은 신호 0건으로 신규매수 차단이다.
- Oracle 후보 기준이 24시간보다 낡으면 무효다.
- 출력은 파일+디렉터리 fsync 후 원자 교체하고 단일 flock으로 실행 중복을 막는다.
- systemd timer는 완료 뒤 5분이라 실행이 겹치지 않는다.
- systemd `StateDirectory`·`CacheDirectory`가 깨끗한 호스트에서도 쓰기 경로를
  먼저 만들며 저장소 홈 경로에는 쓰지 않는다.
- 1GB VM에서 `MemoryMax=420M`, `MemoryHigh=360M`, 낮은 CPU 우선순위를 둔다.
- sentinel 손절은 이 선택기와 무관하게 계속 독립 실행한다.

반드시 확인할 반례:

1. fallback=0인데 GitHub 45분 초과 시 로컬 신호로 매수하는가?
2. 미래 시각, timezone 없는 시각, JSON 절반쓰기, 잘못된 contract가 통과하는가?
3. 같은 code가 `now`와 `shelf` 양쪽에 있을 때 또는 완전 중복일 때 판정이 안전한가?
4. GitHub 복구 뒤 로컬에서 GitHub로 자동 원복할 때 동일종목 이중 주문 가능성이
   있는가?
5. 두 소스를 union하지 않아도 기존 ledger의 in-flight·멱등키가 유지되는가?
6. Oracle 출력 시각만 최신이고 후보 basis가 24시간 넘었는데 통과하는가?
7. 원격 피드가 없을 때 순환 탐색/고정 관찰군이 basis를 부당하게 신선화하는가?
8. 분석기 OOM·timeout·권한 오류가 sentinel/buyloop를 죽이거나 KIS 유량을
   소비하는가?
9. `ProtectHome=read-only`에서 `StateDirectory`·`CacheDirectory`가 실제
   Ubuntu systemd의 깨끗한 호스트에서도 226/NAMESPACE 없이 만들어지는가?
10. 표준 `/opt/stock` 기본 유닛과 실제 Oracle
    `/home/ubuntu/Stock-chart-analyze` drop-in이 정확히 결합되는가?
11. autodeploy import smoke에 분석기를 추가한 것이 선택 기능 미설치 상태에서
    기존 매매 서비스 배포를 과도하게 막는가?
12. watchdog 미설치를 더는 health beacon이 정상으로 숨길 수 없는가?

## 로컬 검증 결과

- 전체 독립 Python 회귀: `44/44` 모듈 통과
- 거래이력: ACK 제외, 부분매수 4+6, event_id 중복, 평단, 매도 손익, 원장손상
  fail-closed 통과
- 신호 선택: GitHub 우선, 명시적 전환, 양쪽 노후, fallback=0, 미래시각,
  중복·계약 오류 통과
- Oracle 분석기: 1시간 원격장애 후보 유지, 25시간 만료, 장외 강제점검,
  원자 출력·권한·주문 import 부재 통과
- 1차 검토에서 발견된 “후보 basis 만료 뒤 discovery/watch가 실행시각을 새 basis로
  만드는 경로”를 제거했다. 25시간 만료 basis + discovery + configured-watch를
  동시에 주입해 시세조회·출력 전에 차단되는 회귀 테스트를 추가했다.
- `ReadWritePaths` 사전존재 의존을 없애고 systemd 관리 State/Cache 디렉터리로
  바꿨으며, 실제 Oracle 경로는 drop-in으로 분리했다.
- health beacon은 `BEACON_UNITS ∪ BEACON_REQUIRED_UNITS`를 중복 없이 순회한다.
  필수 watchdog를 사용자 목록에서 빼도 실제 감시집합에 복원되는 실행 테스트를
  추가했다.
- 같은 pos_key의 legacy `open` 뒤 확정 `buy_fill`은 ACK-era open 수량을
  메타데이터로만 취급해 이중계상하지 않는다. 동일 종목 A/B lot이 둘 이상인데
  pos_key 없는 sell_fill은 임의 귀속하지 않고 표시를 생략한다.
- Oracle 분석기의 소스 문자열뿐 아니라 깨끗한 하위 프로세스의 실제 전이 import
  graph에도 KIS·원장·사이징·손절 모듈이 없음을 검사한다.
- JavaScript `node --check` 통과
- shell `bash -n` 통과
- `git diff --check` 통과

첫 전체 회귀에서는 테스트용 최소 선택 응답에 선택 사유가 없을 때 로그 출력이
`KeyError`를 낸 1건이 있었다. 로그 필드를 선택값으로 바꾼 뒤 해당 전용 테스트와
전체 `44/44`를 처음부터 다시 통과했다.

## 검토 뒤 단계

승인 전에는 commit/push/merge/Oracle 변경을 하지 않는다.

승인되더라도 바로 주문 입력으로 쓰지 않는다.

1. 별도 커밋·PR
2. Oracle에서 누락된 `watchdog.service`부터 복구
3. `oracle-brain.timer` 설치, fallback=0 그림자 운전
4. 한국장 1회 + 미국장 1회 실행시간·RSS·신호차이 관찰
5. GitHub 60분 장애주입, L1에서 신규주문 0 확인
6. 외부 재승인 뒤 fallback=1 고려
7. L1 해제는 별도 사용자 승인
