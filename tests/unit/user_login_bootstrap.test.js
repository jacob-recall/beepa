// Exercise the actual app wiring both before and after DOMContentLoaded.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../../apps/user/main.js'), 'utf8');
const imports = [...source.matchAll(/import\s*\{([^}]+)\}\s*from\s*['"][^'"]+['"];?/g)];
const program = source.replace(/import\s*\{[^}]+\}\s*from\s*['"][^'"]+['"];?/g, '');
(async () => {
  for (const readyState of ['loading', 'interactive', 'complete']) {
    const elements = new Map(), events = new Map(), requests = [];
    function element(id) {
      if (!elements.has(id)) elements.set(id, {
        value: '', textContent: '', classList: { add() {}, remove() {} },
        addEventListener(name, fn) { this[name] = fn; },
      });
      return elements.get(id);
    }
    const context = { document: { readyState, addEventListener: (name, fn) => events.set(name, fn) },
      console, Map, Set, setTimeout, clearTimeout, setInterval, clearInterval,
      sessionStorage: { getItem: () => null, setItem() {} },
      localStorage: { getItem: () => null, setItem() {} },
      fetch: async () => ({ok: false}) };
    for (const match of imports) for (const name of match[1].split(',').map(x => x.trim())) context[name] = () => {};
    Object.assign(context, { $: element, S: {},
      resolveLoginName: user => user === 'elliot' ? '@elliot-msgr-spoof:localhost' : user,
      api: async (method, path, body) => { requests.push({method, path, body}); return {access_token:'synthetic', user_id:'@elliot-msgr-spoof:localhost',device_id:'synthetic'}; },
    });
    vm.createContext(context);
    vm.runInContext(program, context);
    if (readyState === 'loading') {
      assert.equal(elements.has('btn-signin'), false);
      events.get('DOMContentLoaded')();
    }
    assert.equal(typeof element('btn-signin').click, 'function', readyState + ': missing login handler');
    // Test login and wiring without starting message sync or provider calls.
    vm.runInContext('enterApp = async () => {};', context);
    element('in-user').value = 'elliot';
    element('in-pass').value = 'password';
    await element('btn-signin').click();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, '/_matrix/client/v3/login');
    assert.equal(requests[0].body.identifier.user, '@elliot-msgr-spoof:localhost');
    assert.equal(requests[0].body.password, 'password');
    assert.equal(element('in-pass').value, '');
  }
  console.log('PASS: login wiring and alias work before and after DOMContentLoaded');
})().catch(e => { console.error(e); process.exitCode = 1; });
