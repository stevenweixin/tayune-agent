// 只消费已核验的答案事件；不解析模型HTML，不自动重试请求。
// 后端85秒处理 + 最多约20秒分段交付（12000字符）+ 10秒网络余量。
export const CHAT_TIMEOUT_MS = 115000;

export function safeShopUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['tayunetea.com', 'www.tayunetea.com'].includes(url.hostname)) return null;
    if (url.username || url.password || url.port) return null;
    return url.href;
  } catch { return null; }
}

export function safeParts(parts) {
  if (!Array.isArray(parts)) throw new Error('Invalid answer format.');
  const result = [];
  for (const part of parts) {
    if (typeof part.text !== 'string') throw new Error('Invalid answer format.');
    result.push({ text: part.text, url: safeShopUrl(part.url) });
  }
  return result;
}

export function appendDelta(parts, delta) {
  if (!Number.isInteger(delta.index) || delta.index < 0 || delta.index > parts.length ||
      typeof delta.text !== 'string' || !delta.text) throw new Error('Invalid answer fragment.');
  const result = [...parts];
  const url = safeShopUrl(delta.url);
  if (delta.index === result.length) {
    result.push({ text: delta.text, url });
  } else {
    const previous = result[delta.index];
    if (delta.index !== result.length - 1 || previous.url !== url) throw new Error('Invalid answer order.');
    result[delta.index] = { text: previous.text + delta.text, url };
  }
  return result;
}

export async function readChatStream(response, onEvent) {
  if (!response.body) throw new Error('The response stream is unavailable.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let bytes = 0;
  let started = false;
  let hasText = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 131072) throw new Error('The answer is too large.');
      // 网络包不等于SSE事件，也可能从一个中文字的UTF-8字节中间断开。
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      if (buffer.length > 65536) throw new Error('Invalid response stream.');
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let event = '';
        const dataLines = [];
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
        }
        if (dataLines.length) {
          const data = JSON.parse(dataLines.join('\n'));
          if (event === 'meta' && !started) started = true;
          else if (event === 'delta' && started) hasText = true;
          else if (event === 'done' && started && hasText) return data;
          else throw new Error('Invalid response stream.');
          onEvent({ event, data });
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
    // 不能把断线后留下的半截答案当成成功，更不能自动重试导致重复计费。
    throw new Error('The answer was interrupted. Please try later or contact the shop.');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
