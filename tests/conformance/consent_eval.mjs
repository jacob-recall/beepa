// JS half of the consent conformance harness (tests/conformance/consent_conformance.py).
// Reads a JSON array of vectors on stdin, evaluates each through
// shared/model/consent.js, and prints a JSON array of results on stdout —
// one per vector, in order. An exception becomes {"__error__": "<name>"} so a
// crash is a visible, comparable outcome, never a silent skip.
//
// This file contains NO policy logic of its own: it only dispatches to the
// exported resolver functions, so the Python runner is comparing the two real
// implementations, not two test doubles.
import {
  resolve, effectiveShared, resolveAll, normalizePolicy, normalizeOverride,
  overridesFromSync, normalizeContactPolicy, resolveContactShare,
} from '../../shared/model/consent.js';

function evalOne(v) {
  switch (v.kind) {
    case 'resolve':
      return resolve(v.convo, v.policy, v.override, v.profile);
    case 'effective_shared':
      return effectiveShared(v.convo, v.policy, v.override, v.profile);
    case 'resolve_all':
      return resolveAll(v.convos, v.policy, v.overrides, v.profiles);
    case 'normalize_policy':
      return normalizePolicy(v.p);
    case 'normalize_override':
      return normalizeOverride(v.data);
    case 'overrides_from_sync':
      return overridesFromSync(v.sync);
    case 'normalize_contact_policy':
      return normalizeContactPolicy(v.raw);
    case 'resolve_contact_share':
      return resolveContactShare(v.source, v.policy);
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
      // undefined is not JSON; make it an explicit, comparable value.
      return r === undefined ? { __undefined__: true } : r;
    } catch (e) {
      return { __error__: (e && e.constructor && e.constructor.name) || 'Error' };
    }
  });
  process.stdout.write(JSON.stringify(out));
});
