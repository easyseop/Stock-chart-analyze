"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  positionInvestmentSummary,
  holdingPeriod,
  positionDistances,
  strategyStats,
  concentrationRows,
  maximumDrawdown,
  completeHoldingsValue,
  completeHoldingsSeries,
  monthlyPerformance,
  monthlyTradeStats,
  strategyTradeStats,
  unrealizedSummary,
} = require("../scanner/site_app/portfolio_math.js");

function assertClose(actual, expected, epsilon = 1e-10) {
  assert.ok(Math.abs(actual - expected) <= epsilon,
    `expected ${actual} to be within ${epsilon} of ${expected}`);
}

test("positionDistances keeps stop and target boundary signs correct", () => {
  assert.deepEqual(
    positionDistances({ cur: 100, stop: 100, target: 120 }),
    { stopPct: 0, targetPct: 20, progress: 0 },
  );
  assert.deepEqual(
    positionDistances({ cur: 100, stop: 80, target: 100 }),
    { stopPct: 20, targetPct: 0, progress: 100 },
  );
  const belowStop = positionDistances({ cur: 90, stop: 100, target: 120 });
  assertClose(belowStop.stopPct, -100 / 9);
  assertClose(belowStop.targetPct, 100 / 3);
  assert.equal(belowStop.progress, 0);
});

test("positionDistances treats missing or zero plans as unavailable", () => {
  assert.deepEqual(
    positionDistances({ cur: 100, stop: 0, target: 0 }),
    { stopPct: null, targetPct: null, progress: null },
  );
  assert.deepEqual(
    positionDistances({ cur: 0, stop: 80, target: 120 }),
    { stopPct: null, targetPct: null, progress: null },
  );
});

test("positionInvestmentSummary prefers broker totals and keeps exact prices", () => {
  const summary = positionInvestmentSummary({
    qty: 8, avg: 42.5, cur: 45, buy_amt: 341, eval_amt: 362,
    pl_amt: 21, pl_rt: 6.158,
  });
  assert.deepEqual({ ...summary, priceGapPct: null }, {
    quantity: 8,
    averagePrice: 42.5,
    currentPrice: 45,
    investedAmount: 341,
    currentValue: 362,
    pnlAmount: 21,
    returnPct: 6.158,
    perSharePnl: 2.5,
    priceGapPct: null,
    breakEvenMovePct: 0,
  });
  assertClose(summary.priceGapPct, 45 / 42.5 * 100 - 100);
});

test("positionInvestmentSummary derives safe fallbacks and true break-even move", () => {
  const summary = positionInvestmentSummary({
    qty: 4, avg: 100, cur: 80,
  });
  assert.equal(summary.investedAmount, 400);
  assert.equal(summary.currentValue, 320);
  assert.equal(summary.pnlAmount, -80);
  assert.equal(summary.returnPct, -20);
  assert.equal(summary.perSharePnl, -20);
  assertClose(summary.priceGapPct, -20);
  assert.equal(summary.breakEvenMovePct, 25);
  const missing = positionInvestmentSummary({
    qty: 0, avg: 0, cur: "", buy_amt: 0, eval_amt: 0,
  });
  assert.equal(missing.averagePrice, null);
  assert.equal(missing.currentPrice, null);
  assert.equal(missing.investedAmount, null);
  assert.equal(missing.currentValue, null);
  assert.equal(missing.pnlAmount, null);
  assert.equal(missing.returnPct, null);
});

test("holdingPeriod validates dates, rejects future dates, and counts entry day", () => {
  assert.deepEqual(holdingPeriod("2026-07-25", "2026-07-25"), {
    opened: "2026-07-25", holdingDays: 1,
  });
  assert.deepEqual(holdingPeriod("2026-07-23", "2026-07-25"), {
    opened: "2026-07-23", holdingDays: 3,
  });
  assert.equal(holdingPeriod("", "2026-07-25"), null);
  assert.equal(holdingPeriod("2026-02-30", "2026-07-25"), null);
  assert.equal(holdingPeriod("2026-07-26", "2026-07-25"), null);
  assert.equal(holdingPeriod("<script>", "2026-07-25"), null);
});

test("maximumDrawdown needs two samples and uses the zero-return baseline", () => {
  assert.equal(maximumDrawdown([]), null);
  assert.equal(maximumDrawdown([{ account: 4 }]), null);
  assertClose(maximumDrawdown([{ account: 0 }, { account: 10 }, { account: 5 }]),
    -100 / 22);
  assertClose(maximumDrawdown([{ account: 5 }, { account: -5 }]),
    -200 / 21);
});

