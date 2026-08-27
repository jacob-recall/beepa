// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

// Cross-module mutable state. The original app.js kept these as module-scope
// `let`/`const` bindings in one file; ES modules cannot share reassignable
// `let` across files, so every reassigned variable lives on the single shared
// object S (property writes are shared by reference). The always-mutated (never
// reassigned) collections stay as exported const and are mutated in place.
export const S = {
  token: null, userId: null,
  syncRunning: false, syncSince: null,
  qr: { eventId: null, blobUrl: null, boxId: null },
  loginFlowActive: false,
  busy: false,
  activeSettingsSource: 'whatsapp',
  joinedSet: new Set(),
  sourceViewId: null,
  feedRunning: false, feedSince: null,
  feedRenderScheduled: false,
  feedRevalTimer: null,
  feedLowPriority: new Set(),
  feedMuted: new Set(),
  feedShowHidden: false,
  openRoomId: null,
  convoRunning: false, convoSince: null,
  selfMxids: new Set(),
};
export const convosBySource = {};
export const runtime = { whatsapp: { mgmtRoomId: null }, imessage: { mgmtRoomId: null }, gmessages: { mgmtRoomId: null }, instagram: { mgmtRoomId: null }, linkedin: { mgmtRoomId: null }, twitter: { mgmtRoomId: null } };
export const feedModel = new Map();
export const feedManualHidden = new Set();
export const convoSeen = new Set();
export const convoNames = new Map();
export const convoNamePending = new Set();
