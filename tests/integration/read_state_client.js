import { parseMessageReceipts, mergeReceipts, NATIVE_RECEIPT_TYPE } from '../../shared/model/message_receipts.js';
import { SOURCES } from '../../shared/model/source_catalog.js';
// Exercise the app's real receipt writer/parser against the disposable homeserver.
// Credentials arrive on stdin, never argv; sandbox.py owns the server lifecycle.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const model = new Map([[input.roomId, { unread: 1 }]]);
let writes = 0;
const ctx = vm.createContext({ parseMessageReceipts, mergeReceipts, NATIVE_RECEIPT_TYPE,
  S: { openRoomId: input.roomId, joinedSet: new Set([input.roomId]) },
  ROOMID_RE: /^![^:]+:.+$/, feedModel: model, SOURCES, setTimeout,
  localStorage: { getItem: () => null }, scheduleFeedRender() {},
  api: async (method, path, body) => {
    const response = await fetch(input.base + path, {
      method, headers: { Authorization: 'Bearer ' + input.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    assert.equal(response.status, 200, 'receipt write accepted');
    writes++;
    return response.json();
  },
});
vm.runInContext(source('../../shared/ui/account-data.js'), ctx);
vm.runInContext(source('../../shared/ui/chat.js'), ctx);
if (input.action === 'mark') {
  await ctx.markRead(input.roomId, input.eventId);
  assert.equal(writes, 1, 'real app markRead sent one receipt');
  assert.equal(model.get(input.roomId).unread, 0, 'app cleared local badge after success');
  console.log(JSON.stringify({ marked: true }));
} else {
  console.log(JSON.stringify((() => { const value = ctx.parseReadState(input.room, new Set([input.userId]), input.sourceId); return {...value, receipts: Object.fromEntries(value.receipts)}; })()));
}