test("holdings benchmark is shown only with full same-day coverage", () => {
  assert.deepEqual(completeHoldingsValue({
    account: 1.2, covered: 1, eligible: 16,
  }), {
    value: null, covered: 1, eligible: 16, complete: false,
  });
  assert.deepEqual(completeHoldingsValue({
    account: -0.4, covered: 16, eligible: 16,
  }), {
    value: -0.4, covered: 16, eligible: 16, complete: true,
  });
  assert.equal(completeHoldingsValue({
    account: 3, covered: 0, eligible: 0,
  }).value, null);
});

test("holdings period benchmark requires complete coverage on every day", () => {
  assert.deepEqual(completeHoldingsSeries([
    { account: 1, covered: 16, eligible: 16 },
    { account: 2, covered: 15, eligible: 16 },
  ]), { complete: false, samples: 2, incompleteDays: 1 });
  assert.deepEqual(completeHoldingsSeries([
    { account: 1, covered: 16, eligible: 16 },
    { account: -1, covered: 16, eligible: 16 },
  ]), { complete: true, samples: 2, incompleteDays: 0 });
});

test("strategyStats never combines KRW and USD totals", () => {
  const stats = strategyStats([
    { sleeve: "A", ccy: "USD", buy_amt: 100, pl_amt: 5 },
    { sleeve: "A", ccy: "KRW", buy_amt: 200_000, pl_amt: -10_000 },
    { sleeve: "B", ccy: "USD", buy_amt: 80, pl_amt: 2 },
  ]);
  assert.deepEqual(stats, {
    A: {
      count: 2,
      markets: {
        USD: { buy: 100, pl: 5 },
        KRW: { buy: 200_000, pl: -10_000 },
      },
    },
    B: {
      count: 1,
      markets: {
        USD: { buy: 80, pl: 2 },
      },
    },
  });
});

test("concentrationRows calculates the largest holding inside each currency", () => {
  const rows = concentrationRows([
    { ccy: "USD", code: "AAA", eval_amt: 60 },
    { ccy: "USD", code: "BBB", eval_amt: 40 },
    { ccy: "KRW", code: "111111", eval_amt: 300_000 },
    { ccy: "KRW", code: "222222", eval_amt: 700_000 },
  ]);
  assert.deepEqual(rows, [
    { ccy: "USD", name: "AAA", weight: 60, tone: "warning" },
    { ccy: "KRW", name: "222222", weight: 70, tone: "warning" },
  ]);
});

// ── 성과 vs 지수 null 산술·복리 (Codex TWR-V4 P1-1·P1-2·P2-3 반례) ──────

const {
  todayIndexDiff,
  cumulativeAlphaSeries,
  dailyIndexValue,
  incompleteCount,
} = require("../scanner/site_app/portfolio_math.js");

test("todayIndexDiff never treats a null index as zero", () => {
  assert.equal(todayIndexDiff(1.25, null), null);       // V4 P1-1 재현 입력
  assert.equal(todayIndexDiff(null, 1.0), null);
  assert.equal(todayIndexDiff(1.25, 0.5), 0.75);
  assert.equal(todayIndexDiff(0, 0), 0);
  assert.equal(todayIndexDiff(undefined, 2), null);
  assert.equal(todayIndexDiff("1.25", "0.25"), 1);      // 문자열 숫자는 허용
});

test("cumulativeAlphaSeries compounds account and index separately", () => {
  const alpha = (acct, idx) => cumulativeAlphaSeries(
    acct.map((a, i) => ({ acct: a, idx: idx[i] })));
  assert.deepEqual(alpha([10, 10], [0, 0]), [10, 21]);        // +21.00%p
  assert.deepEqual(alpha([10, -10], [0, 0]), [10, -1]);       // -1.00%p (본전 아님)
  assert.deepEqual(alpha([10, 10], [10, -10]), [0, 22]);      // +22.00%p
});

test("cumulativeAlphaSeries breaks the segment at any missing day", () => {
  // 지수 +2, null, +3 — 결측을 건너뛰어 +5.06%로 잇는 것 금지.
  const out = cumulativeAlphaSeries([
    { acct: 1, idx: 2 }, { acct: 1, idx: null }, { acct: 1, idx: 3 },
  ]);
  assert.equal(out[0] !== null, true);
  assert.deepEqual(out.slice(1), [null, null]);          // 결측 이후 전부 미확정
  const accountMissing = cumulativeAlphaSeries([
    { acct: null, idx: 1 }, { acct: 2, idx: 1 },
  ]);
  assert.deepEqual(accountMissing, [null, null]);
});

