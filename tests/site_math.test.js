"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  positionDistances,
  strategyStats,
  concentrationRows,
  maximumDrawdown,
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

test("maximumDrawdown needs two samples and uses the zero-return baseline", () => {
  assert.equal(maximumDrawdown([]), null);
  assert.equal(maximumDrawdown([{ account: 4 }]), null);
  assertClose(maximumDrawdown([{ account: 0 }, { account: 10 }, { account: 5 }]),
    -100 / 22);
  assertClose(maximumDrawdown([{ account: 5 }, { account: -5 }]),
    -200 / 21);
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
