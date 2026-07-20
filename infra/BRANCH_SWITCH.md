# 기본 브랜치 교체 가이드 — `korean-text-review` → `happy-gauss`

두뇌(스캐너·워크플로·cf-worker)가 낡은 기본 브랜치에서 돌아, 최신 코드(국내 스캔·
알림 게이팅·KIS 작업)가 반영 안 되는 구조 문제를 **한 번에** 푼다.

## 이 교체로 동시 해결
- 국내 스캔 부활(스케줄 + `kr` dispatch 모드 + 워커 국내 발사)
- `[관찰] 매수 제안` 알림 스팸 해소(게이팅 코드 반영)
- 최신 KIS 코드가 두뇌에 반영 · 이후 내 수정이 두뇌에도 자동 반영

## 안전성(검증됨)
- 두 브랜치 갈라짐 `1:118` — happy-gauss가 라이브 두뇌의 상위집합.
  기본 브랜치에만 있던 1개(`bb24cf8`, toss)는 **이미 happy-gauss에 반영됨**(무손실).
- 서버 봇은 **`state` 브랜치** feed를 읽음 → 기본 브랜치 교체와 무관, 안 끊김.
- 대시보드 코드는 118커밋(전부 bot/ KIS 작업)에서 미변경 → 회귀 없음.
- 롤백: Settings에서 기본 브랜치 되돌리면 즉시 원복, 무손실.

## Phase 0 — 사전 준비 (완료 ✅, 코드에 반영됨)
- `daily.yml`: `workflow_dispatch`에 `kr` 모드 추가(+공회전 방지 guard)
- `worker.js`: 국내 장중엔 `fast` 대신 `kr` 발사(국내 신호까지 생성)

## Phase 1 — 브랜치 교체 (GitHub 웹, 1분)
1. **Settings → General → Default branch**(연필 아이콘) → `claude/happy-gauss-cwoq21` → Update
2. **Actions 탭** → 워크플로 목록에서 `daily-scan`·`buy-sell-advisor` 등이
   "This workflow was disabled" 상태면 각각 **Enable workflow**

## Phase 2 — 인프라 재배포 (필수 — 안 하면 옛 브랜치로 계속 dispatch)
3. **cf-worker**: `BRANCH` 환경변수(현재 `claude/korean-text-review-o3wmsv`)를
   `claude/happy-gauss-cwoq21`로 변경 후 재배포
   - wrangler: `wrangler.toml`의 `[vars] BRANCH` 수정 → `wrangler deploy`
   - 또는 Cloudflare 대시보드 → Worker → Settings → Variables에서 수정
4. **cron-job.org**: 요청 바디에 `ref`/branch가 박혀 있으면 happy-gauss로 변경
   (dispatch API는 `?ref=`가 아니라 body의 `ref` 필드 — 워커와 동일)

## Phase 3 — 검증
5. `full` 스캔 1회 수동 실행(Actions → daily-scan → Run workflow → update=full,
   confirm=FULL) → 국내 303종목 캐시 시딩
6. 확인:
   - 국내 신호: 서버에서 `python -c "from bot import kis_buyloop as bl; print(len([s for s in bl._fetch_signals() if s.get('ccy')=='KRW']))"` → 0 아니면 성공
   - `[관찰]` 알림 멈췄나 · 대시보드 렌더 정상인가 · 서버 봇 feed 정상인가(state라 원래 무관)

## 롤백
Settings에서 기본 브랜치를 `korean-text-review-o3wmsv`로 되돌리고, 워커 `BRANCH`도
원복. 무손실·즉시.