test("dailyIndexValue keeps explicit nulls instead of session fallback", () => {
  const newRow = { daily_indices: { 나스닥: null }, indices: { 나스닥: 1.0 } };
  assert.equal(dailyIndexValue(newRow, "나스닥"), null);  // V4 P1-2: 세탁 금지
  const present = { daily_indices: { 나스닥: -0.4 }, indices: { 나스닥: 9 } };
  assert.equal(dailyIndexValue(present, "나스닥"), -0.4);
  // 새 스키마 행에서 키가 빠진 지수도 세션 값으로 폴백하지 않는다.
  const missingName = { daily_indices: { "S&P500": 0.2 }, indices: { 나스닥: 2 } };
  assert.equal(dailyIndexValue(missingName, "나스닥"), null);
  // daily_indices 키 자체가 없는 구버전 행만 세션 값 폴백 허용.
  const legacy = { indices: { 나스닥: 0.7 } };
  assert.equal(dailyIndexValue(legacy, "나스닥"), 0.7);
  assert.equal(dailyIndexValue({}, "나스닥"), null);
});

test("incompleteCount counts null/undefined/non-finite as missing", () => {
  const rows = [{ v: 1 }, { v: null }, {}, { v: "x" }, { v: 0 }];
  assert.equal(incompleteCount(rows, "v"), 3);
});

test("monthlyPerformance compounds each month and never bridges a missing value", () => {
  const rows = monthlyPerformance([
    { date: "2026-07-01", account: 10, A: 2, B: 1,
      daily_indices: { NASDAQ: 5 } },
    { date: "2026-07-02", account: 10, A: -2, B: null,
      daily_indices: { NASDAQ: 5 } },
    { date: "2026-08-03", account: -10, A: 1, B: 3,
      daily_indices: { NASDAQ: null } },
  ], ["NASDAQ"]);
  assert.equal(rows.length, 2);
  assertClose(rows[0].account, 21);
  assertClose(rows[0].A, -0.04);
  assert.equal(rows[0].B, null);
  assertClose(rows[0].indices.NASDAQ, 10.25);
  assert.equal(rows[1].indices.NASDAQ, null);
});

test("monthlyTradeStats uses sell month cost basis and exposes estimated samples", () => {
  const rows = monthlyTradeStats([
    { side: "sell", market: "US", day: "2026-07-25",
      realized_pnl_krw: 60, cost_closed_krw: 1000, price_estimated: true },
    { side: "sell", market: "US", day: "2026-07-28",
      realized_pnl_krw: -20, cost_closed_krw: 1000, price_estimated: false },
    { side: "buy", market: "US", day: "2026-07-28" },
    { side: "sell", market: "KR", day: "2026-07-28",
      realized_pnl_krw: 999, cost_closed_krw: 1 },
  ], "US");
  assert.deepEqual(rows, [{
    month: "2026-07", exits: 2, wins: 1, losses: 1, winRate: 50,
    realizedPnlKrw: 40, costClosedKrw: 2000, realizedReturnPct: 2,
    estimatedCount: 1, complete: true,
  }]);
});

test("strategyTradeStats keeps a one-trade B win visibly small", () => {
  assert.deepEqual(strategyTradeStats([{
    side: "sell", market: "US", sleeve: "B", realized_pnl_krw: 96_606.9,
    price_estimated: true,
  }], "B", "US"), {
    exits: 1, wins: 1, losses: 0, winRate: 100,
    estimatedCount: 1, realizedPnlKrw: 96_606.9,
  });
});

test("unrealizedSummary never adds an incomplete broker snapshot", () => {
  assert.deepEqual(unrealizedSummary([
    { ccy: "USD", buy_amt: 1000, pl_amt: 50 },
    { ccy: "USD", buy_amt: 500, pl_amt: -10 },
    { ccy: "KRW", buy_amt: 100_000, pl_amt: 1_000 },
  ], "US"), { count: 2, invested: 1500, pnl: 40,
    returnPct: 40 / 1500 * 100, complete: true });
  assert.equal(unrealizedSummary([
    { ccy: "USD", buy_amt: 1000, pl_amt: null },
  ], "US").complete, false);
});
