/**
 * 매매 차선 발사·검증 워커 (RELIABILITY A2)
 *
 * GitHub schedule과 독립된 외부 계층 — "정시 보장"이 아니라, 5분마다 깨어나
 * ① 장중인지 ② 마지막 실빌드(state 하트비트)가 얼마나 낡았는지 확인하고,
 * 낡았으면 fast 차선을 발사(workflow_dispatch)한 뒤 ③ 런이 '실제 생성'됐는지
 * 검증, 단계별로 텔레그램 경보를 낸다(F1·F2·F7 대응).
 *
 *   나이 < 12분          → 아무것도 안 함(신선)
 *   12~30분              → fast 발사
 *   30~45분              → fast 발사 + ⚠️ P1 경보
 *   45분+                → fast 발사 + 🚨 P0 경보
 *   발사했는데 60초 내 런 미생성 → 🚨 P0 (GitHub 이벤트 계층 이상 — 실측 B6 유형)
 *
 * 설정(대시보드 → Settings):
 *   [Variables]  REPO="easyseop/Stock-chart-analyze"
 *                BRANCH="claude/korean-text-review-o3wmsv"   // 기본 브랜치
 *   [Secrets]    GH_PAT   — fine-grained PAT(Actions RW, 이 repo만) — cron-job과 동일 토큰 재사용 가능
 *                TG_TOKEN — 텔레그램 봇 토큰
 *                TG_CHAT  — 텔레그램 chat id
 *   [Trigger]    Cron: *\/5 * * * *   (5분마다)
 *
 * 사용량: 하루 ~300 요청 — 무료 한도(100,000/일)의 0.3%.
 */

const FRESH_MIN = 12;   // 이 미만이면 no-op(guard의 12분과 동일 — 이중 안전)
const P1_MIN = 30;
const P0_MIN = 45;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
  // 수동 점검용: 브라우저로 워커 URL 열면 현재 판정을 보여준다(발사는 안 함)
  async fetch(req, env) {
    const s = await status(env);
    return new Response(JSON.stringify(s, null, 1),
                        { headers: { "content-type": "application/json" } });
  },
};

function marketOpen(now = new Date()) {
  // KR: 평일 09:00~15:30 KST(=00:00~06:30 UTC, 이 시간대엔 요일도 UTC=KST)
  const dowUtc = now.getUTCDay();
  const mUtc = now.getUTCHours() * 60 + now.getUTCMinutes();
  const kr = dowUtc >= 1 && dowUtc <= 5 && mUtc >= 0 && mUtc <= 390;
  // US: 미 동부시간 평일 09:30~16:00 (서머타임은 타임존 변환이 처리)
  const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const mEt = et.getHours() * 60 + et.getMinutes();
  const us = et.getDay() >= 1 && et.getDay() <= 5 && mEt >= 570 && mEt < 960;
  return { kr, us, open: kr || us };
}

async function heartbeatAgeMin(env) {
  const url = `https://raw.githubusercontent.com/${env.REPO}/state/feed/heartbeat.json?cb=${Date.now()}`;
  const r = await fetch(url, { cf: { cacheTtl: 0 } });
  if (!r.ok) return null;                       // 조회 실패 = 판단 보류(fail-open)
  const hb = await r.json();
  const t = Date.parse(hb.generated_at);
  if (!t) return null;
  return (Date.now() - t) / 60000;
}

async function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.REPO}${path}`, {
    ...init,
    headers: {
      "authorization": `Bearer ${env.GH_PAT}`,
      "accept": "application/vnd.github+json",
      "user-agent": "fastlane-worker",
      ...(init.headers || {}),
    },
  });
}

async function tg(env, text) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return;
  await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML" }),
  }).catch(() => {});
}

async function status(env) {
  const mkt = marketOpen();
  const age = await heartbeatAgeMin(env);
  return { market: mkt, heartbeat_age_min: age === null ? null : Math.round(age) };
}

async function tick(env) {
  const mkt = marketOpen();
  if (!mkt.open) return;                        // 장외 — 아무것도 안 함
  const age = await heartbeatAgeMin(env);
  if (age === null) return;                     // 하트비트 조회 실패 — 다음 틱에
  if (age < FRESH_MIN) return;                  // 신선 — no-op

  // fast 발사 (guard가 한 번 더 신선도를 재확인하므로 중복 발사 무해)
  const d = await gh(env, "/actions/workflows/daily.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: env.BRANCH, inputs: { update: "fast" } }),
  });

  // 발사 검증(F7·B6) — 60초 뒤 dispatch 런이 '실제 생성'됐는지 확인
  let created = null;
  if (d.status === 204) {
    await new Promise((res) => setTimeout(res, 60000));
    const r = await gh(env,
      "/actions/runs?event=workflow_dispatch&per_page=5&created=>" +
      new Date(Date.now() - 5 * 60000).toISOString());
    if (r.ok) {
      const j = await r.json();
      created = (j.workflow_runs || []).length > 0;
    }
  }

  const a = Math.round(age);
  if (d.status !== 204) {
    await tg(env, `🚨 <b>발사 실패</b> — dispatch HTTP ${d.status} (하트비트 ${a}분). PAT 만료/권한 확인 필요.`);
  } else if (created === false) {
    await tg(env, `🚨 <b>런 미생성</b> — dispatch 204인데 60초 내 런이 안 생김 (하트비트 ${a}분). GitHub 이벤트 계층 이상 추정(B6 유형).`);
  } else if (age >= P0_MIN) {
    await tg(env, `🚨 <b>매매 차선 정체 ${a}분</b> — fast 재발사함. 지속되면 GitHub Actions 상태 확인 필요.`);
  } else if (age >= P1_MIN) {
    await tg(env, `⚠️ 매매 차선 ${a}분 정체 — fast 재발사함(자동 복구 중).`);
  }
  // 12~30분 구간은 조용히 발사만(정상 슬롯 보정 — 알림 피로 방지)
}
