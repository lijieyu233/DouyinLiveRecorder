/**
 * UI 基础件：DOM 构造、提示条、确认框、按钮忙碌态、列表增量渲染、格式化。
 *
 * 统一用 h() 构建 DOM 而不是拼 innerHTML —— 主播名/文件名/URL 都是外部数据，
 * 字符串拼接很容易引入注入与转义问题。
 */
import { icon } from './icons.js';

/**
 * 极简 hyperscript。
 * @param {string} tag 形如 'div' / 'div.card' / 'button.btn.primary'
 * @param {object} [props]
 * @param {...(Node|string|Array|null)} children
 */
export function h(tag, props, ...children) {
  const [name, ...classes] = String(tag).split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');

  // 第二个参数只有在「是普通对象」时才当作属性表。
  // 否则 h('div', someNode) / h('div', 'text') / h('div', null) 这类调用
  // 会被误解析成属性（DOM 节点带数字索引属性，会产生 '0' 这种非法属性名）。
  const looksLikeProps = props !== null && props !== undefined
    && typeof props === 'object' && !Array.isArray(props)
    && !(props instanceof Node) && !props.nodeType;
  if (!looksLikeProps) {
    if (props !== undefined) children.unshift(props);
    props = {};
  }

  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class' || key === 'className') {
      el.className = [el.className, value].filter(Boolean).join(' ');
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(el.style, value);
    } else if (key === 'dataset') {
      Object.assign(el.dataset, value);
    } else if (key === 'html') {
      el.innerHTML = value;
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'selected') {
      el[key] = value;
    } else if (key in el && key !== 'list' && typeof value !== 'object') {
      try {
        el[key] = value;
      } catch {
        el.setAttribute(key, String(value));
      }
    } else {
      el.setAttribute(key, String(value));
    }
  }

  append(el, children);
  return el;
}

