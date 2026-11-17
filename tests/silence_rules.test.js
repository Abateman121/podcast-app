// Checks for the Shorten Silence rate schedule in the player (spec section 9, silence tests 1 to 4, 6, 7).
// The pure functions are taken from app/static/index.html between the "silence rules" markers.
// A real browser cannot be timed here, so playback is simulated the way the player runs it:
// a 5 ms timer sets the rate, and media time moves at that rate until the next tick.
//
//   docker run --rm -v /path/to/podcast-app:/src:ro node:22-alpine node /src/tests/silence_rules.test.js /src/app/static/index.html
const fs = require('fs');
const html = fs.readFileSync(process.argv[2] || __dirname + '/../app/static/index.html', 'utf8');
const block = html.split('// ---- silence rules')[1].split('// ---- end silence rules ----')[0];
const R = new Function(block.replace(/^[^\n]*\n/, '') +
  '\nreturn {SIL, silenceBoost, silencePlan, silenceRate, wallRemaining, savedSplit, findIv};')();

let passed = 0, failed = 0;
function check(name, ok, detail) {
  if (ok) passed++; else failed++;
  console.log(`[${passed + failed}] ${ok ? 'ok  ' : 'FAIL'} ${name}${ok ? '' : '  -> ' + JSON.stringify(detail)}`);
}

// Tone bursts of 1 s with the gaps from test 1. The 100 ms gap is below the detector minimum, so the
// detector never reports it. It is fed to the plan anyway to show it is left alone.
const GAPS = [0.1, 0.3, 0.6, 1.5];
function layout() { const gaps = []; let t = 1; for (const g of GAPS) { gaps.push([t, t + g]); t += g + 1; } return {gaps, dur: t}; }
const {gaps, dur} = layout();

// Play media time from t0 to t1. Returns wall seconds taken, seconds saved by source, and the rate log.
function simulate(plan, t0, t1, base, opt = {}) {
  const TICK = 0.005; let t = t0, wall = 0, rate = base, on = true;
  const saved = {speed: 0, silence: 0}, log = [];
  while (t < t1) {
    if (opt.toggleAt !== undefined && t >= opt.toggleAt && on) { on = false; opt.toggledWall = wall; }
    if (opt.skips) for (const k of opt.skips) if (t >= k.start && t < k.end) t = k.end;   // skips run first (R-SS-13)
    if (t >= t1) break;
    rate = R.silenceRate(plan, t, base, on);
    log.push([wall, t, rate]);
    const step = Math.min(TICK * rate, t1 - t);
    const s = R.savedSplit(step, base, rate); saved.speed += s.speed; saved.silence += s.silence;
    t += step; wall += step / rate;
  }
  return {wall, saved, log};
}

// The wall time the spec expects for a pause: 80 ms kept at each edge, the middle at the boost (6.3.2).
function specWall(L, base) {
  const b = R.silenceBoost(L, base), mid = L - 2 * R.SIL.KEEP;
  return b <= 1 ? L / base : (2 * R.SIL.KEEP + mid / b) / base;
}

console.log('1. Gaps shorten to the expected wall time');
for (const base of [1, 1.5, 2]) {
  const plan = R.silencePlan(gaps, [], base);
  check(`at ${base}x the 100 ms gap is left alone`, R.findIv(plan, gaps[0][0] + 0.05) === -1);
  for (const [s, e] of gaps.slice(1)) {
    const got = simulate(plan, s, e, base).wall, want = specWall(e - s, base);
    check(`at ${base}x a ${(e - s).toFixed(1)} s gap plays in ${want.toFixed(3)} s (within 20 ms)`, Math.abs(got - want) <= 0.02, {got, want});
  }
}
const plan1 = R.silencePlan(gaps, [], 1);
const one = simulate(plan1, gaps[3][0], gaps[3][1], 1).wall;
check('spec example: a 1.5 s pause at 1.0x keeps at least the 160 ms of edges', one > 0.16 && one < 1.5, one);
check('boost never goes over 4.0x absolute', R.silenceBoost(2, 2) * 2 <= 4 && R.silenceBoost(2, 3) * 3 <= 4.0001);
check('a short pause keeps at least 180 ms', specWall(0.25, 1) >= R.SIL.MIN_PAUSE - 1e-9, specWall(0.25, 1));

