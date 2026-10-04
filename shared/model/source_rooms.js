import { ROOMID_RE } from '../matrix/client.js';
import { SOURCES } from './source_catalog.js';

export function sourceRoomIds(source, rooms, excluded = new Set()) {
  const roots = Object.values(rooms).filter(room => room.isSpace &&
    typeof room.name === 'string' && room.name.startsWith(source.spaceName));
  const seen = new Set(roots.map(room => room.id));
  const pending = [];
  for (const root of roots) {
    for (const childId of root.children || []) {
      const child = rooms[childId];
      if (source.childSpaceNames && (!child?.isSpace || !source.childSpaceNames.includes(child.name))) continue;
      pending.push(childId);
    }
  }
  const result = [];
  while (pending.length && seen.size < 10000) {
    const roomId = pending.pop();
    if (seen.has(roomId) || !ROOMID_RE.test(roomId) || excluded.has(roomId)) continue;
    seen.add(roomId);
    const room = rooms[roomId];
    if (!room) continue;
    if (room.isSpace) {
      if (SOURCES.some(other => other.kind === 'source' && other.id !== source.id &&
          typeof room.name === 'string' && room.name.startsWith(other.spaceName))) continue;
      if (source.childSpaceNames && !source.childSpaceNames.includes(room.name)) continue;
      for (const childId of room.children || []) pending.push(childId);
    } else {
      result.push(roomId);
    }
  }
  return result;
}
