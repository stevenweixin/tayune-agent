import { useEffect, useRef, useState } from 'react';
import { ArrowUp, ArrowUpRight, ArrowCounterClockwise, X, Pause, Play, CaretDown } from '@phosphor-icons/react';
import { Orb } from './Orb';

const starters = ['Help me find a tea', 'How should I brew oolong in a mug?', 'Shipping & returns'];

function publicSources(sources) {
  const result = [];
  const seen = new Set();
  if (!Array.isArray(sources)) return result;
  for (const source of sources) {
    try {
      const url = new URL(source.url);
      // 来源只能指向店铺公开页面，不允许模型生成的脚本、外站或带密码的链接。
      if (url.protocol !== 'https:' || !['tayunetea.com', 'www.tayunetea.com'].includes(url.hostname)) continue;
      if (url.username || url.password || url.port || seen.has(url.href)) continue;
      if (typeof source.title !== 'string') continue;
      seen.add(url.href);
      result.push({ title: source.title, url: url.href });
    } catch { /* 外部返回中的无效链接不影响其余答案显示。 */ }
  }
  return result.slice(0, 10);
}

export function Widget({ config }) {
  const enabled = config.chatEnabled === true;
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [thinking, setThinking] = useState(false);
  const [notice, setNotice] = useState('');
  const [cooldown, setCooldown] = useState(0);
  const tokenRef = useRef('');
  const busyRef = useRef(false);
  const controllerRef = useRef(null);
  const launcherRef = useRef(null);
  const panelRef = useRef(null);
  const logRef = useRef(null);

  useEffect(() => () => controllerRef.current?.abort(), []);
  useEffect(() => {
    if (open) panelRef.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages, thinking, notice, open]);
  useEffect(() => {
    if (!cooldown) return;
    const timer = setTimeout(() => setCooldown(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  function closePanel() {
    setOpen(false);
    launcherRef.current?.focus();
  }

  function resetConversation() {
    // 禁止请求途中重置：浏览器取消不代表服务器已取消，不能借此重复收费调用。
    if (busyRef.current) return;
    tokenRef.current = '';
    setMessages([]);
    setDraft('');
    setNotice('');
  }

  async function post(path, body) {
    const controller = new AbortController();
    controllerRef.current = controller;
    const timer = setTimeout(() => controller.abort(), path === '/api/session' ? 10000 : 95000);
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (path === '/api/chat') headers.Authorization = 'Bearer ' + tokenRef.current;
      const response = await fetch(config.apiBase + path, {
        method: 'POST', headers, body: JSON.stringify(body), signal: controller.signal,
        credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      });
      if (response.status === 401) {
        tokenRef.current = '';
        throw new Error('Your conversation expired. Please send your question again to start a new one.');
      }
      if (response.status === 429) {
        const seconds = Number(response.headers.get('Retry-After'));
        setCooldown(Number.isFinite(seconds) && seconds > 0 ? Math.min(Math.ceil(seconds), 600) : 10);
        throw new Error('The assistant is busy or its request limit has been reached. Please wait and try later, or contact the shop.');
      }
      if (!response.ok) throw new Error('The tea assistant is unavailable right now. You can still browse and buy normally, or contact the shop.');
      return await response.json();
    } finally {
      clearTimeout(timer);
      controllerRef.current = null;
    }
  }

  async function send(question) {
    const clean = question.trim();
    if (!enabled || !clean || clean.length > 500 || busyRef.current || cooldown) return;
    busyRef.current = true;
    setThinking(true);
    setNotice('');
    setDraft('');
    setMessages(previous => [...previous, { role: 'user', text: clean }]);
    try {
      // 只有用户主动发送才申请会话并调用Agent；打开面板、动画和打字不调用模型。
      if (!tokenRef.current) {
        const session = await post('/api/session', {});
        if (typeof session.session_token !== 'string' || !session.session_token) throw new Error('Could not start a conversation. Please try again later.');
        tokenRef.current = session.session_token;
      }
      const result = await post('/api/chat', { message: clean });
      if (typeof result.answer !== 'string' || !result.answer.trim()) throw new Error('No answer was received. Please try again later.');
      // React按文本渲染回答，不使用dangerouslySetInnerHTML执行模型内容。
      setMessages(previous => [...previous, { role: 'assistant', text: result.answer, sources: publicSources(result.sources) }]);
      if (result.degraded) setNotice('Some information could not be verified. Please check the linked pages or contact the shop.');
    } catch (error) {
      setDraft(previous => previous || clean);
      setNotice(error.name === 'AbortError' || error instanceof TypeError
        ? 'The assistant could not be reached in time. Please try later or contact the shop. Your shopping is not affected.'
        : error.message);
      // 不自动重试：第一次请求可能已被服务器处理，重试可能重复消耗日额度。
    } finally {
      busyRef.current = false;
      setThinking(false);
    }
  }

  return <div className={'assistant-widget' + (config.homePage ? ' widget-home' : '')}>
    {open && <section ref={panelRef} tabIndex={-1} className="chat-panel" role="dialog" aria-label="TAYUNE tea companion" onKeyDown={event => { if (event.key === 'Escape') closePanel(); }}>
      <header className="chat-header">
        <div><span className="chat-brand">TAYUNE</span><h2>Tea companion</h2></div>
        <div className="chat-actions">
          <button className="quiet-icon" onClick={() => setPaused(value => !value)} aria-label={paused ? 'Resume motion' : 'Pause motion'} title={paused ? 'Resume motion' : 'Pause motion'}>{paused ? <Play size={16} /> : <Pause size={16} />}</button>
          <button className="quiet-icon" aria-label="New conversation" title="New conversation" disabled={thinking} onClick={resetConversation}><ArrowCounterClockwise size={18} /></button>
          <button className="quiet-icon" aria-label="Close conversation" onClick={closePanel}><X size={19} /></button>
        </div>
      </header>
      <div className="assistant-notice">{enabled ? 'AI assistant · Please verify important details with the shop.' : 'Chat is not enabled yet. No messages are being sent.'}</div>
      <div className="chat-body" ref={logRef} role="log" aria-live="polite" aria-relevant="additions text">
        {messages.length === 0 ? <div className="welcome">
          <span className="welcome-label">A MOMENT FOR TEA</span>
          <h3>Your next cup<br />starts here.</h3>
          <p>{enabled ? <>Tell me what you enjoy.<br />We will take it from there.</> : <>Our tea companion is on its way.<br />For now, explore the shop or ask us directly.</>}</p>
          {enabled ? <div className="starters">{starters.map(text => <button key={text} onClick={() => send(text)} disabled={thinking || cooldown > 0}>{text}<ArrowUpRight size={17} /></button>)}</div> : <div className="coming-soon-links">
            <a href={config.shopUrl}>Explore our teas <ArrowUpRight size={17} /></a>
            <a href={config.contactUrl}>Contact the shop <ArrowUpRight size={17} /></a>
          </div>}
        </div> : messages.map((message, index) => <div className={'message message-' + message.role} key={index}>
          <span className="message-name">{message.role === 'user' ? 'YOU' : 'TAYUNE'}</span>
          <p>{message.text}</p>
          {message.sources?.length > 0 && <div className="sources" aria-label="Sources">{message.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.title}<ArrowUpRight size={14} /></a>)}</div>}
        </div>)}
        {thinking && <div className="thinking" role="status"><span /><span /><span /><span className="thinking-label">Checking your question...</span></div>}
        {notice && <div className="offline-notice" role="status"><p>{notice}</p><a href={config.contactUrl} target="_blank" rel="noopener noreferrer">Contact the shop <ArrowUpRight size={14} /></a></div>}
      </div>
      <form className="chat-composer" onSubmit={event => { event.preventDefault(); send(draft); }}>
        <label className="sr-only" htmlFor="tea-question">Your tea question</label>
        <div className="input-shell">
          <textarea id="tea-question" disabled={!enabled} placeholder={enabled ? 'Ask about tea, brewing, or the shop...' : 'Chat coming soon'} rows={2} maxLength={500} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(draft); }
          }} />
          <button type="submit" aria-label="Send message" disabled={!enabled || !draft.trim() || thinking || cooldown > 0}><ArrowUp size={20} /></button>
        </div>
        <div className="composer-note"><span>{cooldown ? `Please wait ${cooldown}s before retrying.` : 'No personal or payment details, please.'}</span><span>{draft.length}/500</span></div>
        {enabled && <div className="privacy-note">AI provider: DeepSeek. Conversation memory expires after 24 hours of inactivity. <a href={config.privacyUrl} target="_blank" rel="noopener noreferrer">Privacy</a></div>}
      </form>
    </section>}
    <div className="launcher-row">
      <button ref={launcherRef} className="orb-launcher" aria-label={open ? 'Minimize tea companion' : 'Open tea companion'} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(value => !value)}>
        <span className="orb-visual">
          <Orb paused={paused} thinking={thinking} />
          {open && <span className="launcher-chevron"><CaretDown size={16} /></span>}
          <span className="orb-fallback">Tea</span>
        </span>
      </button>
    </div>
  </div>;
}
