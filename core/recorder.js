/*
 * MIMIC recorder — injected into every document of the browsing context.
 *
 * It does two jobs:
 *   1. observe real user/agent interactions (click, fill, select, check, Enter)
 *   2. describe each target well enough to rebuild a stable selector later
 *
 * The descriptor is deliberately generous: testid, authored id, accessible role +
 * accessible name, form label, visible text, a uniqueness-checked CSS path and an
 * absolute XPath. The compiler decides which of those become the step's selector
 * ladder; a human never has to read this file.
 */
(() => {
  if (window.__mimicRecorderInstalled) return;
  window.__mimicRecorderInstalled = true;

  const MAX_EVENTS = 2000;
  const LOG = [];

  function emit(event) {
    event.ts = Date.now();
    if (LOG.length >= MAX_EVENTS) LOG.shift();
    LOG.push(event);
    try {
      if (typeof window.__mimicEmit === 'function') window.__mimicEmit(event);
    } catch (err) {
      /* the host binding is best-effort live progress; the buffer is authoritative */
    }
  }

  // The runner drains this at "stop" — a single source of truth, so a dropped
  // binding callback can never silently lose a step.
  window.__mimicFlush = () => LOG.splice(0, LOG.length);

  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim().slice(0, 200);

  const escapeId = (value) => {
    try {
      return CSS.escape(value);
    } catch (err) {
      return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
    }
  };

  const AUTHORED_ID = /^[A-Za-z_][\w-]{0,63}$/;

  function visibleText(el) {
    const text = (el.innerText || el.textContent || '').trim();
    return clean(text);
  }

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : '';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'button';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'search') return 'searchbox';
      if (type === 'number') return 'spinbutton';
      if (type === 'range') return 'slider';
      if (['text', 'email', 'tel', 'url', 'password'].includes(type)) return 'textbox';
      return '';
    }
    return '';
  }

  function accName(el) {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => clean(node.innerText || node.textContent))
        .filter(Boolean)
        .join(' ');
      if (text) return clean(text);
    }
    if (el.labels && el.labels.length) {
      const text = Array.from(el.labels)
        .map((label) => clean(label.innerText || label.textContent))
        .filter(Boolean)
        .join(' ');
      if (text) return clean(text);
    }
    const tag = el.tagName.toLowerCase();
    if (['button', 'a', 'summary'].includes(tag)) {
      const text = visibleText(el);
      if (text) return text;
    }
    if (tag === 'input' && ['submit', 'button', 'reset'].includes((el.getAttribute('type') || '').toLowerCase())) {
      const value = clean(el.value);
      if (value) return value;
    }
    const title = clean(el.getAttribute('title'));
    if (title) return title;
    const img = el.querySelector ? el.querySelector('img[alt]') : null;
    if (img) {
      const alt = clean(img.getAttribute('alt'));
      if (alt) return alt;
    }
    return '';
  }

  function labelText(el) {
    if (el.labels && el.labels.length) {
      const text = Array.from(el.labels)
        .map((label) => clean(label.innerText || label.textContent))
        .filter(Boolean)
        .join(' ');
      if (text) return clean(text);
    }
    const by = (el.getAttribute('aria-labelledby') || '').trim();
    if (by) {
      const text = by
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => clean(node.innerText || node.textContent))
        .filter(Boolean)
        .join(' ');
      if (text) return clean(text);
    }
    return '';
  }

  function uniqueCss(el) {
    if (el.id && AUTHORED_ID.test(el.id)) {
      const selector = '#' + escapeId(el.id);
      try {
        if (document.querySelectorAll(selector).length === 1) return selector;
      } catch (err) {
        /* fall through to a structural path */
      }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      if (node.id && AUTHORED_ID.test(node.id)) {
        const selector = '#' + escapeId(node.id);
        try {
          if (document.querySelectorAll(selector).length === 1) {
            parts.unshift(selector);
            break;
          }
        } catch (err) {
          /* keep walking */
        }
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      const candidate = parts.join(' > ');
      try {
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      } catch (err) {
        /* keep walking */
      }
      node = parent;
    }
    return parts.join(' > ');
  }

  function xpathOf(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(node.tagName.toLowerCase() + '[' + index + ']');
      node = node.parentElement;
    }
    return '/' + parts.join('/');
  }

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      nameAttr: el.getAttribute('name') || '',
      type: (el.getAttribute('type') || '').toLowerCase(),
      role: roleOf(el),
      accessibleName: accName(el),
      label: labelText(el),
      text: visibleText(el),
      testid:
        el.getAttribute('data-testid') ||
        el.getAttribute('data-test') ||
        el.getAttribute('data-test-id') ||
        el.getAttribute('data-qa') ||
        el.getAttribute('data-cy') ||
        '',
      placeholder: el.getAttribute('placeholder') || '',
      value: typeof el.value === 'string' ? el.value : '',
      checked: typeof el.checked === 'boolean' ? el.checked : undefined,
      disabled: !!el.disabled,
      css: uniqueCss(el),
      xpath: xpathOf(el),
    };
  }

  const CLICKABLE =
    'button, a[href], input, select, textarea, label, summary, [role=button], [role=link], ' +
    '[role=tab], [role=menuitem], [role=option], [role=checkbox], [role=switch], [onclick], ' +
    '[contenteditable=""], [contenteditable=true]';

  function pick(target) {
    if (!target || target.nodeType !== 1) return null;
    const found = target.closest(CLICKABLE);
    if (found) {
      if (found.tagName === 'LABEL') {
        const control = found.control || (found.htmlFor ? document.getElementById(found.htmlFor) : null);
        if (control) return control;
      }
      return found;
    }
    return target;
  }

  // ------------------------------------------------------------------ clicks
  let lastEnter = null;

  document.addEventListener(
    'click',
    (event) => {
      const el = pick(event.target);
      if (!el) return;
      // Enter on a field usually triggers a synthetic click on the submit button.
      // Keep the Enter (it carries intent) and drop the echo.
      if (lastEnter && lastEnter.el === el && Date.now() - lastEnter.ts < 900) {
        lastEnter = null;
        return;
      }
      const descriptor = describe(el);
      if (!descriptor) return;
      emit({ kind: 'click', target: descriptor });
    },
    true
  );

  // ------------------------------------------------------------------- input
  const pending = new WeakMap();

  function flushField(el) {
    const entry = pending.get(el);
    if (!entry) return;
    pending.delete(el);
    clearTimeout(entry.timer);
    const descriptor = entry.descriptor || describe(el);
    if (!descriptor) return;
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      emit({ kind: 'select', value: el.value, target: descriptor });
      return;
    }
    if (el.type === 'checkbox' || el.type === 'radio') {
      emit({ kind: 'check', checked: !!el.checked, target: descriptor });
      return;
    }
    const value = typeof el.value === 'string' ? el.value : clean(el.innerText);
    emit({ kind: 'fill', value, target: descriptor });
  }

  document.addEventListener(
    'input',
    (event) => {
      const el = event.target;
      if (!el || el.nodeType !== 1) return;
      if (el.type === 'checkbox' || el.type === 'radio') return;
      const tag = el.tagName.toLowerCase();
      if (!(tag === 'input' || tag === 'textarea' || el.isContentEditable)) return;
      const entry = pending.get(el) || { descriptor: describe(el), timer: null };
      entry.descriptor = describe(el);
      clearTimeout(entry.timer);
      entry.timer = setTimeout(() => flushField(el), 400);
      pending.set(el, entry);
    },
    true
  );

  document.addEventListener(
    'change',
    (event) => {
      const el = event.target;
      if (!el || el.nodeType !== 1) return;
      const tag = el.tagName.toLowerCase();
      if (!['select', 'input', 'textarea'].includes(tag)) return;
      if (!pending.has(el)) pending.set(el, { descriptor: describe(el), timer: null });
      flushField(el);
    },
    true
  );

  // ------------------------------------------------------------------ keydown
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.key !== 'Enter') return;
      const el = pick(event.target) || event.target;
      if (!el || el.nodeType !== 1) return;
      const descriptor = describe(el);
      if (!descriptor) return;
      lastEnter = { el, ts: Date.now() };
      emit({ kind: 'press', key: 'Enter', target: descriptor });
    },
    true
  );
})();
