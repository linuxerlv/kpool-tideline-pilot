import { cellKey, createGame, DIRECTIONS, LEVELS, step, tideFor } from './game-engine.mjs';

const byId = (id) => document.getElementById(id);
const board = byId('board');
const routeButtons = [];
const helm = [...document.querySelectorAll('.helm [data-action]')];
const score = document.querySelector('.score-panel');
const result = byId('result');
const symbols = { '.': '水域', '#': '礁石', '~': '浅滩', D: '码头', '*': '水域' };
const keyActions = {
  ArrowUp: 'north', ArrowRight: 'east', ArrowDown: 'south', ArrowLeft: 'west',
  w: 'north', d: 'east', s: 'south', a: 'west', ' ': 'wait',
};

let route = 0;
let unlocked = 0;
let game;
let trail;
let tiles = [];

const coordinate = (x, y) => `${String.fromCharCode(65 + x)}${y + 1}`;

function marker(className) {
  const span = document.createElement('span');
  span.className = className;
  span.setAttribute('aria-hidden', 'true');
  return span;
}

function buildRoutes() {
  LEVELS.forEach(({ title }, index) => {
    const button = document.createElement('button');
    button.className = 'route';
    button.type = 'button';
    const number = document.createElement('span');
    number.className = 'route-index';
    number.textContent = `0${index + 1}`;
    number.setAttribute('aria-hidden', 'true');
    button.append(number, title);
    button.addEventListener('click', () => start(index));
    byId('routes').append(button);
    routeButtons.push(button);
  });
}

function buildChart() {
  const fragment = document.createDocumentFragment();
  tiles = game.level.rows.flatMap((row, y) => [...row].map((terrain, x) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tile';
    button.dataset.cell = coordinate(x, y);
    button.addEventListener('click', () => {
      const direction = Object.entries(DIRECTIONS).find(([, [dx, dy]]) => game.x + dx === x && game.y + dy === y);
      if (direction) act(direction[0]);
    });
    fragment.append(button);
    return { button, terrain, x, y };
  }));
  board.replaceChildren(fragment);
}

function renderChart(tide) {
  board.dataset.tide = tide;
  for (const { button, terrain, x, y } of tiles) {
    const current = x === game.x && y === game.y;
    const neighbor = Math.abs(x - game.x) + Math.abs(y - game.y) === 1;
    const key = cellKey(x, y);
    const parcel = game.remaining.includes(key);
    const shallow = terrain === '~';
    const blocked = terrain === '#' || (shallow && tide === 'low');
    const reachable = neighbor && game.status === 'playing';
    const description = [coordinate(x, y), symbols[terrain]];
    if (shallow) description.push(blocked ? '退潮，不能进入' : '涨潮，可以通行');
    if (parcel) description.push('有邮包');
    if (current) description.push('邮船当前位置');
    button.setAttribute('aria-label', description.join('，'));
    button.disabled = !reachable;
    button.tabIndex = reachable ? 0 : -1;
    button.classList.toggle('near', reachable);
    button.classList.toggle('rock', terrain === '#');
    button.classList.toggle('shallow', shallow);
    button.classList.toggle('blocked', blocked);
    const icons = [];
    if (terrain === 'D') icons.push(marker('dock'));
    if (parcel) icons.push(marker('parcel'));
    else if (!current && terrain !== 'D' && trail.has(key)) icons.push(marker('dot'));
    if (current) {
      const disc = marker('sailor-disc');
      const boat = marker('boat');
      boat.dataset.heading = game.heading;
      disc.append(boat);
      icons.push(disc);
    }
    button.replaceChildren(...icons);
  }
}

