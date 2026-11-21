import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createGame, LEVELS, step, tideFor } from '../game-engine.mjs';

const chart = (rows, budget = 12) => ({ rows, budget });
const sail = (game, ...route) => route.reduce(step, game);

test('moving and waiting consume a turn and flip the tide every two turns', () => {
  const start = createGame(chart(['D~*.', '....']));
  assert.equal(tideFor(start), 'high');
  const shallow = step(start, 'east');
  assert.deepEqual([shallow.x, shallow.y, shallow.turn, tideFor(shallow)], [1, 0, 1, 'high']);
  const ebb = step(shallow, 'wait');
  assert.deepEqual([ebb.x, ebb.y, ebb.turn, tideFor(ebb)], [1, 0, 2, 'low']);
  const escaped = step(ebb, 'east');
  assert.equal(escaped.remaining.length, 0, 'you can leave a shallow during ebb tide');
  assert.equal(step(escaped, 'west').outcome, 'low_tide');
  assert.equal(tideFor(step(escaped, 'wait')), 'high');
});

test('rock, coast and dry shallows preserve turn and location', () => {
  const start = createGame(chart(['D~*', '#..']));
  for (const [heading, outcome] of [['south', 'rock'], ['north', 'boundary']]) {
    const blocked = step(start, heading);
    assert.deepEqual([blocked.x, blocked.y, blocked.turn, blocked.outcome], [0, 0, 0, outcome]);
  }
  const low = sail(start, 'wait', 'wait');
  const dry = step(low, 'east');
  assert.deepEqual([dry.x, dry.y, dry.turn, dry.outcome], [0, 0, 2, 'low_tide']);
  assert.equal(start.turn, 0, 'previous snapshots stay intact');
});

test('parcel is collected once and returning to dock early does not finish the route', () => {
  const start = createGame(chart(['D.*']));
  const dock = sail(start, 'east', 'west');
  assert.equal(dock.outcome, 'early_dock');
  assert.equal(dock.status, 'playing');
  const delivered = sail(dock, 'east', 'east', 'west', 'east');
  assert.deepEqual(delivered.remaining, []);
  assert.equal(delivered.outcome, 'sailing');
  assert.equal(sail(delivered, 'west', 'west').status, 'won');
});

test('the last turn can win, or exhaust the budget away from dock', () => {
  const level = chart(['D*'], 2);
  const start = createGame(level);
  assert.equal(sail(start, 'east', 'west').status, 'won');
  const exhausted = sail(start, 'east', 'wait');
  assert.equal(exhausted.status, 'lost');
  assert.strictEqual(step(exhausted, 'west'), exhausted);
  assert.deepEqual(createGame(level), start);
});

test('three handmade routes are progressively harder and all fit their budgets', () => {
  assert.equal(LEVELS.length, 3);
  let previous = 0;
  for (const level of LEVELS) {
    assert.equal(level.rows.length, 7);
    assert.ok(level.rows.every((row) => row.length === 7));
    const length = shortestRoute(level);
    assert.ok(length > previous, `${level.title} is a longer puzzle than the previous route`);
    assert.ok(length <= level.budget, `${level.title} needs ${length} of ${level.budget} turns`);
    previous = length;
  }
});

function shortestRoute(level) {
  const queue = [createGame(level)];
  const seen = new Set();
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const game = queue[cursor];
    const key = [game.x, game.y, game.turn % 4, game.remaining.join(';')].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    if (game.status === 'won') return game.turn;
    if (game.status === 'lost') continue;
    for (const heading of ['north', 'east', 'south', 'west', 'wait']) {
      const next = step(game, heading);
      if (next.turn > game.turn) queue.push(next);
    }
  }
  return Infinity;
}
