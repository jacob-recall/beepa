import assert from 'node:assert/strict';
import {markdownParts} from '../../master/control-core.mjs';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';
const answer="Here's what this interface supports:\n\n**Reading & monitoring**\n- View lists.\n\n**Sending**\n- Prepare `exact text` for review.\n\n*No automatic confirmation.*";
const rendered=markdownParts(answer)[0];
assert(!rendered.text.includes('**'));assert(!rendered.text.includes('`'));
assert.deepEqual(rendered.entities.map(e=>[e.type,rendered.text.slice(e.offset,e.offset+e.length)]),[['bold','Reading & monitoring'],['bold','Sending'],['code','exact text'],['italic','No automatic confirmation.']]);
const hostile=markdownParts('😀 **A & B <admin>**\n<script>literal</script>\n```js\n**literal** & <tag>\n```\n# Heading')[0];
assert.equal(hostile.entities[0].offset,3);assert(hostile.text.includes('<script>literal</script>'));
assert(hostile.entities.some(e=>e.type==='pre'&&hostile.text.slice(e.offset,e.offset+e.length)==='**literal** & <tag>\n'));
assert(hostile.entities.some(e=>e.type==='bold'&&hostile.text.slice(e.offset,e.offset+e.length)==='Heading'));
assert.equal(markdownParts('unmatched **marker & <tag>')[0].text,'unmatched **marker & <tag>');
const long='😀'.repeat(1749)+'**'+('bold 😀 '.repeat(800))+'**';
const parts=markdownParts(long);assert(parts.length>2);assert.equal(parts.map(p=>p.text).join(''),long.replaceAll('**',''));
for(const part of parts){assert(part.text.length<=3500);assert(!/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/.test(part.text));for(const e of part.entities){assert(e.offset>=0&&e.length>0&&e.offset+e.length<=part.text.length);}}
const h=acceptanceFixture(async()=>({content:answer}));try{
 await h.ask('What can you do?','formatted-answer');const payload=h.sent.at(-1);
 assert(payload.entities.some(e=>e.type==='bold'&&payload.text.slice(e.offset,e.offset+e.length)==='Sending'));
 assert.equal(payload.parse_mode,undefined);
 h.core.enqueue('literal-review','Send exactly: **literal** `text`');await h.core.flush();
 assert(h.sent.at(-1).text.includes('**literal** `text`'));assert.equal(h.sent.at(-1).entities,undefined);
 h.core.enqueue('long-formatted',long,null,null,{markdown:true});await h.core.flush();
 for(const p of h.sent.filter(p=>p.text.startsWith('['))){assert(p.text.length<4096);for(const e of p.entities||[]){assert(e.offset>=p.text.indexOf('\n')+1);assert(e.offset+e.length<=p.text.length);}}
 assert.equal(h.writes.length,0);
}finally{h.close();}
console.log('Telegram formatting: actual model reply entities, bold/headings/italic/code, literal reviews, malformed markup, UTF-16 emoji offsets and multipart spans passed.');
