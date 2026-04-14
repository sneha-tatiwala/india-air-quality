(function () {
  'use strict';

  const API_CHAT = 'http://localhost:8000/api/chat';

  // ── State ──────────────────────────────────────────────────────────
  let isOpen    = false;
  let isWaiting = false;
  const history = [];

  // ── Elements ───────────────────────────────────────────────────────
  const widget   = document.getElementById('cw');
  const bubble   = document.getElementById('cwBubble');
  const panel    = document.getElementById('cwPanel');
  const closeBtn = document.getElementById('cwClose');
  const messages = document.getElementById('cwMessages');
  const input    = document.getElementById('cwInput');
  const sendBtn  = document.getElementById('cwSend');

  if (!widget || !bubble || !panel) return;

  // ── Open / close ───────────────────────────────────────────────────
  function openChat() {
    isOpen = true;
    widget.classList.add('chat-open');
    panel.setAttribute('aria-hidden', 'false');
    bubble.setAttribute('aria-expanded', 'true');
    setTimeout(() => input.focus(), 220);
  }

  function closeChat() {
    isOpen = false;
    widget.classList.remove('chat-open');
    panel.setAttribute('aria-hidden', 'true');
    bubble.setAttribute('aria-expanded', 'false');
  }

  bubble.addEventListener('click', () => isOpen ? closeChat() : openChat());
  closeBtn.addEventListener('click', closeChat);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && isOpen) closeChat(); });

  // ── Scroll to bottom ───────────────────────────────────────────────
  function scrollBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  // ── Append a message bubble ────────────────────────────────────────
  function appendMessage(role, text) {
    // Remove chips once user sends first message
    const chips = messages.querySelector('.cw__chips');
    if (chips && role === 'user') chips.remove();

    const wrap = document.createElement('div');
    wrap.className = `cw__msg cw__msg--${role === 'user' ? 'user' : role === 'error' ? 'error' : 'ai'}`;
    const p = document.createElement('p');
    p.textContent = text;
    wrap.appendChild(p);
    messages.appendChild(wrap);
    scrollBottom();
  }

  // ── Typing indicator ───────────────────────────────────────────────
  let typingEl = null;

  function showTyping() {
    typingEl = document.createElement('div');
    typingEl.className = 'cw__typing';
    typingEl.innerHTML = `<div class="cw__typing-dots"><span></span><span></span><span></span></div>`;
    messages.appendChild(typingEl);
    scrollBottom();
  }

  function hideTyping() {
    if (typingEl) { typingEl.remove(); typingEl = null; }
  }

  // ── Lock / unlock input ────────────────────────────────────────────
  function setWaiting(val) {
    isWaiting        = val;
    input.disabled   = val;
    sendBtn.disabled = val;
  }

  // ── Send message ───────────────────────────────────────────────────
  async function sendMessage(text) {
    text = (text || input.value).trim();
    if (!text || isWaiting) return;

    input.value = '';
    appendMessage('user', text);
    history.push({ role: 'user', content: text });
    setWaiting(true);
    showTyping();

    try {
      const res = await fetch(API_CHAT, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ messages: history }),
      });

      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `Error ${res.status}`);

      const reply = data.content ?? '';
      history.push({ role: 'assistant', content: reply });
      hideTyping();
      appendMessage('assistant', reply);

    } catch (err) {
      hideTyping();
      history.pop();
      appendMessage('error', 'Could not reach the AI service — please try again.');
      console.error('[chat]', err);
    }

    setWaiting(false);
    input.focus();
  }

  // ── Event bindings ─────────────────────────────────────────────────
  sendBtn.addEventListener('click', () => sendMessage());
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // Suggestion chips
  messages.addEventListener('click', e => {
    const chip = e.target.closest('.cw__chip');
    if (chip && !isWaiting) {
      if (!isOpen) openChat();
      sendMessage(chip.textContent.trim());
    }
  });

})();
