// Pure display shaping: no session, DOM, transport or send imports.
function feedPreviewFromEvent(ev) {
  if (!ev || ev.type !== 'm.room.message' || !ev.content) return null;
  let content = ev.content;
  const rel = content['m.relates_to'];
  if (rel && rel.rel_type === 'm.replace') {        // edit: read m.new_content.body only
    content = content['m.new_content'];
    if (!content) return null;
  }
  const mt = content.msgtype;
  let body;
  if ((mt === 'm.text' || mt === 'm.notice') && typeof content.body === 'string') {
    body = content.body;                            // text/notice: the real (sanitized-on-render) body
  } else if (mt === 'm.image') { body = 'Photo'; }  // media: static label ONLY (never the filename)
  else if (mt === 'm.video') { body = 'Video'; }
  else if (mt === 'm.audio') { body = 'Audio'; }
  else if (mt === 'm.file')  { body = 'File'; }
  else { return null; }                             // anything else is not a previewable message
  return { body, ts: typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0 };
}

function feedRelTime(ts) {
  const d = Date.now() - ts;
  if (d < 0) return '';
  if (d < 60000) return 'now';
  if (d < 3600000) return Math.floor(d / 60000) + 'm';
  if (d < 86400000) return Math.floor(d / 3600000) + 'h';
  if (d < 604800000) return Math.floor(d / 86400000) + 'd';
  const dt = new Date(ts);
  return (dt.getMonth() + 1) + '/' + dt.getDate();
}

export { feedPreviewFromEvent, feedRelTime };
