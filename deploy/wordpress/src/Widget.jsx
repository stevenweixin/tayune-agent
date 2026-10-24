import { useEffect, useRef, useState } from 'react';
import { ArrowUp, ArrowUpRight, ArrowCounterClockwise, X, Pause, Play, CaretDown } from '@phosphor-icons/react';
import { Orb } from './Orb';
import { appendDelta, readChatStream, safeParts, CHAT_TIMEOUT_MS } from './chat_protocol';

const starters = ['Help me find a tea', 'How should I brew oolong in a mug?', 'Shipping & returns'];

export function Widget({ config }) {
  const enabled = config.chatEnabled === true;
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [thinking, setThinking] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [notice, setNotice] = useState('');
  const [cooldown, setCooldown] = useState(0);
  const tokenRef = useRef('');
  const busyRef = useRef(false);
  const controllerRef = useRef(null);
  const launcherRef = useRef(null);
  const panelRef = useRef(null);
  const logRef = useRef(null);
  const messageIdRef = useRef(0);
  const retryRef = useRef(null);
  const cooldownUntilRef = useRef(0);

  useEffect(() => () => controllerRef.current?.abort(), []);
  useEffect(() => {
    if (open) panelRef.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages, thinking, notice, open]);
  useEffect(() => {
    if (!cooldown) return;
    // 等待被延长时，数字可能暂时不变；interval仍会继续检查，不能因此卡住按钮。
    const timer = setInterval(() => setCooldown(Math.max(0,
      Math.ceil((cooldownUntilRef.current - Date.now()) / 1000))), 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  function closePanel() {
    setOpen(false);
    launcherRef.current?.focus();
  }

  function waitBeforeNext(seconds) {
    // 时间戳避免后台标签页的计时器变慢后，多等一整段倒计时；也不缩短已有等待。
    cooldownUntilRef.current = Math.max(cooldownUntilRef.current, Date.now() + seconds * 1000);
    setCooldown(Math.max(0, Math.ceil((cooldownUntilRef.current - Date.now()) / 1000)));
  }

  function resetConversation() {
    // 禁止请求途中重置：浏览器取消不代表服务器已取消，不能借此重复收费调用。
    if (busyRef.current) return;
    tokenRef.current = '';
    retryRef.current = null;
    setMessages([]);
    setDraft('');
    setNotice('');
  }

  async function post(path, body, onEvent) {
    const controller = new AbortController();
    controllerRef.current = controller;
    const timer = setTimeout(() => controller.abort(), path === '/api/session' ? 10000 : CHAT_TIMEOUT_MS);
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (path === '/api/chat') {
        headers.Authorization = 'Bearer ' + tokenRef.current;
        headers.Accept = 'text/event-stream';
        // 后端限制同会话两次开始至少相隔8秒；前端留1秒余量，不改变服务端规则。
        waitBeforeNext(9);
      }
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
        waitBeforeNext(Number.isFinite(seconds) && seconds > 0 ? Math.min(Math.ceil(seconds), 600) : 10);
        throw new Error('The assistant is busy or its request limit has been reached. Please wait and try later, or contact the shop.');
      }
      if (response.status === 409) {
        retryRef.current = null;
        throw new Error('That earlier request cannot be reused. Please send your question again to verify current information.');
      }
      if (!response.ok) throw new Error('The tea assistant is unavailable right now. You can still browse and buy normally, or contact the shop.');
      if (onEvent && response.headers.get('Content-Type')?.includes('text/event-stream')) {
        // 等到done才释放请求锁和超时计时器，不能收到响应头就当作完成。
        return await readChatStream(response, onEvent);
      }
      return await response.json();
    } finally {
      clearTimeout(timer);
      controllerRef.current = null;
    }
  }

  async function send(question) {
    const clean = question.trim();
    if (!enabled || !clean || clean.length > 500 || busyRef.current || cooldown || Date.now() < cooldownUntilRef.current) return;
    busyRef.current = true;
    setThinking(true);
    setNotice('');
    setDraft('');
    setMessages(previous => [...previous, { role: 'user', text: clean }]);
    const answerId = ++messageIdRef.current;
    let currentParts = [];
    let receivedStream = false;
    try {
      // 只有用户主动发送才申请会话并调用Agent；打开面板、动画和打字不调用模型。
      if (!tokenRef.current) {
        const session = await post('/api/session', {});
        if (typeof session.session_token !== 'string' || !session.session_token) throw new Error('Could not start a conversation. Please try again later.');
        tokenRef.current = session.session_token;
      }
      // 同会话、同问题在网络失败后沿用请求ID；服务端可重放结果而不再次扣次/调用模型。
      if (!retryRef.current || retryRef.current.text !== clean || retryRef.current.token !== tokenRef.current) {
        retryRef.current = { text: clean, token: tokenRef.current, id: crypto.randomUUID() };
      }
      const result = await post('/api/chat', { message: clean, request_id: retryRef.current.id }, ({ event, data }) => {
        if (event === 'meta') {
          receivedStream = true;
          setThinking(false);
          setStreaming(true);
          setMessages(previous => [...previous, { id: answerId, role: 'assistant', parts: [] }]);
        } else if (event === 'delta') {
          const nextParts = appendDelta(currentParts, data);
          currentParts = nextParts;
          setMessages(previous => previous.map(message => message.id === answerId
            ? { ...message, parts: nextParts } : message));
        }
      });
      // 兼容仍返回JSON的旧后端；新版本通过delta持续更新，不在done时整段替换。
      if (!result || typeof result !== 'object' || typeof result.degraded !== 'boolean') {
        throw new Error('The answer format was invalid. Please try later or contact the shop.');
      }
      if (!receivedStream) {
        if (typeof result.answer !== 'string' || !result.answer.trim()) throw new Error('No answer was received. Please try again later.');
        const parts = safeParts(result.answer_parts || [{ text: result.answer }]);
        if (!parts.some(part => part.text.trim())) throw new Error('No answer was received. Please try again later.');
        setMessages(previous => [...previous, { id: answerId, role: 'assistant', parts }]);
      }
      if (result.degraded) setNotice('Some information could not be verified. Please check the linked pages or contact the shop.');
      retryRef.current = null;
    } catch (error) {
      setMessages(previous => previous.map(message => message.id === answerId
        ? { ...message, incomplete: true } : message));
      setDraft(previous => previous || clean);
      setNotice(error.name === 'AbortError' || error instanceof TypeError
        ? 'The assistant could not be reached in time. Please try later or contact the shop. Your shopping is not affected.'
        : error.message);
      // 不自动重试；用户主动重试同问题时保留ID，改问题或重建会话时生成新ID。
    } finally {
      busyRef.current = false;
      setThinking(false);
      setStreaming(false);
    }
  }

  return <div className={'assistant-widget' + (config.homePage ? ' widget-home' : '')}>
    {open && <section ref={panelRef} tabIndex={-1} className="chat-panel" role="dialog" aria-label="TAYUNE tea companion" onKeyDown={event => { if (event.key === 'Escape') closePanel(); }}>
      <header className="chat-header">
        <div><span className="chat-brand">TAYUNE</span><h2>Tea companion</h2></div>
        <div className="chat-actions">
          <button className="quiet-icon" onClick={() => setPaused(value => !value)} aria-label={paused ? 'Resume motion' : 'Pause motion'} title={paused ? 'Resume motion' : 'Pause motion'}>{paused ? <Play size={16} /> : <Pause size={16} />}</button>
          <button className="quiet-icon" aria-label="New conversation" title="New conversation" disabled={thinking || streaming} onClick={resetConversation}><ArrowCounterClockwise size={18} /></button>
          <button className="quiet-icon" aria-label="Close conversation" onClick={closePanel}><X size={19} /></button>
        </div>
      </header>
      <div className="assistant-notice">{enabled ? 'AI assistant · Please verify important details with the shop.' : 'Chat is not enabled yet. No messages are being sent.'}</div>
      <div className="chat-body" ref={logRef} role="log" aria-live="polite" aria-busy={streaming} aria-relevant="additions text">
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
          {message.role === 'user' ? <p>{message.text}</p> : <div className="answer-text">
            {message.parts?.map((part, partIndex) => part.url
              ? <a key={partIndex} href={part.url} target="_blank" rel="noopener noreferrer">{part.text}</a>
              : <span key={partIndex}>{part.text}</span>)}
            {message.incomplete && <span className="incomplete-note"> Response interrupted; please verify before using it.</span>}
          </div>}
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
          <button type="submit" aria-label="Send message" disabled={!enabled || !draft.trim() || thinking || streaming || cooldown > 0}><ArrowUp size={20} /></button>
        </div>
        <div className="composer-note"><span>{cooldown ? `Please wait ${cooldown}s before sending another message.` : 'No personal or payment details, please.'}</span><span>{draft.length}/500</span></div>
        {enabled && <div className="privacy-note">AI provider: DeepSeek. Conversation memory expires after 24 hours of inactivity. <a href={config.privacyUrl} target="_blank" rel="noopener noreferrer">Privacy</a></div>}
      </form>
    </section>}
    <div className="launcher-row">
      <button ref={launcherRef} className="orb-launcher" aria-label={open ? 'Minimize tea companion' : 'Open tea companion'} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(value => !value)}>
        <span className="orb-visual">
          <Orb paused={paused} thinking={thinking || streaming} />
          {open && <span className="launcher-chevron"><CaretDown size={16} /></span>}
          <span className="orb-fallback">Tea</span>
        </span>
      </button>
    </div>
  </div>;
}
