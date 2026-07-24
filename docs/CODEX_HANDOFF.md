# Codex 개발 인수인계

마지막 갱신: 2026-07-24  
저장소: `easyseop/Stock-chart-analyze`

이 문서는 다른 노트북이나 새 Codex 작업에서 개발을 바로 이어가기 위한 현재 상태,
검증 결과, 미완료 작업과 운영 주의사항을 기록한다. API 키·계좌번호·토큰·SSH
개인키 등 비밀값은 이 문서와 Git에 절대 기록하지 않는다.

## 1. 현재 Git 상태

- 기본 브랜치: `claude/happy-gauss-cwoq21`
- 현재 문서 마감 브랜치: `codex/final-handoff`
- 통합 PR: [#72 오라클 KIS 대시보드와 실매매 안정성 보강](https://github.com/easyseop/Stock-chart-analyze/pull/72)
- PR #72 병합 커밋: `b88eee1`
- 중복 Draft PR #70: 통합 확인 후 닫음
- 배포-매매 분리 PR: [#73 코드 push 배포와 모의매매 분리](https://github.com/easyseop/Stock-chart-analyze/pull/73)
- PR #73 병합 커밋: `bba4341`
- 통합된 주요 커밋:
  - `be0dd7a` — Oracle 보유자산 서비스와 조용한 알림 정책
  - `f2dc1ed` — 주문·체결·대사·일일손실 안전장치 통합
  - `bfc8997` — 장중 폴링 주기와 지표 합의 게이트 보강
  - `fff297d` — 코드 push 배포를 읽기 전용으로 분리
  - `a8d3ac5` — Oracle 배포를 SSH 정보가 있는 컴퓨터로 인수인계

기본 브랜치에는 과거 `codex/trading-safety-large-5`의 안전성 개선 커밋과
Oracle 자산 대시보드, 코드 push 무거래 보정이 PR #72와 #73을 통해 통합돼 있다.

## 2. 완료된 개발

### 공개 웹앱

- PR #71 병합 및 GitHub Pages 배포 완료.
- 공개 주소: <https://easyseop.github.io/Stock-chart-analyze/app/>
- 기존 `/api/signals.json`, `/api/paper_auto.json`, `/api/track.json` 계약은 유지.
- 공개 사이트에는 KIS 계좌·보유종목·키·토큰을 발행하지 않음.
- 검색, KR/US 필터, 정렬, 다크 모드, 모바일 내비게이션, 로딩/빈 상태/오류/신선도
  경고 구현.

### Oracle KIS 보유자산 화면

- `bot/portfolio_web.py`: 주문 모듈을 불러오지 않는 GET/HEAD 전용 서버.
- `infra/server/portfolio-web.service`: Oracle Ubuntu용 systemd 서비스.
- 서버는 코드 수준에서 `127.0.0.1:8765`에만 바인딩.
- 기존 `/etc/stock/kis.env`를 이용해 KIS `mock` 또는 `live` 환경의 국내·미국
  보유종목을 조회.
- 표시 필드: 종목, 보유수량, 평단, KIS 잔고 기준 현재가, 평가금액, 손익, 손익률.
- 기본 15초 갱신. 같은 주기 안의 요청은 캐시해 파수꾼·매수루프와 KIS 호출 경합 방지.
- 계좌번호, App Key/Secret, 토큰은 JSON과 로그에 포함하지 않음.

### 매매 운영 안정성

- 매수할 종목이 있어도 주문가능 현금이 부족하면 매수하지 않는 fail-closed 처리.
- 일일 실현손실 한도 도달 시 신규매수 영속 차단.
- 주문 접수와 체결을 분리하고, ACK/부분체결/UNKNOWN을 잔고·체결내역으로 대사.
- 미확정 주문이 있으면 중복 주문 차단, 확인된 잔여 수량만 재주문.
- 반반/눌림 지정가 주문의 대기·취소·만료 수명주기 구현.
- 손절 주문 chase, 취소 확인, 가격 하한, 초과매도 방지 구현.
- 체결 확인 후에만 원가장부와 보호 포지션 생성.
- 매수 신호 확인 60초, 파수꾼 시세 확인 20초, 보유자산 화면 15초.
- 각 주기는 환경변수로 조정 가능하며 너무 짧거나 긴 값은 코드에서 제한.

### 지표와 전략 연결

- 점수 모듈 8개: 추세/다중 TF, 상대강도, 52주 신고가, 시장방향, 거래량,
  지지저항, RSI, 추세선.
- ADX는 시장 국면과 국면별 가중치에 사용.
- ATR은 손절선·위험금액·수량 계산에 사용.
- 매물대/POC/VAH/VAL은 전략 B 진입·목표·손절에 사용.
- 8개 방향성 점수 중 6개 이상이 동시에 약세이면 `now` 신규 진입 거부.
- 검증 전 패턴 품질 지표는 기록·분석용으로 유지해 과최적화를 방지.

### 알림

- `NOTIFY_MODE=trade_only` 지원.
- 실제 매매, 사용자 요청 조회, 치명 안전 경보만 전송.
- 매수 제안, 성과 리포트, 일상 운영 성공 알림은 억제.
- 치명 경보는 어떤 알림 모드에서도 유지하며 ntfy 이중화 가능.

## 3. 검증 결과

로컬에서 아래 검증이 모두 통과했다.

```bash
python3 -m compileall -q bot scanner tests

for test in tests/test_*.py; do
  module="${test%.py}"
  module="${module//\//.}"
  python3 -m "$module"
done

node --check scanner/site_app/app.js
git diff --check
```

검증 범위에는 매수 현금, 일일손실, 중복주문, 부분체결, UNKNOWN 대사, 국내/미국
주문 라우팅, 손절 chase, 지표 매핑, 알림 필터, 공개/개인 웹 안전 경계가 포함된다.

## 4. CI 테스트 격리 보정

PR #72에서 발견된 자동매매 회귀 테스트의 운영 상태 격리 누락을 보정했다.

GitHub Actions에서는 `GITHUB_ACTIONS=true`이므로 테스트가 실제 `state` 브랜치의
`autopaper.snapshot.json`을 복구해 테스트용 빈 계좌를 오염시킬 수 있었다.
`fastsafe`, `killswitch`, `phase0`, `pos_cap`, `trail` 테스트의 `_fresh()`
초기화에서 운영 스냅샷 복구를 끈다. CI 분산 매매 락도 검증 대상이 아닌
`phase0`, `pos_cap`, `trail`에서는 명시적으로 `off`로 고정한다.

```python
def _fresh(tmp: str) -> None:
    ...
    ap._state_branch_snapshot = lambda: None
    ap._trading_lock_status = lambda run_id: "off"
```

이 수정은 테스트에서만 운영 스냅샷 복구를 끄며 실제 자동매매 복구 로직은 변경하지
않는다. 로컬 검증도 `GITHUB_ACTIONS=true` 조건으로 실행해 CI 환경을 재현한다.

## 5. 코드 push 배포와 매매 분리

기본 브랜치에 PR을 병합하면 `daily-scan`의 push 실행이 사이트를 재배포한다. 이전에는
`MODE=none`이어도 스크리너 생성 과정에서 모의계좌를 갱신하고 텔레그램 시크릿을
전달해 코드 배포만으로 모의 체결 알림이 발생할 수 있었다.

후속 보정에서는 push 실행에 `AUTOPAPER_READ_ONLY=1`을 적용한다.

- 모의계좌 신규진입·청산·대기주문을 실행하지 않음
- 매매 분산 락과 `state` 브랜치 백업에 접근하지 않음
- 텔레그램·ntfy 자격증명을 스크리너에 전달하지 않음
- 공개 화면을 위한 읽기 전용 `paper_auto.json` 스냅샷만 생성
- 정기·수동 데이터/매매 실행과 빌드·배포 실패 경보는 기존대로 유지

PR #73은 전체 CI 통과 후 병합됐다. 병합으로 시작된
[daily-scan 실행 #30074425686](https://github.com/easyseop/Stock-chart-analyze/actions/runs/30074425686)은
빌드, Pages 배포, GitHub 호스팅 스모크 테스트까지 모두 성공했다. 원격 로그에서도
아래 문구를 확인했다.

```text
[autopaper] 코드 배포 전용 — 매매·상태 저장·알림 생략(표시 스냅샷만 생성)
```

이 실행에서는 `state` 브랜치 계좌 백업 단계도 건너뛰었다.

## 6. 다른 노트북에서 이어가기

GitHub CLI 인증 후 아래 순서로 시작한다.

```bash
gh auth status
git clone https://github.com/easyseop/Stock-chart-analyze.git
cd Stock-chart-analyze
git fetch --all --prune
git switch claude/happy-gauss-cwoq21
git pull --ff-only origin claude/happy-gauss-cwoq21
python3 -m pip install -r requirements.txt
```

작업 시작 전:

```bash
git status --short --branch
gh pr list
gh run list --limit 10
```

다른 노트북의 Codex에 전달할 문장:

> `docs/CODEX_HANDOFF.md`를 먼저 읽고, Oracle 서버 배포와 실계정 조회 검증부터
> 이어서 진행해줘. 완료 단위마다 별도 `codex/` 브랜치에 커밋·푸시하고 이
> 인수인계서도 갱신해줘.

## 7. Oracle 배포 — 다른 컴퓨터에서 진행

사용자 요청에 따라 Oracle 서버 설치와 KIS 실계정 조회 검증은 SSH 정보가 저장된
다른 컴퓨터에서 이어서 진행한다. 현재 컴퓨터에서는 의도적으로 배포하지 않았다.
개인키 내용이나 비밀번호를 채팅·Git에 붙이지 말고 다음 정보만 사용한다.

- SSH 대상: `ubuntu@공인IP` 또는 SSH 별칭
- 로컬 개인키 경로: 예) `/Users/.../oracle.key`

PR #72 병합 후 서버에서:

```bash
cd /opt/stock/Stock-chart-analyze
sudo -u bot git fetch origin
sudo -u bot git pull --ff-only

sudo install -o root -g root -m 644 \
  infra/server/portfolio-web.service \
  /etc/systemd/system/portfolio-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now portfolio-web.service
sudo systemctl status portfolio-web --no-pager
```

`/etc/stock/kis.env`에는 비밀값과 함께 다음 운영값을 서버에서만 설정한다.

```dotenv
NOTIFY_MODE=trade_only
PORTFOLIO_REFRESH_SECONDS=15
BUYLOOP_POLL_SECONDS=60
SENTINEL_POLL_SECONDS=20
```

서버 자체 검증:

```bash
curl -fsS http://127.0.0.1:8765/api/portfolio.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["environment"], len(d["positions"]), d["partial"])'
```

사용자 기기에서 SSH 터널:

```bash
ssh -N -L 8765:127.0.0.1:8765 ubuntu@오라클주소
```

터널을 유지한 상태로 <http://127.0.0.1:8765/app/>에 접속한다. OCI 보안 목록이나
Ubuntu 방화벽에서 8765 포트를 공개하지 않는다.

## 8. 공개 사이트 접속 참고

GitHub API 기준 Pages는 public이고 최신 배포와 GitHub 호스팅 스모크 테스트가
성공했다. 배포 artifact에도 다음 파일이 포함된 것을 별도로 확인했다.

- `app/index.html`
- `app/app.js`
- `app/app.css`
- `api/paper_auto.json`
- `api/signals.json`
- `api/track.json`

다만 2026-07-24 현재 작업 중인 Mac 네트워크에서는
`easyseop.github.io:443` 연결이 IPv4/IPv6 모두 타임아웃됐다. 사이트 파일
문제라기보다 해당 네트워크의 GitHub Pages 접근 문제로 확인됐다. 다른
네트워크/모바일 핫스팟으로 확인하거나, 개인 KIS 화면은 Oracle SSH 터널 경로를
사용한다.

## 9. 다른 컴퓨터에서 남은 순서

현재 컴퓨터에서 할 수 있는 코드 개발, 테스트, PR 병합, 공개 Pages 배포 확인은
완료됐다. 다음 작업만 Oracle 정보가 저장된 컴퓨터에서 진행한다.

1. Oracle SSH 정보 확인 후 `portfolio-web.service` 설치.
2. KIS 모의계좌 실제 보유종목 수·평단·현재가·손익 15초 갱신 검증.
3. 작업 완료 단위마다 이 문서 갱신 → 커밋 → `codex/` 브랜치 푸시.