function renderStats(tide) {
  const left = game.level.budget - game.turn;
  const collected = game.total - game.remaining.length;
  byId('tide-panel').dataset.tide = tide;
  byId('tide-value').textContent = tide === 'high' ? '涨潮' : '退潮';
  byId('tide-next').textContent = tide === 'high'
    ? `浅滩可通行 · ${2 - game.turn % 2} 回合后退潮`
    : `浅滩封闭 · ${2 - game.turn % 2} 回合后涨潮`;
  byId('parcel-count').textContent = `${collected} / ${game.total}`;
  byId('turn-count').textContent = `${left} / ${game.level.budget}`;
  byId('turn-meter').style.width = `${left / game.level.budget * 100}%`;
  score.dataset.low = String(left <= 4);
  byId('parcel-meter').replaceChildren(...Array.from({ length: game.total }, (_, index) => {
    const pip = document.createElement('i');
    pip.className = index < collected ? 'collected' : '';
    return pip;
  }));
  for (const button of helm) button.disabled = game.status !== 'playing';
}

function renderResult() {
  if (game.status === 'playing') {
    result.hidden = true;
    return;
  }
  const won = game.status === 'won';
  const last = route === LEVELS.length - 1;
  result.hidden = false;
  result.dataset.status = game.status;
  byId('result-title').textContent = won ? (last ? '全部航线完成' : '邮包已送达') : '航程耗尽';
  byId('result-caption').textContent = won ? `用了 ${game.turn} 回合安全回港。` : '调整路线或等待时机，再试一次。';
  byId('result-action').textContent = won && !last ? '前往下一航区 →' : '重玩本关';
}

function log(previousTide, tide) {
  const location = coordinate(game.x, game.y);
  const messages = {
    ready: game.level.hint,
    boundary: '已到航图边缘，请转向。',
    rock: '前方有礁石，请绕行。',
    low_tide: '浅滩已露出水面。可以绕行，或等待涨潮。',
    sailing: `驶至 ${location}。`,
    waited: `在 ${location} 等待了一回合。`,
    collected: game.remaining.length ? '已收取一件邮包。' : '邮包已收齐。现在驶回码头。',
    early_dock: `已返回码头，海上还有 ${game.remaining.length} 件邮包。`,
    won: '邮包已送达，邮船安全回港。',
    lost: '航程耗尽，本次配送结束。',
  };
  let message = messages[game.outcome];
  if (previousTide !== tide && game.status === 'playing') message += tide === 'high' ? ' 涨潮了。' : ' 退潮了。';
  byId('log').textContent = message;
  const caution = ['boundary', 'rock', 'low_tide', 'lost'].includes(game.outcome);
  byId('logbook').dataset.tone = caution ? 'caution' : 'calm';
}

function render(previousTide = tideFor(game)) {
  const tide = tideFor(game);
  renderChart(tide);
  renderStats(tide);
  renderResult();
  for (const [index, button] of routeButtons.entries()) {
    button.disabled = index > unlocked;
    button.setAttribute('aria-label', `${LEVELS[index].title}${button.disabled ? '，完成上一航区后开启' : ''}`);
    if (index === route) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  }
  log(previousTide, tide);
}

function start(index) {
  route = index;
  game = createGame(LEVELS[route]);
  trail = new Set();
  byId('route-title').textContent = game.level.title;
  byId('route-name').textContent = game.level.name;
  buildChart();
  render();
}

function act(action) {
  if (game.status !== 'playing') return;
  const previous = game;
  const previousTide = tideFor(previous);
  const boardFocused = board.contains(document.activeElement);
  game = step(game, action);
  const moved = previous.x !== game.x || previous.y !== game.y;
  if (moved) trail.add(cellKey(previous.x, previous.y));
  if (game.status === 'won') unlocked = Math.max(unlocked, Math.min(route + 1, LEVELS.length - 1));
  render(previousTide);
  if (game.status !== 'playing') byId('result-action').focus();
  else if (boardFocused && moved) board.querySelector('.tile.near:not(.blocked)')?.focus({ preventScroll: true });
}

for (const button of helm) button.addEventListener('click', () => act(button.dataset.action));
byId('restart').addEventListener('click', () => start(route));
byId('result-action').addEventListener('click', () => {
  start(game.status === 'won' && route < LEVELS.length - 1 ? route + 1 : route);
  routeButtons[route].focus();
});

document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === ' ' && event.target.closest?.('button, summary')) return;
  if (!Object.hasOwn(keyActions, key)) return;
  event.preventDefault();
  act(keyActions[key]);
});

buildRoutes();
start(0);
