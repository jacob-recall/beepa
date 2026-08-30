// tests/unit/contact_consent.test.js
import { resolveContactShare, normalizeContactPolicy } from '../../shared/model/consent.js';
function eq(a, b, m){ if (JSON.stringify(a)!==JSON.stringify(b)) throw new Error(m+': '+JSON.stringify(a)); }

// default = private
eq(resolveContactShare('imessage', normalizeContactPolicy(undefined)), {shared:false, reason:'private'}, 'default');
// global share-all
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all'})), {shared:true, reason:'all contacts'}, 'global');
// per-source overrides global
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all', sources:{imessage:'private-all'}})), {shared:false, reason:'private'}, 'src-private wins');
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'private', sources:{imessage:'share-all'}})), {shared:true, reason:'all imessage contacts'}, 'src-share wins');
// garbage collapses to safe default
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'yolo', sources:{imessage:'maybe'}})), {shared:false, reason:'private'}, 'garbage safe');
// array-shaped sources (typeof 'object' in JS) must be rejected like no sources,
// not walked as {'0':..,'1':..} — matches Python's isinstance(dict). Parity guard.
eq(normalizeContactPolicy({global:'private', sources:['share-all','private-all']}), {global:'private', sources:{}}, 'array sources -> {}');
eq(normalizeContactPolicy({global:'private', sources:['share-all','private-all']}), normalizeContactPolicy({global:'private'}), 'array sources same as no sources');
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'private', sources:['share-all']})), {shared:false, reason:'private'}, 'array sources resolves like empty -> private');
console.log('ok contact_consent');