function append(el, children) {
  el.append(...flatten(children));
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/**
 * 安全地替换子节点。
 *
 * 注意：原生 ``replaceChildren(null)`` / ``append(null)`` 会插入字符串 "null"，
 * 而条件渲染里 ``cond ? node : null`` 非常常见。统一走这里，避免界面上冒出多余的 null。
 */
export function fill(el, ...children) {
  el.replaceChildren(...flatten(children));
  return el;
}

export function add(el, ...children) {
  el.append(...flatten(children));
  return el;
}

function flatten(children) {
  return children
    .flat(6)
    .filter((child) => child !== null && child !== undefined && child !== false);
}

export function svgIcon(name, size = 18, cls = '') {
  const span = document.createElement('span');
  span.className = 'icon-wrap';
  span.innerHTML = icon(name, size, cls);
  return span.firstElementChild;
}

// ---------------------------------------------------------------- 格式化
export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size < 10 ? 1 : 0)} ${units[index]}`;
}

export function formatTime(ts) {
  if (!ts) return '—';
  const date = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function relativeTime(ts) {
  if (!ts) return '—';
  const diff = Date.now() / 1000 - ts;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return formatTime(ts);
}

export function debounce(fn, ms = 220) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ---------------------------------------------------------------- 提示条
let toastHost = null;

function ensureToastHost() {
  if (!toastHost) {
    toastHost = h('div.toast-host', { role: 'status', 'aria-live': 'polite' });
    document.body.append(toastHost);
  }
  return toastHost;
}

/**
 * 右下角提示条。
 * @param {string} message
 * @param {{type?:'info'|'success'|'warning'|'error', title?:string, timeout?:number}} [options]
 */
export function toast(message, options = {}) {
  const { type = 'info', title = '', timeout } = options;
  const icons = { info: 'about', success: 'check', warning: 'alert', error: 'alert' };
  const life = timeout ?? (type === 'error' ? 8000 : type === 'warning' ? 5200 : 3200);

  const node = h(`div.toast.toast-${type}`,
    h('span.toast-icon', svgIcon(icons[type], 18)),
    h('div.toast-body',
      title ? h('div.toast-title', title) : null,
      h('div.toast-message', message),
    ),
    h('button.toast-close', { 'aria-label': '关闭', onclick: () => dismiss() }, svgIcon('close', 14)),
  );
  ensureToastHost().append(node);
  requestAnimationFrame(() => node.classList.add('is-in'));

  let timer = setTimeout(() => dismiss(), life);
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  node.addEventListener('mouseleave', () => {
    timer = setTimeout(() => dismiss(), 1400);
  });

  function dismiss() {
    clearTimeout(timer);
    node.classList.remove('is-in');
    node.classList.add('is-out');
    setTimeout(() => node.remove(), 220);
  }
  return dismiss;
}

// ---------------------------------------------------------------- 模态
let modalHost = null;

function ensureModalHost() {
  if (!modalHost) {
    modalHost = h('div.modal-host');
    document.body.append(modalHost);
  }
  return modalHost;
}

function openModal({ title, body, actions, width = 460 }) {
  const host = ensureModalHost();
  const card = h('div.modal-card', { style: { maxWidth: `${width}px` } });
  const overlay = h('div.modal-overlay', { style: { position: 'fixed', inset: '0' } });

  const close = (result) => {
    document.removeEventListener('keydown', onKey);
    overlay.classList.remove('is-in');
    setTimeout(() => {
      overlay.remove();
      if (!host.childElementCount) host.classList.remove('is-open');
    }, 160);
    resolvePromise?.(result);
  };
  overlay.addEventListener('mousedown', (event) => {
    if (event.target === overlay) close(null);
  });
  const onKey = (event) => {
    if (event.key === 'Escape') close(null);
  };
  document.addEventListener('keydown', onKey);

  let resolvePromise = null;
  const promise = new Promise((resolve) => {
    resolvePromise = resolve;
  });

  card.append(
    h('div.modal-head',
      h('div.modal-title', title),
      h('button.icon-btn', { 'aria-label': '关闭', onclick: () => close(null) }, svgIcon('close', 16)),
    ),
    h('div.modal-body', body),
    h('div.modal-foot', ...(actions || [])),
  );
  overlay.append(card);
  host.append(overlay);
  host.classList.add('is-open');
  requestAnimationFrame(() => overlay.classList.add('is-in'));
  setTimeout(() => card.querySelector('input,button.primary,textarea')?.focus(), 60);
  return { promise, close };
}

/**
 * 危险操作确认。
 * @returns {Promise<boolean>}
 */
export async function confirmDialog({ title, message, confirmText = '确认', cancelText = '取消', danger = false }) {
  let dialog;
  dialog = openModal({
    title,
    body: h('div.modal-text', message),
    actions: [
      h('button.btn.ghost', { onclick: () => dialog.close(false) }, cancelText),
      h(`button.btn.${danger ? 'danger' : 'primary'}`, { onclick: () => dialog.close(true) }, confirmText),
    ],
  });
  return (await dialog.promise) === true;
}

/** 文本输入弹窗，返回输入值或 null。 */
export async function promptDialog({ title, label, value = '', placeholder = '', multiline = false }) {
  const field = multiline
    ? h('textarea.input', { rows: 4, placeholder, value })
    : h('input.input', { type: 'text', placeholder, value });
  let dialog;
  dialog = openModal({
    title,
    body: h('label.field', label ? h('span.field-label', label) : null, field),
    actions: [
      h('button.btn.ghost', { onclick: () => dialog.close(null) }, '取消'),
      h('button.btn.primary', { onclick: () => dialog.close(field.value) }, '确定'),
    ],
  });
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !multiline) dialog.close(field.value);
  });
  const result = await dialog.promise;
  return result;
}

// ---------------------------------------------------------------- 按钮忙碌
/** 给按钮加转圈并禁用，直到回调结束。 */
export async function withBusy(button, task) {
  if (!button || button.dataset.busy === '1') return undefined;
  const original = button.innerHTML;
  button.dataset.busy = '1';
  button.disabled = true;
  button.classList.add('is-busy');
  clear(button);
  button.append(h('span.spinner', { 'aria-hidden': 'true' }), h('span', '处理中'));
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.classList.remove('is-busy');
    delete button.dataset.busy;
    button.innerHTML = original;
  }
}

// ---------------------------------------------------------------- 增量列表
/**
 * 按 key 增量更新列表：只重排/新增/删除变化的节点，
 * 既不会丢滚动位置，也不会让正在播放的媒体元素被重建。
 *
 * @param {HTMLElement} container
 * @param {Array<object>} items
 * @param {{key:(item:any)=>string, create:(item:any)=>HTMLElement,
 *          update?:(node:HTMLElement, item:any)=>void, onRemove?:(node:HTMLElement)=>void}} spec
 */
export function keyedList(container, items, spec) {
  const { key, create, update, onRemove } = spec;
  const existing = new Map();
  for (const node of Array.from(container.children)) {
    existing.set(node.dataset.key, node);
  }

  const seen = new Set();
  let cursor = container.firstElementChild;

  for (const item of items) {
    const id = key(item);
    seen.add(id);
    let node = existing.get(id);
    if (!node) {
      node = create(item);
      node.dataset.key = id;
      container.insertBefore(node, cursor);
    } else if (node !== cursor) {
      container.insertBefore(node, cursor);
    } else {
      cursor = cursor.nextElementSibling;
    }
    update?.(node, item);
    if (node === cursor) cursor = cursor.nextElementSibling;
  }

  for (const [id, node] of existing) {
    if (!seen.has(id)) {
      onRemove?.(node);
      node.remove();
    }
  }
  return container;
}

/** 骨架屏占位。 */
export function skeleton(rows = 3, className = '') {
  return h(`div.skeleton-list.${className}`,
    ...Array.from({ length: rows }, () => h('div.skeleton-row', h('div.skeleton-bar'))),
  );
}

/** 空状态。 */
export function emptyState({ title, description, actionLabel, onAction, iconName = 'spark' }) {
  return h('div.empty-state',
    h('div.empty-icon', svgIcon(iconName, 26)),
    h('div.empty-title', title),
    description ? h('div.empty-desc', description) : null,
    actionLabel ? h('button.btn.primary', { onclick: onAction }, actionLabel) : null,
  );
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板', { type: 'success', timeout: 1600 });
    return true;
  } catch {
    toast('复制失败，请手动选择文本', { type: 'warning' });
    return false;
  }
}

/** 平台名 → 稳定的配色索引，用于徽章着色。 */
export function platformTone(name = '') {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 997;
  return `tone-${hash % 6}`;
}
