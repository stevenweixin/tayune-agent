import { test } from 'node:test';
import assert from 'node:assert/strict';
import { appendDelta, readChatStream, safeParts, safeShopUrl, CHAT_TIMEOUT_MS } from '../deploy/wordpress/src/chat_protocol.js';

test('chat timeout covers generation and bounded stream delivery with network margin', () => {
  assert.equal(CHAT_TIMEOUT_MS, 115000);
  assert.ok(CHAT_TIMEOUT_MS >= 85000 + (12000 / 12) * 20 + 5000);
});

const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
function responseFrom(text, step = 1) {
  const bytes = new TextEncoder().encode(text);
  let position = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (position >= bytes.length) return controller.close();
      controller.enqueue(bytes.slice(position, position + step));
      position += step;
    },
  }));
}

test('UTF-8 characters, CRLF boundaries and multiple events survive arbitrary packets', async () => {
  const text = event('meta', {request_id:'fixture'}) + event('delta', {index:0,text:'茶🌿'}) +
    event('delta', {index:0,text:' is tea'}) + event('done', {request_id:'fixture',degraded:false});
  let parts = [];
  const result = await readChatStream(responseFrom(text.replaceAll('\n','\r\n')), frame => {
    if (frame.event === 'delta') parts = appendDelta(parts, frame.data);
  });
  assert.equal(parts[0].text, '茶🌿 is tea');
  assert.equal(result.degraded, false);
});

test('links remain one inline part while their text grows', () => {
  const url = 'https://tayunetea.com/product/example/';
  let parts = appendDelta([], {index:0,text:'Try '});
  parts = appendDelta(parts,{index:1,text:'Dong ',url});
  parts = appendDelta(parts,{index:1,text:'Ding',url});
  parts = appendDelta(parts,{index:2,text:' today.'});
  assert.equal(parts.length,3);
  assert.equal(parts[1].text,'Dong Ding');
  assert.equal(parts[1].url,url);
});

test('unsafe links become literal text, never executable markup', () => {
  for (const url of ['javascript:alert(1)','https://evil.example/','https://user:pass@tayunetea.com/',
    'https://tayunetea.com:8443/','https://tayunetea.com.evil.example/']) assert.equal(safeShopUrl(url),null);
  const parts = safeParts([{text:'<img onerror=alert(1)>',url:'javascript:alert(1)'}]);
  assert.equal(parts[0].url,null);
  assert.equal(parts[0].text,'<img onerror=alert(1)>');
});

test('out-of-order fragments and changing targets are rejected', () => {
  assert.throws(()=>appendDelta([],{index:1,text:'wrong'}));
  const parts = appendDelta([],{index:0,text:'Tea',url:'https://tayunetea.com/tea/'});
  assert.throws(()=>appendDelta(parts,{index:0,text:'changed',url:'https://tayunetea.com/other/'}));
});

test('early EOF is not reported as a successful answer', async () => {
  const text = event('meta',{}) + event('delta',{index:0,text:'Only half'});
  await assert.rejects(readChatStream(responseFrom(text),()=>{}),/interrupted/);
});

test('invalid JSON and done-before-text are rejected', async () => {
  for (const text of ['event: meta\ndata: broken\n\n', event('done',{})]) {
    await assert.rejects(readChatStream(responseFrom(text),()=>{}));
  }
});

test('received text is available before the completion event', async () => {
  let push;
  const response = new Response(new ReadableStream({start(controller){push=controller;}}));
  let seen = '';
  const completed = readChatStream(response,frame=>{if(frame.event==='delta') seen+=frame.data.text;});
  const encode = text=>new TextEncoder().encode(text);
  push.enqueue(encode(event('meta',{})+event('delta',{index:0,text:'First'})));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(seen,'First');
  push.enqueue(encode(event('delta',{index:0,text:' second'})+event('done',{degraded:false})));
  push.close();
  await completed;
  assert.equal(seen,'First second');
});
