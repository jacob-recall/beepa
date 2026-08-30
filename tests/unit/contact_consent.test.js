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
console.log('ok contact_consent');
