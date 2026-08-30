// tests/unit/contacts_profile_handles.test.js
import { normalizeProfiles, linkHandle, unlinkHandle, handleOwner } from '../../shared/model/contacts.js';
function eq(a,b,m){ if(JSON.stringify(a)!==JSON.stringify(b)) throw new Error(m+': '+JSON.stringify(a)); }

// a handle can belong to at most one profile (first wins on normalize)
let p = normalizeProfiles({ profiles: [
  { id:'cp_a', displayName:'A', roomIds:[], handleIds:[{source:'imessage', network_id:'+1555'}] },
  { id:'cp_b', displayName:'B', roomIds:[], handleIds:[{source:'imessage', network_id:'+1555'}] },
]});
eq(handleOwner(p.profiles,'imessage','+1555'), 'cp_a', 'first profile wins');

// link moves the handle (removes from the old owner, upholds invariant)
p = linkHandle(p.profiles, 'cp_b', 'imessage', '+1555');
eq(handleOwner(p.profiles,'imessage','+1555'), 'cp_b', 'relink moves handle');
const aStill = p.profiles.find(x=>x.id==='cp_a').handleIds.length;
eq(aStill, 0, 'old owner lost the handle');

// unlink clears ownership
p = unlinkHandle(p.profiles, 'imessage', '+1555');
eq(handleOwner(p.profiles,'imessage','+1555'), null, 'unlinked');

// malformed handle entries are dropped; rooms + share untouched
const n = normalizeProfiles({ profiles: [
  { id:'cp_c', displayName:'C', roomIds:['!r:h'], share:'share',
    handleIds:[{source:'', network_id:'x'}, {source:'whatsapp'}, {source:'whatsapp', network_id:'1@w'}] },
]});
eq(n.profiles[0].handleIds, [{source:'whatsapp', network_id:'1@w'}], 'malformed dropped');
eq(n.profiles[0].share, 'share', 'share untouched');
console.log('ok contacts_profile_handles');
