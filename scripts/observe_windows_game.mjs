import { strict as assert } from 'node:assert';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';

export function selectGameTarget(targets, gameUrl, port) {
  const pages = targets.filter(({ type, url }) => type === 'page' && url === gameUrl);
  assert(pages.length < 2, 'Multiple browser targets match the launched game');
  if (pages.length === 0) return undefined;

  const endpoint = new URL(pages[0].webSocketDebuggerUrl);
  assert(endpoint.origin === 'ws://127.0.0.1:' + port &&
    endpoint.pathname.startsWith('/devtools/page/') &&
    !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash,
  'Invalid DevTools endpoint for the launched game');
  return endpoint.href;
}

async function fetchLocal(port, path) {
  const response = await fetch('http://127.0.0.1:' + port + path, {
    redirect: 'error', signal: AbortSignal.timeout(1000),
  });
  assert(response.ok, 'DevTools HTTP request failed');
  return response.json();
}

async function browserTarget(profile, gameUrl) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    let port;
    let targets;
    try {
      const text = await readFile(profile + '/DevToolsActivePort', 'utf8');
      const firstLine = text.split(/\r?\n/)[0];
      if (!/^[1-9][0-9]{0,4}$/.test(firstLine)) throw new Error('Invalid DevTools port file');
      port = Number(firstLine);
      assert(port <= 65535, 'Invalid DevTools port');
      targets = await fetchLocal(port, '/json/list');
    } catch {
      await delay(150);
      continue;
    }
    const endpoint = selectGameTarget(targets, gameUrl, port);
    if (endpoint) return { endpoint, port };
    await delay(150);
  }
  throw new Error('Chromium did not publish a tab for the file opened by the launcher');
}

async function openDevTools(endpoint) {
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('DevTools connection timed out')), 5000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('DevTools connection failed')); }, { once: true });
  }).catch((error) => {
    socket.close();
    throw error;
  });

  const pending = new Map();
  const events = [];
  let sequence = 0;
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    const request = pending.get(message.id);
    if (request) {
      clearTimeout(request.timer);
      pending.delete(message.id);
      if (message.error) request.reject(new Error(request.method + ': ' + message.error.message));
      else request.resolve(message.result);
    } else if (message.method) {
      events.push(message);
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(method + ' timed out'));
    }, 4500);
    pending.set(id, { method, timer, resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  return { socket, send, events };
}

async function evaluate(send, expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true });
  assert(!result.exceptionDetails, 'The runtime did not return the requested game state');
  return result.result.value;
}

const directions = {
  right: ['ArrowRight', 'ArrowRight', 39],
  left: ['ArrowLeft', 'ArrowLeft', 37],
  up: ['ArrowUp', 'ArrowUp', 38],
  down: ['ArrowDown', 'ArrowDown', 40],
  wait: [' ', 'Space', 32],
};

async function play({ send, events }, gameUrl, screenshotPath) {
  for (const domain of ['Page', 'Runtime', 'Log', 'Network']) await send(domain + '.enable');
  await send('Network.emulateNetworkConditions', {
    offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
  });
  await send('Page.bringToFront');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  });
  const ready = "document.readyState === 'complete' && document.querySelector('#board')?.children.length === 49";
  const deadline = Date.now() + 10000;
  while (!await evaluate(send, ready)) {
    assert(Date.now() < deadline, 'Chromium did not render the 49-cell game board');
    await delay(100);
  }

  const snapshot = () => evaluate(send, [
    '(() => {',
    'const get = (id) => document.getElementById(id);',
    'return { location: location.href, title: document.title, route: get("route-title").textContent,',
    '  cells: get("board").childElementCount, parcels: get("parcel-count").textContent,',
    '  turns: get("turn-count").textContent, log: get("log").textContent,',
    '  result: get("result").hidden ? null : get("result").dataset.status,',
    '  resultTitle: get("result-title").textContent };',
    '})()',
  ].join('\n'));
  const key = async (direction) => {
    const [value, code, windowsVirtualKeyCode] = directions[direction];
    for (const type of ['keyDown', 'keyUp']) {
      await send('Input.dispatchKeyEvent', { type, key: value, code, windowsVirtualKeyCode });
    }
  };

  const start = await snapshot();
  assert(start.location === gameUrl && /\bTIDELINE\b/.test(start.title) && start.route &&
    start.cells === 49 && start.parcels === '0 / 2' && start.result === null,
  'Unexpected initial state for the launcher-opened game');
  for (const direction of ['right', 'right', 'right']) await key(direction);
  const beforeBlocked = await snapshot();
  await key('left');
  const blocked = await snapshot();
  assert(blocked.turns === beforeBlocked.turns && blocked.log.includes('浅滩'), 'Low-tide move was not blocked');
  for (const direction of ['wait', 'left', 'right', 'up', 'right']) await key(direction);
  assert.equal((await snapshot()).parcels, '1 / 2');
  for (const direction of ['down', 'down', 'down', 'left', 'left', 'left', 'up', 'up', 'left']) await key(direction);
  const finish = await snapshot();
  assert(finish.location === gameUrl && finish.parcels === '2 / 2' &&
    finish.result === 'won' && finish.resultTitle.length > 0, 'First route did not finish');

  const { data } = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
  const image = Buffer.from(data, 'base64');
  assert(image.length > 1024 && image.subarray(1, 4).toString() === 'PNG', 'Missing the rendered PNG');
  const webRequests = events.filter(({ method, params }) =>
    method === 'Network.requestWillBeSent' && /^https?:/.test(params.request.url));
  const browserErrors = events.filter(({ method, params }) =>
    method === 'Runtime.exceptionThrown' || (method === 'Log.entryAdded' && params.entry.level === 'error'));
  assert.equal(webRequests.length, 0, 'The observed game issued a web request');
  assert.equal(browserErrors.length, 0, 'The observed game reported an error');
  await writeFile(screenshotPath, image, { flag: 'wx' });
  return {
    file_scheme: true,
    title: start.title,
    route: start.route,
    board_cells: start.cells,
    low_tide_blocked: true,
    parcels: finish.parcels,
    route_result: finish.result,
    result_title: finish.resultTitle,
    observed_web_requests: webRequests.length,
    observed_browser_errors: browserErrors.length,
    screenshot_sha256: createHash('sha256').update(image).digest('hex'),
  };
}

async function main(profile, gameFile, screenshotPath) {
  assert(profile && gameFile && screenshotPath, 'Expected profile, game file and screenshot paths');
  const gameUrl = pathToFileURL(gameFile).href;
  const { endpoint, port } = await browserTarget(profile, gameUrl);
  const { Browser: browserVersion } = await fetchLocal(port, '/json/version');
  assert(typeof browserVersion === 'string' && browserVersion.length > 0, 'Missing Chromium version');
  const channel = await openDevTools(endpoint);
  try {
    const proof = await play(channel, gameUrl, screenshotPath);
    process.stdout.write(JSON.stringify({ browser_version: browserVersion, ...proof }) + '\n');
  } finally {
    channel.socket.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main(...process.argv.slice(2));
}
