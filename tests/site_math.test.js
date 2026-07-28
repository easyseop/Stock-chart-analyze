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
