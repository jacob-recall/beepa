import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { nativeReadState, nativeReadFromRoom, canAutoRead } from '../../shared/model/native_read.js';
import { messageTimestamp } from '../../shared/model/message_timestamps.js';
import { parseMessageReceipts, mergeReceipts } from '../../shared/model/message_receipts.js';
const native = { schema:1, supported:true, revision:'a'.repeat(64), unread_count:0, marked_unread:false, latest_ts:100, observed_ts:200 };
const ev = {type:'com.jkali.imessage_read_state', state_key:'', sender:'@imessagebot:localhost', content:native};
assert.equal(nativeReadFromRoom({state:{events:[ev]}}, ev.sender).unread_count, 0);
assert.equal(nativeReadFromRoom({state:{events:[{...ev,sender:'@bad:localhost'}]}}, ev.sender), null);
assert.equal(nativeReadState({...native, unread_count:true}), null);
assert.equal(nativeReadFromRoom({timeline:{events:[{...ev,content:{...native,observed_ts:1,unread_count:9}}]}},ev.sender,native).unread_count,0);
assert.equal(canAutoRead({...native,unread_count:2},100,true),true);
for (const args of [[99,true,false],[100,false,false],[100,true,true]]) assert.equal(canAutoRead({...native,unread_count:2},...args),false);
const accountSource = fs.readFileSync(new URL('../../shared/ui/account-data.js', import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replace(/^export .*;\n/gm,'');
const feedModel = new Map([['!room:localhost',{id:'!room:localhost',sourceId:'imessage',unread:9999,nativeRead:native,receipts:new Map()}]]);
const ctx = vm.createContext({nativeReadFromRoom, parseMessageReceipts, mergeReceipts, S:{}, feedModel,
 IMSG_BOT_MXID:ev.sender, SOURCES:[{id:'imessage',botMxid:ev.sender}], scheduleFeedRender(){}, applyReadCaption(){}, CORRECTION_TYPE:'correction'});
vm.runInContext(accountSource,ctx);
vm.runInContext('feedLastPreview = () => null;',ctx);
let parsed = ctx.parseReadState({unread_notifications:{notification_count:9999},state:{events:[ev]}},new Set(),'imessage');
assert.equal(parsed.unread,0);
ctx.feedIngest({rooms:{join:{'!room:localhost':{unread_notifications:{notification_count:9999}}}}});
assert.equal(feedModel.get('!room:localhost').unread,0,'incremental Matrix count cannot resurrect native-read badge');
ctx.feedIngest({rooms:{join:{'!room:localhost':{timeline:{events:[{...ev,content:{...native,unread_count:1,marked_unread:true,observed_ts:300}}]}}}}});
assert.equal(feedModel.get('!room:localhost').unread,1,'native manual unread arrives without a new message');

// Exercise the actual master request function, including its pinned context.
const master = fs.readFileSync(new URL('../../apps/master/main.js',import.meta.url),'utf8');
const block = master.slice(master.indexOf('let nativeReadIntent = null;'),master.indexOf('function renderRoomReadLine(rec)'));
const rec = {sourceId:'imessage',nativeRead:{...native,unread_count:1},mirrorOf:'!local:localhost',userLabel:'peer',createSender:'@peer:master',timestampCorrections:new Map([['$latest',100]])};
const MS = {openRoomId:'!mirror:master',rooms:{'!mirror:master':rec},openProposalCtx:{mirrorRoomId:'!mirror:master',targetRoom:'!local:localhost',label:'peer',proposalsRoomId:'!proposals:master'},
 proposalsRoomSet:new Set(['!proposals:master']),proposalsByUser:new Map([['peer','!proposals:master']]),crmCapabilities:new Map([['@peer:master',{generation:'current',native_read:true}]]),roomEvents:new Map([['$older',{event_id:'$older',origin_server_ts:50}],['$latest',{event_id:'$latest',origin_server_ts:90}]])};
const writes=[];
const m = vm.createContext({MS,S:{token:'session'},roomEpoch:1,canAutoRead,nativeReadState,document:{visibilityState:'visible'}, ROOM_SHAPE_RE:/^![^:]+:.+$/, messageTimestamp,
 setTimeout(){}, $:()=>null,renderRoomReadLine(){},roomStatus(){},api:async(...args)=>{writes.push(args);return {};}});
vm.runInContext(master.slice(master.indexOf('function masterUnreadForRoom('),master.indexOf('function freshRoomInfo(')) + master.slice(master.indexOf('function mirrorTs('),master.indexOf('// Display-only name for an mxid.')) + block,m);
await m.requestNativeRead('read');
assert.equal(writes.length,1);assert.ok(writes[0][1].includes('/send/com.jkali.read_request/'));
assert.equal(writes[0][2]['com.jkali.read_action'].action,'read');assert.equal(writes[0][2].body,undefined);
await m.requestNativeRead('read');assert.equal(writes.length,1,'pending auto-read deduplicated');
rec.nativeRead.last_command={id:writes[0][2]['com.jkali.read_action'].command_id,status:'applied'};
await m.requestNativeRead('unread',true);assert.equal(writes.length,2);
rec.nativeRead.last_command={id:writes[1][2]['com.jkali.read_action'].command_id,status:'applied'};
rec.nativeRead.revision='b'.repeat(64);
await m.requestNativeRead('read');assert.equal(writes.length,2,'manual unread survives open viewer');
MS.proposalsByUser.set('peer','!replacement:master');
await m.requestNativeRead('read',true);assert.equal(writes.length,2,'changed destination cannot redirect a read action');
console.log('native_read: source authority, native count precedence, visibility/latest-message guards, pinned master request, dedup and manual-unread hold pass');

// A state-only native confirmation must repaint the existing sidebar/feed rows.
const row = {id:'!mirror:master',unread:8,drafts:[{body:'retained'}]};
MS.byUser = new Map([['peer',[row]]]); MS.feed = [row];
rec.readState = {unread_count:9000};rec.masterUnread=0; let repaints=0;
m.rerenderActiveList=()=>{repaints++;};
m.refreshRoomUnread('!mirror:master');
assert.equal(row.unread,0);assert.equal(MS.feed[0].unread,0);
assert.equal(row.drafts.length,1);assert.equal(repaints,1);
m.refreshRoomUnread('!mirror:master');assert.equal(repaints,1);
rec.masterUnread=1;m.refreshRoomUnread('!mirror:master');
assert.equal(row.unread,1);assert.equal(repaints,2);

// Master unread is independent of a source backlog or missing native support.
assert.equal(m.masterUnreadForRoom({sourceId:'gmessages',masterUnread:0,readState:{unread_count:500}}),0);
assert.equal(m.masterUnreadForRoom({masterUnread:5,readState:{unread_count:0}}),5);
assert.equal(m.masterUnreadForRoom({masterUnread:5,masterReadThrough:'$latest',lastEventId:'$latest'}),0);
assert.equal(m.masterUnreadForRoom({masterUnread:1,masterReadThrough:'$older',lastEventId:'$new'}),1);
assert.equal(m.masterUnreadForRoom({sourceId:'imessage',masterUnread:5,nativeRead:{...native,latest_ts:100},lastTs:100}),0);
// The private read path works on an account without native read support.
rec.nativeRead=null;rec.lastEventId='$latest';rec.masterUnread=9;
MS.roomEvents=new Map([['$latest',{event_id:'$latest',sender:rec.createSender,origin_server_ts:100}]]);
m.ROOMID_RE=/^![^:]+:.+$/;m.snapshotCacheSave=async()=>{};
await m.markMasterRead();
const marker=writes.at(-1);assert.equal(marker[0],'POST');assert.ok(marker[1].endsWith('/read_markers'));
assert.equal(marker[2]['m.read.private'],'$latest');assert.equal(marker[2]['m.read'],undefined);
assert.equal(row.unread,0);assert.equal(rec.masterReadThrough,'$latest');

rec.lastEventId='$next';rec.masterUnread=3;
MS.roomEvents=new Map([['$next',{event_id:'$next',sender:rec.createSender,origin_server_ts:200}]]);
m.api=async()=>{throw Error('offline');};
await m.markMasterRead();assert.equal(rec.masterUnread,3,'failed master marker does not erase count');
let calls=0, finish;
m.api=async()=>{calls++;await new Promise(resolve=>{finish=resolve;});return {};};
m.document.visibilityState='hidden';await m.markMasterRead();assert.equal(calls,0);
m.document.visibilityState='visible';const inflight=m.markMasterRead();
rec.lastEventId='$newer';finish();await inflight;
assert.equal(rec.masterUnread,3,'late receipt completion cannot clear a newer message');
