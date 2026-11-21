export const LEVELS = [
  {
    title: '回声湾',
    name: 'Echo Bay',
    hint: '熟悉潮汐，带两件邮包回到入口码头。',
    budget: 22,
    rows: ['..#....', '.......', '.#.~.*.', '.D.~...', '.#.#...', '...*...', '.......'],
  },
  {
    title: '月牙水道',
    name: 'Crescent Sound',
    hint: '两条浅滩穿过岛链，留意抵达时的潮位。',
    budget: 31,
    rows: ['..*#...', '..##..*', '..~~.##', '.D##...', '..~~.#.', '.#..*#.', '......#'],
  },
  {
    title: '夜航外礁',
    name: 'Outer Reef',
    hint: '四件邮包分布在外礁，出发前留好回程航段。',
    budget: 33,
    rows: ['*..#..*', '.#.#.#.', '...~...', '.D#~#..', '.#.~.#.', '...#.*.', '..*....'],
  },
];

export const DIRECTIONS = {
  north: [0, -1],
  east: [1, 0],
  south: [0, 1],
  west: [-1, 0],
};

export function cellKey(x, y) {
  return `${x},${y}`;
}

export function createGame(level) {
  const remaining = [];
  let dock;
  level.rows.forEach((row, y) => {
    [...row].forEach((tile, x) => {
      if (tile === 'D') dock = { x, y };
      if (tile === '*') remaining.push(cellKey(x, y));
    });
  });
  return {
    level, dock, ...dock, remaining, total: remaining.length,
    turn: 0, heading: 'east', status: 'playing', outcome: 'ready',
  };
}

export function tideFor(game) {
  return Math.floor(game.turn / 2) % 2 === 0 ? 'high' : 'low';
}

export function step(game, action) {
  if (game.status !== 'playing') return game;
  const waiting = action === 'wait';
  const offset = waiting ? [0, 0] : Object.hasOwn(DIRECTIONS, action) && DIRECTIONS[action];
  if (!offset) return game;

  const x = game.x + offset[0];
  const y = game.y + offset[1];
  const terrain = game.level.rows[y]?.[x];
  if (!terrain) return { ...game, outcome: 'boundary' };
  if (terrain === '#') return { ...game, outcome: 'rock' };
  if (!waiting && terrain === '~' && tideFor(game) === 'low') {
    return { ...game, outcome: 'low_tide' };
  }

  const destination = cellKey(x, y);
  const collected = game.remaining.includes(destination);
  const remaining = collected ? game.remaining.filter((parcel) => parcel !== destination) : game.remaining;
  const turn = game.turn + 1;
  const atDock = x === game.dock.x && y === game.dock.y;
  const status = atDock && remaining.length === 0 ? 'won' : turn === game.level.budget ? 'lost' : 'playing';
  let outcome = waiting ? 'waited' : 'sailing';
  if (status !== 'playing') outcome = status;
  else if (collected) outcome = 'collected';
  else if (atDock && !waiting) outcome = 'early_dock';

  return { ...game, turn, x, y, remaining, heading: waiting ? game.heading : action, status, outcome };
}
