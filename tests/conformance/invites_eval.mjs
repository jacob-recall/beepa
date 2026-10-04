// JS half of the bridge-invite conformance harness
// (tests/conformance/invites_conformance.py). Reads a JSON array of vectors on
// stdin, evaluates each through apps/user/invites.js, and prints a JSON array
// of results on stdout — one per vector, in order. An exception becomes
// {"__error__": "<name>"} so a crash is a visible, comparable outcome.
//
// NO logic of its own: it only dispatches to the exported predicate, so the
// Python runner compares the two real implementations, not two test doubles.
import { localpart, bridgeInvitesToJoin, ROOM_SHAPE_RE } from '../../apps/user/invites.js';

function evalOne(v) {
  switch (v.kind) {
    case 'bridge_invites_to_join':
      // `undefined` for an absent key, so the JS default-argument path is the
      // one exercised (Python's .get(...) -> None must land identically).
      return bridgeInvitesToJoin(
        'section' in v ? v.section : undefined,
        'bots' in v ? v.bots : undefined,
        'self' in v ? v.self : undefined,
        'spaces' in v ? v.spaces : undefined,
        'opts' in v ? v.opts : undefined);
    case 'localpart':
      return localpart('mxid' in v ? v.mxid : undefined);
    case 'room_shape':
      // RegExp.test coerces; only strings are ever passed from the generator.
      return ROOM_SHAPE_RE.test(v.s);
    default:
      throw new Error('unknown vector kind: ' + v.kind);
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  const vectors = JSON.parse(input);
  const out = vectors.map((v) => {
    try {
      const r = evalOne(v);
      return r === undefined ? { __undefined__: true } : r;
    } catch (e) {
      return { __error__: (e && e.constructor && e.constructor.name) || 'Error' };
    }
  });
  process.stdout.write(JSON.stringify(out));
});
