"use strict";

const API = Object.freeze({
  signals: "../api/signals.json",
  paper: "../api/paper_auto.json",
  track: "../api/track.json",
  portfolio: "../api/portfolio.json",
  chart: "../api/chart.json",
});

const VIEW_META = Object.freeze({
  now: { eyebrow: "MARKET SIGNALS", title: "오늘의 진입 후보", group: "now" },
  watch: { eyebrow: "WATCHLIST", title: "조금 더 지켜볼 종목", group: "watch" },
  shelf: { eyebrow: "STRATEGY B", title: "매물대 반등 후보", group: "shelf" },
  portfolio: { eyebrow: "PRIVATE · READ ONLY", title: "내 자산" },
  performance: { eyebrow: "FORWARD TEST", title: "모의투자 성과" },
});

const state = {
  view: "now",
  market: "all",
  query: "",
  sort: "stage",
  signalsDoc: null,
  paper: null,
  track: null,
  portfolio: null,
  publicError: null,
  portfolioError: null,
  loading: true,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const content = $("#content");
const dialog = $("#detail-dialog");

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function finite(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatPrice(value, ccy = "USD", signed = false) {
  const number = finite(value);
  const sign = signed ? (number >= 0 ? "+" : "−") : (number < 0 ? "−" : "");
  const amount = Math.abs(number);
  if (ccy === "KRW") return `${sign}${Math.round(amount).toLocaleString("ko-KR")}원`;
  return `${sign}$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPercent(value, digits = 1) {
  const number = finite(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function marketLabel(ccy) {
  return ccy === "KRW" ? "KR · 원화" : "US · 달러";
}

function groupSignals(group) {
  return (state.signalsDoc?.signals || []).filter((signal) => signal.group === group);
}

function relativeMinutes(iso) {
  const timestamp = Date.parse(iso || "");
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
}

async function fetchJSON(url, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const separator = url.includes("?") ? "&" : "?";
  try {
    const response = await fetch(`${url}${separator}v=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadData({ quiet = false } = {}) {
  if (!quiet) {
    state.loading = true;
    renderLoading();
  }
  const refresh = $("#refresh-button");
  refresh.classList.add("spinning");

  const publicResults = await Promise.allSettled([
    fetchJSON(API.signals),
    fetchJSON(API.paper),
    fetchJSON(API.track),
  ]);
  if (publicResults[0].status === "fulfilled") {
    state.signalsDoc = publicResults[0].value;
    state.publicError = null;
    const note = state.signalsDoc?.note;
    if (note) $("#disclaimer").textContent = note;
  } else {
    state.publicError = publicResults[0].reason;
  }
  if (publicResults[1].status === "fulfilled") state.paper = publicResults[1].value;
  if (publicResults[2].status === "fulfilled") state.track = publicResults[2].value;

  try {
    state.portfolio = await fetchJSON(API.portfolio, 10000);
    state.portfolioError = null;
  } catch (error) {
    state.portfolio = null;
    state.portfolioError = error;
  }

  state.loading = false;
  refresh.classList.remove("spinning");
  updateFreshness();
  render();
}

function updateFreshness() {
  const freshness = $("#freshness");
  const alert = $("#data-alert");
  alert.className = "alert hidden";

  if (!state.signalsDoc) {
    freshness.className = "freshness error";
    freshness.textContent = "불러오기 실패";
    alert.className = "alert error";
    alert.textContent = "최신 신호를 가져오지 못했습니다. 이전 값을 조용히 보여주지 않고 화면을 중단했습니다.";
    return;
  }
  const minutes = relativeMinutes(state.signalsDoc.generated_at);
  if (minutes === null) {
    freshness.className = "freshness stale";
    freshness.textContent = "갱신 시각 미확인";
    alert.className = "alert";
    alert.textContent = "데이터 생성 시각을 확인할 수 없습니다. 주문 판단에는 사용하지 마세요.";
    return;
  }
  if (minutes >= 15) {
    freshness.className = "freshness stale";
    freshness.textContent = `${minutes}분 전 · 오래됨`;
    alert.className = "alert";
    alert.textContent = `마지막 신호가 ${minutes}분 전에 생성됐습니다. 화면의 가격과 현재 시장 가격이 다를 수 있습니다.`;
  } else {
    freshness.className = "freshness fresh";
    freshness.textContent = minutes === 0 ? "방금 갱신" : `${minutes}분 전 갱신`;
  }
}

function setView(view, { updateHash = true } = {}) {
  if (!VIEW_META[view]) return;
  state.view = view;
  if (updateHash) history.replaceState(null, "", `#${view}`);
  $$(".nav-item, .bottom-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $("#page-eyebrow").textContent = VIEW_META[view].eyebrow;
  $("#page-title").textContent = VIEW_META[view].title;
  $("#signal-toolbar").classList.toggle("hidden", !VIEW_META[view].group);
  render();
}

function updateHero() {
  const hero = $("#hero");
  const number = $("#hero-number");
  const unit = $("#hero-unit");
  const kicker = $("#hero-kicker");
  const description = $("#hero-description");
  hero.classList.remove("hidden");

  if (VIEW_META[state.view].group) {
    const items = groupSignals(VIEW_META[state.view].group);
    number.textContent = items.length.toLocaleString("ko-KR");
    unit.textContent = "종목";
    kicker.textContent = state.view === "now" ? "오늘 포착된 흐름" :
      state.view === "watch" ? "다음 전환을 기다리는 흐름" : "매물대 반등 전략";
    description.textContent = state.view === "now"
      ? "매매 봇과 똑같은 신호 한 벌을 읽기 전용으로 보여드려요."
      : state.view === "watch"
        ? "관찰 목록은 매수 지시가 아닙니다. 전환이 확인될 때까지 기다리는 종목이에요."
        : "전략 B로 분류된 매물대 반등 후보를 원본 값 그대로 표시합니다.";
  } else if (state.view === "portfolio") {
    const positions = state.portfolio?.positions || [];
    number.textContent = positions.length.toLocaleString("ko-KR");
    unit.textContent = "보유";
    kicker.textContent = state.portfolio ? "KIS 계좌 · 읽기 전용" : "내 컴퓨터에서만";
    description.textContent = state.portfolio
      ? "주문 기능과 분리된 조회 전용 연결입니다. 정보는 이 브라우저 밖으로 공개되지 않아요."
      : "실제 보유 정보는 공개 사이트에 표시하지 않습니다. 로컬 대시보드를 실행하면 이곳에서 확인할 수 있어요.";
  } else {
    const returnValue = finite(state.paper?.ret_pct);
    number.textContent = formatPercent(returnValue);
    unit.textContent = "";
    kicker.textContent = "모의투자 누적 수익률";
    description.textContent = "실제 계좌가 아닌 전략 검증용 모의투자 성과입니다.";
  }
}

function filteredSignals() {
  const group = VIEW_META[state.view].group;
  let items = groupSignals(group);
  if (state.market !== "all") items = items.filter((item) => item.ccy === state.market);
  if (state.query) {
    const query = state.query.toLowerCase();
    items = items.filter((item) =>
      String(item.name || "").toLowerCase().includes(query) ||
      String(item.code || "").toLowerCase().includes(query));
  }
  const sorters = {
    stage: (a, b) => finite(b.stage) - finite(a.stage) || finite(b.norm) - finite(a.norm),
    fresh: (a, b) => Number(Boolean(b.fresh)) - Number(Boolean(a.fresh)) || finite(b.stage) - finite(a.stage),
    range: (a, b) => finite(a.range_pos, 1) - finite(b.range_pos, 1),
    name: (a, b) => String(a.name).localeCompare(String(b.name), "ko"),
  };
  return [...items].sort(sorters[state.sort] || sorters.stage);
}

function stageBars(stage) {
  const current = Math.max(0, Math.min(4, Math.round(finite(stage))));
  return Array.from({ length: 4 }, (_, index) => `<i class="${index < current ? "on" : ""}"></i>`).join("");
}

function signalCard(signal) {
  const tactic = signal.tactic || {};
  const badges = [
    signal.fresh ? `<span class="badge new">NEW</span>` : "",
    tactic.label ? `<span class="badge tactic">${escapeHTML(tactic.label)}</span>` : "",
    signal.group === "shelf" ? `<span class="badge shelf">전략 B</span>` : "",
  ].join("");
  return `
    <button class="signal-card" type="button" data-signal-id="${escapeHTML(signal.id)}">
      <span class="card-head">
        <span>
          <span class="ticker">${escapeHTML(signal.code)} · ${marketLabel(signal.ccy)}</span>
          <span class="stock-name">${escapeHTML(signal.name)}</span>
        </span>
        <span class="badge-row">${badges}</span>
      </span>
      <span class="price">${formatPrice(signal.price, signal.ccy)}</span>
      <span class="metric-row">
        <span class="metric"><small>진입</small><b>${formatPrice(signal.entry, signal.ccy)}</b></span>
        <span class="metric stop"><small>손절</small><b>${formatPrice(signal.stop, signal.ccy)}</b></span>
        <span class="metric target"><small>목표</small><b>${formatPrice(signal.target, signal.ccy)}</b></span>
      </span>
      <span class="stage-line">
        <small>단계 ${Math.round(finite(signal.stage))}/4</small>
        <span class="stage-track">${stageBars(signal.stage)}</span>
      </span>
    </button>`;
}

function renderSignals() {
  if (state.publicError || !state.signalsDoc) {
    content.innerHTML = errorState("신호를 불러오지 못했어요", "공개 데이터 연결을 확인한 뒤 다시 시도해 주세요.");
    return;
  }
  const items = filteredSignals();
  if (!items.length) {
    const hasGroup = groupSignals(VIEW_META[state.view].group).length > 0;
    content.innerHTML = emptyState(
      hasGroup ? "검색 조건에 맞는 종목이 없어요" : "오늘은 해당 신호가 없어요",
      hasGroup ? "필터나 검색어를 바꾸면 다른 종목을 볼 수 있어요." :
        "신호가 0건인 날도 정상입니다. 조건을 낮추거나 임의 종목을 만들지 않습니다.");
    return;
  }
  content.innerHTML = `<div class="signal-grid">${items.map(signalCard).join("")}</div>`;
  $$(".signal-card", content).forEach((card) => {
    card.addEventListener("click", () => {
      const signal = (state.signalsDoc.signals || []).find((item) => item.id === card.dataset.signalId);
      if (signal) openSignalDetail(signal);
    });
  });
}

function emptyState(title, description) {
  return `<div class="empty-state">
    <span class="state-icon">○</span>
    <h2>${escapeHTML(title)}</h2>
    <p>${escapeHTML(description)}</p>
  </div>`;
}

function errorState(title, description) {
  return `<div class="error-state">
    <span class="state-icon">!</span>
    <h2>${escapeHTML(title)}</h2>
    <p>${escapeHTML(description)}</p>
    <button class="retry-button" type="button" data-retry>다시 불러오기</button>
  </div>`;
}

function openSignalDetail(signal) {
  $("#detail-market").textContent = `${marketLabel(signal.ccy)} · ${escapeHTML(signal.code)}`;
  $("#detail-title").textContent = signal.name || signal.code;
  const range = Math.max(0, Math.min(100, finite(signal.range_pos, .5) * 100));
  const tactic = signal.tactic || {};
  const shelf = signal.shelf;
  $("#detail-content").innerHTML = `
    <div class="detail-price">${formatPrice(signal.price, signal.ccy)}</div>
    <div class="detail-grid">
      <div class="detail-box"><small>제안 진입가</small><strong>${formatPrice(signal.entry, signal.ccy)}</strong></div>
      <div class="detail-box"><small>손절가</small><strong class="loss">${formatPrice(signal.stop, signal.ccy)}</strong></div>
      <div class="detail-box"><small>목표가</small><strong class="gain">${formatPrice(signal.target, signal.ccy)}</strong></div>
    </div>
    <div class="tactic-box">
      <strong>${escapeHTML(tactic.label || "진입 전술")}</strong>
      <p>${escapeHTML(tactic.desc || "원본 신호에 전술 설명이 없습니다.")}</p>
    </div>
    <div class="range-box">
      <div class="range-labels"><span>52주 저점</span><span>현재 위치 ${Math.round(range)}%</span><span>52주 고점</span></div>
      <div class="range-track">
        <div class="range-fill"></div>
        <span class="range-dot"></span>
      </div>
    </div>
    ${shelf ? shelfDetail(shelf, signal.ccy) : ""}
  `;
  $(".range-fill", $("#detail-content")).style.width = `${range}%`;
  $(".range-dot", $("#detail-content")).style.left = `${range}%`;
  dialog.showModal();
}

function shelfDetail(shelf, ccy) {
  return `<div class="shelf-box">
    <h3>매물대 정보</h3>
    <div class="shelf-values">
      <div><small>POC</small><b>${formatPrice(shelf.poc, ccy)}</b></div>
      <div><small>VAL</small><b>${formatPrice(shelf.val, ccy)}</b></div>
      <div><small>VAH</small><b>${formatPrice(shelf.vah, ccy)}</b></div>
      <div><small>손익비</small><b>${finite(shelf.rr).toFixed(2)}R</b></div>
    </div>
  </div>`;
}

function portfolioTotals(positions) {
  const totals = {};
  positions.forEach((position) => {
    const row = totals[position.ccy] ||= { eval: 0, buy: 0, pl: 0 };
    row.eval += finite(position.eval_amt);
    row.buy += finite(position.buy_amt);
    row.pl += finite(position.pl_amt);
  });
  return totals;
}

function portfolioCard(position) {
  const tone = finite(position.pl_amt) >= 0 ? "gain" : "loss";
  return `
    <button class="portfolio-card" type="button" data-position="${escapeHTML(position.code)}">
      <span class="card-head">
        <span>
          <span class="ticker">${escapeHTML(position.code)} · ${marketLabel(position.ccy)}</span>
          <span class="stock-name">${escapeHTML(position.name)}</span>
        </span>
        <span class="badge">${escapeHTML(position.sleeve || "A")} 전략</span>
      </span>
      <span class="portfolio-value">
        <strong>${formatPrice(position.eval_amt, position.ccy)}</strong>
        <span class="${tone}">${formatPercent(position.pl_rt)}</span>
      </span>
      <span class="portfolio-meta">
        ${Number(position.qty).toLocaleString("ko-KR")}주 · 평균 ${formatPrice(position.avg, position.ccy)}
        · 손익 ${formatPrice(position.pl_amt, position.ccy, true)}
      </span>
    </button>`;
}

function renderPortfolio() {
  if (!state.portfolio) {
    content.innerHTML = `<div class="local-gate">
      <span class="state-icon">⌂</span>
      <h2>내 자산은 내 컴퓨터에서만 보여요</h2>
      <p>실제 보유 종목과 손익은 공개 사이트로 전송하지 않습니다. 로컬 읽기 전용 대시보드를 실행하면 KIS 조회 정보가 이 화면에만 나타납니다.</p>
    </div>`;
    return;
  }
  const positions = state.portfolio.positions || [];
  if (!positions.length) {
    content.innerHTML = emptyState("현재 보유 종목이 없어요", "연결된 KIS 환경의 보유 잔고가 0건입니다.");
    return;
  }
  const totals = portfolioTotals(positions);
  const summary = Object.entries(totals).map(([ccy, row]) => {
    const rate = row.buy > 0 ? row.pl / row.buy * 100 : 0;
    const tone = row.pl >= 0 ? "gain" : "loss";
    return `<div class="summary-card">
      <small>${ccy === "KRW" ? "한국 주식" : "미국 주식"} 평가금액</small>
      <strong>${formatPrice(row.eval, ccy)}</strong>
      <div class="${tone}">${formatPrice(row.pl, ccy, true)} · ${formatPercent(rate)}</div>
    </div>`;
  }).join("");
  const partial = state.portfolio.partial
    ? `<div class="alert">일부 시장 조회가 지연돼 목록이 완전하지 않을 수 있습니다.</div>` : "";
  content.innerHTML = `
    <div class="private-banner"><span>●</span> ${escapeHTML(state.portfolio.environment || "KIS")} 계좌 · 주문 기능과 분리된 로컬 조회</div>
    ${partial}
    <div class="portfolio-summary">${summary}</div>
    <div class="portfolio-grid">${positions.map(portfolioCard).join("")}</div>`;
  $$(".portfolio-card", content).forEach((card) => {
    card.addEventListener("click", () => {
      const position = positions.find((item) => item.code === card.dataset.position);
      if (position) openPortfolioDetail(position);
    });
  });
}

function openPortfolioDetail(position) {
  $("#detail-market").textContent = `${marketLabel(position.ccy)} · ${position.code}`;
  $("#detail-title").textContent = position.name || position.code;
  const tone = finite(position.pl_amt) >= 0 ? "gain" : "loss";
  $("#detail-content").innerHTML = `
    <div class="detail-price">${formatPrice(position.cur, position.ccy)}</div>
    <div class="detail-grid">
      <div class="detail-box"><small>보유 수량</small><strong>${Number(position.qty).toLocaleString("ko-KR")}주</strong></div>
      <div class="detail-box"><small>평균 단가</small><strong>${formatPrice(position.avg, position.ccy)}</strong></div>
      <div class="detail-box"><small>평가 손익</small><strong class="${tone}">${formatPercent(position.pl_rt)}</strong></div>
    </div>
    <div class="chart-wrap">
      <div class="chart-head"><strong>최근 6개월 가격</strong><span>기존 스캐너 일봉 · 표시 전용</span></div>
      <div class="chart-loading" id="chart-loading">가격 그래프를 불러오고 있어요.</div>
      <canvas id="price-chart" class="hidden" role="img" aria-label="${escapeHTML(position.name)} 최근 가격 그래프"></canvas>
    </div>`;
  dialog.showModal();
  loadChart(position);
}

async function loadChart(position) {
  const loading = $("#chart-loading");
  const canvas = $("#price-chart");
  try {
    const url = `${API.chart}?code=${encodeURIComponent(position.code)}&days=180`;
    const chart = await fetchJSON(url, 30000);
    if (!chart.points?.length) throw new Error("empty chart");
    loading.classList.add("hidden");
    canvas.classList.remove("hidden");
    drawLineChart(canvas, chart.points, position);
  } catch (error) {
    loading.textContent = "가격 이력을 불러오지 못했습니다. 보유·현재가 정보는 정상적으로 유지됩니다.";
  }
}

function drawLineChart(canvas, points, position) {
  canvas._chartData = { points, position };
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(300, rect.width);
  const height = 260;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);

  const css = getComputedStyle(document.documentElement);
  const color = css.getPropertyValue("--blue").trim();
  const grid = css.getPropertyValue("--line").trim();
  const muted = css.getPropertyValue("--muted").trim();
  const values = points.map((point) => finite(point.close)).filter((value) => value > 0);
  const reference = [position.avg, position.stop, position.entry].map(finite).filter((value) => value > 0);
  const min = Math.min(...values, ...reference);
  const max = Math.max(...values, ...reference);
  const span = Math.max(max - min, max * .02, 1);
  const pad = { left: 12, right: 48, top: 16, bottom: 26 };
  const x = (index) => pad.left + index / Math.max(1, values.length - 1) * (width - pad.left - pad.right);
  const y = (value) => pad.top + (max - value) / span * (height - pad.top - pad.bottom);

  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const gy = pad.top + i / 3 * (height - pad.top - pad.bottom);
    ctx.beginPath();
    ctx.moveTo(pad.left, gy);
    ctx.lineTo(width - pad.right, gy);
    ctx.stroke();
  }

  const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
  gradient.addColorStop(0, `${color}45`);
  gradient.addColorStop(1, `${color}00`);
  ctx.beginPath();
  values.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.lineTo(x(values.length - 1), height - pad.bottom);
  ctx.lineTo(x(0), height - pad.bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  values.forEach((value, index) => {
    if (index === 0) ctx.moveTo(x(index), y(value));
    else ctx.lineTo(x(index), y(value));
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  const lines = [
    ["평균", finite(position.avg), css.getPropertyValue("--violet").trim()],
    ["손절", finite(position.stop), css.getPropertyValue("--red").trim()],
  ].filter(([, value]) => value > 0);
  ctx.font = "10px -apple-system, sans-serif";
  lines.forEach(([label, value, lineColor]) => {
    const lineY = y(value);
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = lineColor;
    ctx.beginPath();
    ctx.moveTo(pad.left, lineY);
    ctx.lineTo(width - pad.right, lineY);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = lineColor;
    ctx.fillText(label, width - pad.right + 5, lineY + 3);
  });

  ctx.fillStyle = muted;
  ctx.font = "10px -apple-system, sans-serif";
  ctx.fillText(points[0].date.slice(2), pad.left, height - 7);
  const lastLabel = points.at(-1).date.slice(2);
  ctx.fillText(lastLabel, width - pad.right - ctx.measureText(lastLabel).width, height - 7);
}

function readTrackStat(...keys) {
  const candidates = [state.track?.stats, state.track, state.track?.stats?.all].filter(Boolean);
  for (const source of candidates) {
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null) return source[key];
    }
  }
  return null;
}

function renderPerformance() {
  const paper = state.paper || {};
  const ret = finite(paper.ret_pct);
  const trades = finite(paper.trades);
  const wins = finite(paper.win_trades);
  const winRate = trades > 0 ? wins / trades * 100 : finite(readTrackStat("win_rate", "win_pct"));
  const avgR = finite(readTrackStat("avg_r", "mean_r"));
  const equity = finite(paper.equity);
  const rules = [
    ["완료 거래", `${Math.round(trades).toLocaleString("ko-KR")}건`],
    ["승리 거래", `${Math.round(wins).toLocaleString("ko-KR")}건`],
    ["비중 위반", `${(paper.cap_violations || []).length}건`],
    ["규칙 위반", `${(paper.rule_violations || []).length}건`],
  ];
  content.innerHTML = `
    <div class="performance-grid">
      <div class="performance-card"><small>누적 수익률</small><strong class="${ret >= 0 ? "gain" : "loss"}">${formatPercent(ret)}</strong><p>자동 모의투자 기준</p></div>
      <div class="performance-card"><small>승률</small><strong>${winRate.toFixed(1)}%</strong><p>종료된 거래 기준</p></div>
      <div class="performance-card"><small>평균 R</small><strong>${avgR.toFixed(2)}R</strong><p>포워드 테스트 집계</p></div>
      <div class="performance-card"><small>평가 자산</small><strong>${formatPrice(equity, "KRW")}</strong><p>실제 계좌가 아닌 모의자산</p></div>
      <div class="performance-card"><small>현금</small><strong>${formatPrice(paper.cash, "KRW")}</strong><p>미체결 주문 반영 전후는 원본 기준</p></div>
      <div class="performance-card"><small>업데이트</small><strong>${relativeMinutes(paper.updated) ?? "—"}분 전</strong><p>성과 JSON 발행 시각</p></div>
      <div class="performance-card performance-wide">
        <small>운영 규칙 상태</small>
        <div class="rule-list">${rules.map(([label, value]) => `<div class="rule-row"><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>
      </div>
    </div>`;
}

function renderLoading() {
  content.innerHTML = `<div class="loading-grid" aria-label="데이터 로딩 중">
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
  </div>`;
}

function render() {
  updateHero();
  if (state.loading) {
    renderLoading();
    return;
  }
  if (VIEW_META[state.view].group) renderSignals();
  else if (state.view === "portfolio") renderPortfolio();
  else renderPerformance();
  $("[data-retry]", content)?.addEventListener("click", () => loadData());
}

function initializeControls() {
  $$("[data-view]").forEach((button) =>
    button.addEventListener("click", () => setView(button.dataset.view)));
  $$(".filter-chip").forEach((button) => {
    button.addEventListener("click", () => {
      state.market = button.dataset.market;
      $$(".filter-chip").forEach((item) => item.classList.toggle("active", item === button));
      render();
    });
  });
  $("#search-input").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    render();
  });
  $("#sort-select").addEventListener("change", (event) => {
    state.sort = event.target.value;
    render();
  });
  $("#refresh-button").addEventListener("click", () => loadData());
  $("#theme-button").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("flow-theme", next);
  });
  $("#dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  window.addEventListener("resize", () => {
    const canvas = $("#price-chart:not(.hidden)");
    if (!canvas || !canvas._chartData) return;
    drawLineChart(canvas, canvas._chartData.points, canvas._chartData.position);
  });
}

function boot() {
  const savedTheme = localStorage.getItem("flow-theme");
  if (savedTheme === "dark" || savedTheme === "light") {
    document.documentElement.dataset.theme = savedTheme;
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
  initializeControls();
  const hashView = location.hash.replace("#", "");
  setView(VIEW_META[hashView] ? hashView : "now", { updateHash: false });
  loadData();
  setInterval(() => loadData({ quiet: true }), 60_000);
}

boot();
