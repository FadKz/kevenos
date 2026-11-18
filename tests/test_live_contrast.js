const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { JSDOM, VirtualConsole } = require('jsdom');

const htmlPath = process.argv[2] || path.join(__dirname, '../static/live.html');
const common = fs.readFileSync(path.join(__dirname, '../static/common.js'), 'utf8');
const source = fs.readFileSync(htmlPath, 'utf8');
assert(source.includes('<script src="/static/common.js"></script>'), 'Live must load the shared runtime');
const palette = fs.readFileSync(path.join(__dirname, '../static/app.css'), 'utf8');
const html = source.replace('<link rel="stylesheet" href="/static/app.css">', () => '<style>' + palette + '</style>')
  .replace('<script src="/static/common.js"></script>', () => '<script>' + common + '</script>');

async function render(query, verify) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  console.on('error', (...values) => errors.push(values.map(String).join(' ')));
  const dom = new JSDOM(html, {
    url: 'https://test.invalid/live' + query,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: console,
    beforeParse(window) {
      // Offline state is intentional; both the shared runtime and Live handle it.
      window.fetch = async () => { throw new Error('offline test'); };
      window.setInterval = () => 1;
    }
  });
  try {
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(errors, [], 'Live script errors must fail the contrast harness');
    verify(dom.window);
  } finally {
    dom.window.close();
  }
}

(async () => {
  for (const query of ['', '?embed=1', '?embed=1&theme=light']) {
    await render(query, window => {
      const theme = window.getComputedStyle(window.document.documentElement);
      assert.equal(theme.colorScheme, 'dark');
      const card = theme.getPropertyValue('--card').trim();
      const glass = theme.getPropertyValue('--glass').trim();
      assert.equal(card, '#122d4c');
      assert.equal(glass === 'var(--card)' ? card : glass, card);
      assert(!window.document.body.classList.contains('light'));
    });
  }
  console.log('Direct and embedded displays use the shared dark palette.');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
