/**
 * 前端状态中心 + 与后端的所有动作封装。
 *
 * 设计：单一数据源 + 事件通知。视图只订阅自己关心的频道，
 * 录制中的节点每 2 秒就会收到一次任务更新，因此视图必须走增量渲染
 * （见 ui.js 的 keyedList），否则会不断闪烁、丢焦点。
 */
import { ApiClient, ApiError, api } from './api.js';

const LOG_LIMIT = 600;
const NOTICE_LIMIT = 60;

class Store extends EventTarget {
  constructor() {
    super();
    this.stats = {};
    this.tasks = [];
    this.issues = [];
    this.platforms = [];
    this.qualities = ['原画', '蓝光', '超清', '高清', '标清', '流畅'];
    this.config = { items: [], groups: [] };
    this.configValues = {};
    this.env = {};
    this.logs = [];
    this.notices = [];
    this.connected = false;
    this.loaded = false;
    this.library = { platforms: [], count: 0, total_size: 0, total_size_text: '0 B' };
    this.disk = null;
    this.seq = 0;
  }

  // ------------------------------------------------------------ 事件
  emit(channel, detail) {
    this.dispatchEvent(new CustomEvent(channel, { detail }));
  }

  on(channel, handler) {
    this.addEventListener(channel, handler);
    return () => this.removeEventListener(channel, handler);
  }

  // ------------------------------------------------------------ 初始化
  async bootstrap(session) {
    api.configure(session.baseUrl, session.token || '');
    api.addEventListener('connection', (event) => {
      this.connected = event.detail.connected;
      this.emit('connection');
    });

    await this.refreshAll();
    api.connectEvents();
    this.loaded = true;
    this.emit('state');
    this.registerEventHandlers();
  }

  registerEventHandlers() {
    api.addEventListener('task', (event) => {
      const payload = event.detail || {};
      if (Array.isArray(payload.tasks)) {
        this.tasks = payload.tasks;
      } else if (payload.task) {
        this.mergeTask(payload.task);
      }
      this.emit('tasks');
    });

    api.addEventListener('stats', (event) => {
      this.stats = { ...this.stats, ...(event.detail || {}) };
      this.emit('stats');
    });

    api.addEventListener('control', (event) => {
      const detail = event.detail || {};
      if (typeof detail.running === 'boolean') this.stats.running = detail.running;
      if (detail.stats) this.stats = { ...this.stats, ...detail.stats };
      this.emit('stats');
    });

    api.addEventListener('log', (event) => {
      const record = event.detail || {};
      this.logs.push(record);
      if (this.logs.length > LOG_LIMIT) this.logs.splice(0, this.logs.length - LOG_LIMIT);
      this.emit('logs', record);
    });

    api.addEventListener('notice', (event) => {
      const notice = event.detail || {};
      this.notices.unshift({ ...notice, at: Date.now() });
      if (this.notices.length > NOTICE_LIMIT) this.notices.pop();
      this.emit('notice', notice);
    });

    api.addEventListener('files', () => {
      this.refreshLibrary();
    });
  }

  mergeTask(task) {
    const index = this.tasks.findIndex((item) => item.id === task.id);
    if (index === -1) {
      this.tasks.push(task);
      this.tasks.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    } else {
      this.tasks[index] = task;
    }
  }

  // ------------------------------------------------------------ 拉取
  async refreshAll() {
    const [state, config, platforms, env] = await Promise.all([
      api.get('/api/state'),
      api.get('/api/config'),
      api.get('/api/platforms'),
      api.get('/api/env'),
    ]);
    this.applyState(state);
    this.config = { items: config.items, groups: config.groups, path: config.path };
    this.configValues = Object.fromEntries(config.items.map((item) => [item.slug, item.value]));
    this.platforms = platforms.items || [];
    this.qualities = platforms.qualities || this.qualities;
    this.env = env || {};
    this.emit('state');
    this.emit('tasks');
    this.emit('stats');
    this.refreshLibrary().catch(() => {});
    return state;
  }

  applyState(state) {
    if (!state) return;
    this.stats = state.stats || {};
    this.tasks = (state.tasks || []).slice();
    this.issues = state.issues || [];
  }

  async refreshState() {
    this.applyState(await api.get('/api/state'));
    this.emit('state');
    this.emit('tasks');
    this.emit('stats');
  }

  async refreshLibrary() {
    this.library = await api.get('/api/files');
    this.emit('library');
    return this.library;
  }

  async refreshDisk() {
    this.disk = await api.get('/api/disk');
    this.emit('stats');
  }

  async refreshLogs(limit = 400) {
    const data = await api.get(`/api/logs?limit=${limit}`);
    this.logs = (data.items || []).map((item) => item.data);
    this.emit('logs');
  }

  // ------------------------------------------------------------ 任务动作
  async addTask(url, quality) {
    const result = await api.post('/api/tasks', { url, quality: quality || null });
    await this.refreshState();
    return result;
  }

  async probe(url) {
    return api.post('/api/probe', { url });
  }

  async updateTask(id, patch) {
    const result = await api.patch(`/api/tasks/${id}`, patch);
    if (result.task) this.mergeTask(result.task);
    this.emit('tasks');
    return result;
  }

  async deleteTask(id) {
    await api.del(`/api/tasks/${id}`);
    this.tasks = this.tasks.filter((task) => task.id !== id);
    this.emit('tasks');
  }

  async taskAction(id, action) {
    await api.post(`/api/tasks/${id}/action`, { action });
    this.emit('tasks');
  }

  async reorderTasks(urls) {
    const result = await api.post('/api/tasks/reorder', { urls });
    if (result.tasks) this.tasks = result.tasks;
    this.emit('tasks');
  }

  async setRunning(running) {
    const result = await api.post('/api/control', { running });
    if (result.stats) this.stats = { ...this.stats, ...result.stats };
    this.stats.running = result.running;
    this.emit('stats');
    this.emit('tasks');
    return result;
  }

  async restartAll() {
    await api.post('/api/control', { action: 'restart_all' });
    await this.refreshState();
  }

  // ------------------------------------------------------------ 配置
  async saveConfig(patch) {
    const result = await api.put('/api/config', { patch });
    if (result.changed?.length) {
      const fresh = await api.get('/api/config');
      this.config = { items: fresh.items, groups: fresh.groups, path: fresh.path };
      this.configValues = Object.fromEntries(fresh.items.map((item) => [item.slug, item.value]));
      this.emit('config');
    }
    return result;
  }

  async testPush(channel) {
    return api.post('/api/config/test-push', { channel });
  }

  // ------------------------------------------------------------ 文件
  async deleteFiles(paths) {
    const result = await api.post('/api/files/delete', { paths });
    await this.refreshLibrary();
    return result;
  }

  async reveal(path) {
    return api.post('/api/files/reveal', { path: path || undefined });
  }

  configItem(slug) {
    return this.config.items.find((item) => item.slug === slug);
  }
}

export { ApiError, api };
export const store = new Store();
