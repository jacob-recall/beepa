// Unit test for shared/ui/el.js linkifyTokens() — the pure tokenizer behind
// clickable links in message bodies. Message text is UNTRUSTED; the security
// contract under test: only explicit http(s):// URLs ever become links, the
// link text is byte-identical to the href source (no deceptive text/href
// mismatch), and tokenization is lossless (concatenating token texts
// reproduces the input exactly, so no message content is ever dropped).
// Run: node tests/unit/linkify.test.js  (exit 0 = all pass).
import { linkifyTokens } from '../../shared/ui/el.js';

let pass = 0, fail = 0; const failures = [];
function check(name, cond) {
  if (cond) pass += 1; else { fail += 1; failures.push(name); }
}
const links = (s) => linkifyTokens(s).filter((t) => t.kind === 'link');
const lossless = (s) => linkifyTokens(s).map((t) => t.text).join('') === s;

// basic http/https
let t = linkifyTokens('see https://example.com/a?b=1 ok');
check('https linkified', links('see https://example.com/a?b=1 ok').length === 1);
check('href normalized', links('go https://example.com')[0].href === 'https://example.com/');
check('text equals matched url', links('x http://ex.com/p y')[0].text === 'http://ex.com/p');
check('surrounding text preserved', t[0].text === 'see ' && t[2].text === ' ok');
check('lossless basic', lossless('see https://example.com/a?b=1 ok'));

// never-link cases
check('javascript: never a link', links('javascript:alert(1)').length === 0);
check('data: never a link', links('data:text/html,x').length === 0);
check('ftp not matched', links('ftp://example.com/x').length === 0);
check('bare domain not matched', links('visit example.com now').length === 0);
check('scheme mid-word not hijacked text', lossless('xhttps://e.com'));
check('empty/non-string safe', linkifyTokens('').length === 0 && linkifyTokens(null).length === 0);

// trailing punctuation
check('trailing period trimmed', links('go to https://ex.com/a.')[0].text === 'https://ex.com/a');
check('trailing period kept as text', lossless('go to https://ex.com/a.'));
check('trailing paren trimmed (plain)', links('(see https://ex.com/a)')[0].text === 'https://ex.com/a');
check('wiki paren kept', links('https://en.wikipedia.org/wiki/Foo_(bar)')[0].text
      === 'https://en.wikipedia.org/wiki/Foo_(bar)');
check('trailing comma trimmed', links('https://ex.com/a, then')[0].text === 'https://ex.com/a');

// multiple links + lossless
const multi = 'a https://one.example/x b http://two.example/y. c';
check('two links found', links(multi).length === 2);
check('lossless multi', lossless(multi));

// a URL that fails new URL() stays plain text
check('unparseable stays text', links('http://[not-a-host').length === 0);
check('unparseable lossless', lossless('see http://[not-a-host end'));

console.log(pass + ' passed, ' + fail + ' failed');
if (failures.length) console.log('FAILED: ' + failures.join(', '));
process.exit(fail ? 1 : 0);
