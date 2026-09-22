/**
 * 与后端（dylr HTTP 接口）通信。
 *
 * - 普通请求走 fetch，统一带上 X-DYLR-Token；
 * - 实时状态走 SSE（EventSource 无法自定义请求头，所以令牌放查询串）；
 * - 断线自动重连，并把连接状态广播出去，界面据此显示顶部提示条。
 */

const TOKEN_KEY = 'dylr.session';

export class ApiClient extends EventTarget {
  constructor() {
    super();
    this.baseUrl = '';
    this.token = '';
    this.connected = false;
    this._source = null;
    this._lastSeq = 0;
    this._retry = 0;
    this._retryTimer = null;
  }

  /** 由主进程注入会话信息（地址 + 令牌）。 */
  configure(baseUrl, token) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.token = token || '';
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify({ baseUrl: this.baseUrl, token: this.token }));
  }

  get configured() {
    return Boolean(this.baseUrl);
  }

  async request(path, { method = 'GET', body, timeout = 20000 } = {}) {
    if (!this.baseUrl) throw new Error('后端地址未配置');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(this.baseUrl + path, {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-DYLR-Token': this.token,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await response.text();
      const data = text ? safeJson(text) : {};
      if (!response.ok) {
        throw new ApiError(data.error || `请求失败（${response.status}）`, response.status, data);
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new ApiError('请求超时，后端可能正忙', 0);
      if (error instanceof ApiError) throw error;
      throw new ApiError(`无法连接后端：${error.message}`, 0);
    } finally {
      clearTimeout(timer);
    }
  }

  get = (path) => this.request(path);
  post = (path, body) => this.request(path, { method: 'POST', body });
  put = (path, body) => this.request(path, { method: 'PUT', body });
  patch = (path, body) => this.request(path, { method: 'PATCH', body });
  del = (path) => this.request(path, { method: 'DELETE' });

  /** 媒体直链（给 <video src> 用），带令牌。 */
  mediaUrl(path) {
    const params = new URLSearchParams({ path, token: this.token });
    return `${this.baseUrl}/api/media?${params.toString()}`;
  }

  // ---------------------------------------------------------------- SSE
  connectEvents() {
    if (!this.baseUrl || this._source) return;
    const params = new URLSearchParams({ token: this.token, since: String(this._lastSeq) });
    const source = new EventSource(`${this.baseUrl}/api/events?${params.toString()}`);
    this._source = source;

    const types = ['log', 'task', 'control', 'stats', 'files', 'notice'];
    types.forEach((type) => {
      source.addEventListener(type, (event) => {
        const seq = Number(event.lastEventId || 0);
        if (seq) this._lastSeq = Math.max(this._lastSeq, seq);
        let data = {};
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        this.dispatchEvent(new CustomEvent(type, { detail: data }));
      });
    });

    source.addEventListener('open', () => {
      this._retry = 0;
      this._setConnected(true);
    });
    source.addEventListener('error', () => {
      this._setConnected(false);
      source.close();
      this._source = null;
      this._scheduleReconnect();
    });
  }

  _scheduleReconnect() {
    clearTimeout(this._retryTimer);
    const delay = Math.min(8000, 700 * 2 ** this._retry);
    this._retry += 1;
    this._retryTimer = setTimeout(() => this.connectEvents(), delay);
  }

  _setConnected(value) {
    if (this.connected === value) return;
    this.connected = value;
    this.dispatchEvent(new CustomEvent('connection', { detail: { connected: value } }));
  }

  disconnectEvents() {
    clearTimeout(this._retryTimer);
    if (this._source) {
      this._source.close();
      this._source = null;
    }
    this._setConnected(false);
  }
}

export class ApiError extends Error {
  constructor(message, status = 0, payload = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

export const api = new ApiClient();