console.log('2. No tone burst is touched');
const bursts = [[0, 1]].concat(gaps.map(([, e], i) => [e, e + 1]));
let touched = [];
for (const base of [1, 2]) {
  const plan = R.silencePlan(gaps, [], base);
  for (const [b0, b1] of bursts) for (let t = b0; t <= b1; t += 0.001) if (Math.abs(R.silenceRate(plan, t, base, true) - base) > 1e-9) touched.push([base, t]);
  for (const [s, e] of gaps) for (const t of [s, s + 0.079, e - 0.079, e - 0.0001])
    if (Math.abs(R.silenceRate(plan, t, base, true) - base) > 1e-9) touched.push([base, t]);
}
check('rate is the base speed over every burst and in the first and last 80 ms of every gap', touched.length === 0, touched.slice(0, 5));
const full = simulate(plan1, 0, dur, 1);
check('the whole file is played (media time reaches the end)', full.log.length > 0 && full.log[full.log.length - 1][1] < dur);

console.log('3. Time saved matches the formula');
for (const base of [1, 1.5, 2]) {
  const plan = R.silencePlan(gaps, [], base), run = simulate(plan, 0, dur, base);
  const formula = plan.after[0];   // d/s - d/(s*b) summed over each pause, exact over the ramps
  check(`at ${base}x silence saved ${run.saved.silence.toFixed(3)} s is within 1% of ${formula.toFixed(3)} s`,
        Math.abs(run.saved.silence - formula) <= 0.01 * formula, {sim: run.saved.silence, formula});
  check(`at ${base}x wall time equals media/base minus the saving`, Math.abs(run.wall - (dur / base - run.saved.silence)) < 0.002, run.wall);
  const speedWant = dur - dur / base;
  check(`at ${base}x speed saved is ${speedWant.toFixed(2)} s`, Math.abs(run.saved.speed - speedWant) < 0.002, run.saved.speed);
  check(`at ${base}x remaining time from the start matches the simulation`, Math.abs(R.wallRemaining(plan, 0, dur, base, true) - run.wall) < 0.002);
}

console.log('4. Seek inside a pause');
{ const [s, e] = gaps[3], mid = (s + e) / 2, run = simulate(plan1, mid, dur, 1);
  const times = run.log.map(x => x[1]);
  check('media time starts at the seek point and only moves forward', times[0] === mid && times.every((t, i) => i === 0 || t > times[i - 1]));
  check('the rest of the pause is still boosted', R.silenceRate(plan1, mid, 1, true) > 1);
  // 10 ms: the 5 ms timer holds each rate for a whole tick, so the ramps are a little coarse.
  check('remaining time from inside a pause matches the simulation (within 10 ms)', Math.abs(R.wallRemaining(plan1, mid, dur, 1, true) - run.wall) < 0.01,
        {rem: R.wallRemaining(plan1, mid, dur, 1, true), sim: run.wall});
  check('remaining time without shortening is media/base', R.wallRemaining(plan1, mid, dur, 2, false) === (dur - mid) / 2); }

console.log('5. No map, or the feature off');
check('no plan plays at base speed', R.silenceRate(null, gaps[3][0] + 0.5, 1.3, true) === 1.3);
check('feature off plays at base speed', R.silenceRate(plan1, gaps[3][0] + 0.5, 1.3, false) === 1.3);
check('an empty map plays at base speed', R.silenceRate(R.silencePlan([], [], 1), 3, 1, true) === 1);

console.log('6. Toggle off during playback');
{ const opt = {toggleAt: gaps[3][0] + 0.5}, run = simulate(plan1, gaps[3][0], dur, 1, opt);
  const after = run.log.find(([w, t]) => w > opt.toggledWall);
  check('rate is back to base speed on the next tick (well within 1 s)', after && after[2] === 1 && after[0] - opt.toggledWall <= 1, after);
  check('rate was boosted just before', run.log.some(([w, , r]) => w < opt.toggledWall && r > 1)); }

console.log('7. A pause inside an ad');
{ const ad = {start: gaps[2][0] - 0.2, end: gaps[2][1] + 0.2};
  const plan = R.silencePlan(gaps, [ad], 1);
  check('the pause inside the ad is dropped from the plan', R.findIv(plan, (gaps[2][0] + gaps[2][1]) / 2) === -1);
  check('the other pauses stay', plan.ivs.length === 2, plan.ivs.length);
  const withAd = simulate(plan, 0, dur, 1, {skips: [ad]}), noAd = simulate(R.silencePlan([gaps[1], gaps[3]], [], 1), 0, dur, 1);
  check('silence saved equals the run without that pause (within 1%)', Math.abs(withAd.saved.silence - noAd.saved.silence) <= 0.01 * noAd.saved.silence,
        {withAd: withAd.saved.silence, noAd: noAd.saved.silence});
  const overlap = R.silencePlan([[ad.end - 0.1, ad.end + 0.5]], [ad], 1);
  check('a pause that only overlaps the edge of an ad is dropped too', overlap.ivs.length === 0); }

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
