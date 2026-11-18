const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const judge = fs.readFileSync(path.join(root, 'static', 'judge.html'), 'utf8');
const desk = fs.readFileSync(path.join(root, 'static', 'desk.html'), 'utf8');

assert.match(judge, /Overall impression and team confidence/);
assert.match(judge, /Your Final score/);
assert.match(judge, /role="radiogroup"/);
assert.match(judge, /aria-checked/);
assert.match(judge, /final_rating:D\.final_rating/);
assert.match(judge, /wrap_rating:D\.wrap_rating/);
assert.match(desk, /Exactly three teams advance/);
assert.match(desk, /Average \/ 5/);
assert.match(desk, /Overall impression and confidence/);
assert.match(desk, /final_rating/);
assert.match(desk, /wrap_rating/);

console.log('star scoring UI contract: ok');
