/**
 * 应用外壳：启动引导、路由、侧边栏、快捷键、退出确认、托盘联动、系统通知。
 */
import { store } from './store.js';
import { prefs } from './prefs.js';
import { confirmDialog, formatBytes, formatDuration, h, svgIcon, toast } from './ui.js';
import { createDashboardView } from './views/dashboard.js';
import { createTasksView } from './views/tasks.js';
import { createLibraryView } from './views/library.js';
import { createSettingsView } from './views/settings.js';
import { createLogsView } from './views/logs.js';
import { createAboutView } from './views/about.js';

const ROUTES = [
  { key: 'dashboard', label: '概览', icon: 'dashboard', title: '概览', create: createDashboardView,
    subtitle: '录制总控与实时状态' },
  { key: 'tasks', label: '任务', icon: 'tasks', title: '任务管理', create: createTasksView,
    subtitle: '直播间地址、画质与启停' },
  { key: 'library', label: '录制文件', icon: 'library', title: '录制文件', create: createLibraryView,
    subtitle: '按平台与主播归类的产物' },
  { key: 'settings', label: '设置', icon: 'settings', title: '设置', create: createSettingsView,
    subtitle: '所有配置项，等价于 config/config.ini' },
  { key: 'logs', label: '日志', icon: 'logs', title: '运行日志', create: createLogsView,
    subtitle: '实时输出，可按级别过滤' },
  { key: 'about', label: '环境', icon: 'about', title: '环境与关于', create: createAboutView,
    subtitle: '依赖自检、目录与平台清单' },
];

const dom = {
  boot: document.getElementById('boot-root'),
  app: document.getElementById('app'),
  sidebar: document.getElementById('sidebar'),
  head: document.getElementById('page-head'),
  viewRoot: document.getElementById('view-root'),
  status: document.getElementById('tb-status'),
  banner: document.getElementById('conn-banner'),
  bannerText: document.getElementById('conn-banner-text'),
};

let currentRoute = null;
let currentView = null;
let pendingRoute = '';
const viewCache = new Map();
let renderScheduled = false;

// ==================================================================== 启动
let bootRenderedKey = '';
let booting = false;
/**
 * 后端启动期间的原始输出。启动页会把它滚动出来——
 * 后端可能要花十几秒做依赖自检，只显示一个转圈会让人以为卡死了。
 */
const bootLogs = [];
const BOOT_LOG_LIMIT = 60;
/**
 * 后端上报的启动里程碑（`python -m dylr --print-port` 会逐条打印）。
 * 这些是**真的发生过**的步骤，不是界面自己编的进度，
 * 所以用户看到的每一步都能对应到后端实际做的事。
 */
const bootStages = [];
let bootStartedAt = 0;
let bootTicker = null;

/** 单条新增（事件推送）。 */
function pushStage(text) {
  const clean = String(text || '').trim();
  if (!clean) return;
  if (bootStages[bootStages.length - 1] === clean) return;
  bootStages.push(clean);
  repaintBootSteps();
}

/** 批量补齐（渲染层晚于后端启动时，从状态快照回放）。 */
function mergeStages(list) {
  if (!Array.isArray(list) || !list.length) return;
  if (list.length < bootStages.length) return;
  bootStages.splice(0, bootStages.length, ...list);
  repaintBootSteps();
}

window.dylr?.onBackendStage?.(pushStage);

window.dylr?.onBackendLog?.((line) => {
  const text = (line?.text || '').trim();
  if (!text) return;
  bootLogs.push(text);
  if (bootLogs.length > BOOT_LOG_LIMIT) bootLogs.shift();
  if (line?.stream === 'stderr' && /Traceback|Error|错误/i.test(text)) {
    console.warn('[backend]', text);
  }
});

/** 里程碑列表：已走过的打勾，最后一条是正在进行中。 */
function repaintBootSteps() {
  const list = dom.boot.querySelector('.boot-steps');
  if (!list) return;
  const ready = store.loaded;
  const rows = bootStages.map((label, index) => {
    const current = !ready && index === bootStages.length - 1;
    // 以「注意：」开头的是自检发现的问题，用告警色而不是完成的绿色
    const warn = label.startsWith('注意：');
    return h('li.boot-step', {
      class: [current ? '' : 'is-done', warn ? 'is-warn' : ''].filter(Boolean).join(' '),
    },
    current ? h('span.spinner.small') : svgIcon(warn ? 'alert' : 'check', 14),
    h('span', label),
    );
  });
  if (!rows.length) {
    rows.push(h('li.boot-step',
      h('span.spinner.small'),
      h('span', '正在启动录制服务…'),
    ));
  }
  list.replaceChildren(...rows);
}

function startBootTicker() {
  if (bootTicker) return;
  bootStartedAt = bootStartedAt || Date.now();
  const paint = () => {
    const elapsed = dom.boot.querySelector('.boot-elapsed');
    if (elapsed) elapsed.textContent = `已用 ${formatElapsed((Date.now() - bootStartedAt) / 1000)}`;
    const logBox = dom.boot.querySelector('.boot-log');
    if (logBox) fillBootLog(logBox);
    repaintBootSteps();
  };
  paint();
  bootTicker = setInterval(paint, 500);
}

/** 启动耗时的口语化表述：秒级用「秒」，超过一分钟才进位到「分」。 */
function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
}

function stopBootTicker() {
  if (bootTicker) {
    clearInterval(bootTicker);
    bootTicker = null;
  }
  bootStartedAt = 0;
}

function fillBootLog(container) {
  const lines = bootLogs.slice(-6);
  if (!lines.length) {
    container.replaceChildren(h('div.boot-log-line.is-idle', '等待服务输出…'));
    return;
  }
  container.replaceChildren(...lines.map((text, index) => h('div.boot-log-line', {
    class: index === lines.length - 1 ? 'is-latest' : '',
    title: text,
  }, text)));
  container.scrollTop = container.scrollHeight;
}

function renderBoot(state, detail = {}) {
  const key = `${state}|${detail.python || ''}|${detail.error || ''}`;
  if (key === bootRenderedKey) return;      // 同状态不重复重建，避免动画抖动
  bootRenderedKey = key;
  const failed = state === 'error' || state === 'exited';

  // 步骤内容由后端上报的真实里程碑决定，见 repaintBootSteps()；这里只建容器
  const stepList = h('ul.boot-steps');

  const elapsed = h('span.boot-elapsed');
  const logBox = h('div.boot-log');
  fillBootLog(logBox);

  const helpButton = h('button.btn.ghost', {
    onclick: () => renderBootHelp(),
  }, svgIcon('logs', 15), h('span', '排查步骤'));

  const retryButton = h('button.btn.primary', {
    onclick: () => attemptStart(true),
  }, svgIcon('refresh', 15), h('span', '重试启动'));

  const card = h('div.boot-card',
    h('div.boot-logo', svgIcon('spark', 30)),
    h('div.boot-title', 'DouyinLiveRecorder'),
    h('div.boot-sub',
      h('span', failed ? '后端启动失败' : '正在准备录制服务…'),
      failed ? null : elapsed,
    ),
    failed
      ? h('div.boot-error', svgIcon('alert', 15), h('span', detail.error || '未知错误'))
      : stepList,
    // 失败时也要看得到日志——报错原因往往就在最后几行里。
    // 失败态改成换行显示（见 styles.css 的 .is-error）：Python 回溯被单行截断
    // 之后就等于没显示，而这时恰恰是最需要看清细节的时刻。
    h('div.boot-log-wrap', { class: failed ? 'is-error' : '' },
      h('div.boot-log-head',
        h('span', '服务输出'),
        h('button.boot-log-copy', {
          title: '复制全部输出',
          onclick: () => {
            navigator.clipboard.writeText(bootLogs.join('\n'))
              .then(() => toast('已复制启动日志', { type: 'success', timeout: 1600 }))
              .catch(() => toast('复制失败', { type: 'warning' }));
          },
        }, svgIcon('copy', 12), h('span', '复制')),
      ),
      logBox,
    ),
    failed
      ? h('div.boot-actions', retryButton, helpButton)
      : h('div.boot-hint', '首次启动会实际自检 ffmpeg 与 Node.js，结果出现在上面的步骤里'),
  );

  dom.boot.replaceChildren(h('div.boot-screen', card));
  if (!failed) startBootTicker();
  else stopBootTicker();
}

function renderBootHelp() {
  bootRenderedKey = 'help';
  stopBootTicker();
  const card = h('div.boot-card.boot-help',
    h('div.boot-title', '手动排查'),
    h('p.boot-help-text', '在项目根目录打开终端，执行下面这条命令可以看到后端完整报错：'),
    h('pre.boot-code', '.venv\\Scripts\\python.exe -m dylr --print-port'),
    h('p.boot-help-text', '如果提示缺少依赖，请先运行「安装依赖.bat」重建 .venv 环境（需要能访问 pip 镜像）。'),
    h('p.boot-help-text', '如果提示找不到 ffmpeg，请把 ffmpeg 加入 PATH，或把可执行文件放到项目的 ffmpeg/ 目录。'),
    h('div.boot-actions',
      h('button.btn.ghost', { onclick: () => renderBoot('error', {}) }, h('span', '返回')),
      h('button.btn.primary', { onclick: () => attemptStart(true) }, svgIcon('refresh', 15), h('span', '重试启动')),
    ),
  );
  dom.boot.replaceChildren(h('div.boot-screen', card));
}

function showApp() {
  stopBootTicker();
  dom.boot.replaceChildren();
  dom.app.hidden = false;
  console.info('[dylr] 启动完成，界面已就绪');
}

async function attemptStart(isRetry = false) {
  bootRenderedKey = '';
  if (isRetry) {
    // 重试时清掉上一轮的进度与输出，否则新旧两轮混在一起看不出问题
    bootStages.length = 0;
    bootLogs.length = 0;
    bootStartedAt = 0;
    try {
      await window.dylr?.restartBackend?.();
    } catch {
      /* 下面统一按状态重试 */
    }
  }
  // 后端可能比渲染层先就绪，也可能更晚；这里既查状态又轮询会话，
  // 保证两种时序下都能正确进入界面。
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const status = await window.dylr?.getBackendStatus?.();
    // 渲染层可能错过了早于自己注册的里程碑，用快照补齐
    mergeStages(status?.stages);
    if (status?.state === 'ready') {
      const session = await window.dylr?.getSession?.();
      if (session?.baseUrl) {
        await bootApp(session);
        return;
      }
    }
    if (status?.state === 'error' || status?.state === 'exited') {
      renderBoot(status.state, status);
      return;
    }
    renderBoot('starting', status || {});
    await wait(700);
  }
  renderBoot('error', { error: '等待后端就绪超时（90 秒）。请查看排查步骤。' });
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function bootApp(session) {
  if (booting || store.loaded) return;
  booting = true;
  try {
    await prefs.load();
    await store.bootstrap(session);
    showApp();
    renderNav();
    const initial = pendingRoute || location.hash.replace('#', '') || 'dashboard';
    pendingRoute = '';
    navigate(initial, { replace: true });
    bindStoreEvents();
    bindDesktopIntegration();
    setStatus('ready');
  } catch (error) {
    booting = false;
    bootRenderedKey = '';
    renderBoot('error', { error: `连接后端接口失败：${error.message}` });
  }
}

// ==================================================================== 状态条
function setStatus(state, text) {
  const dot = dom.status.querySelector('.status-dot');
  const label = dom.status.querySelector('.tb-status-text');
  const map = {
    ready: ['ok', '服务运行中'],
    starting: ['busy', '启动中'],
    error: ['error', '启动失败'],
    exited: ['error', '服务已退出'],
    stopped: ['muted', '服务已停止'],
  };
  const [tone, defaultText] = map[state] || ['muted', '未知'];
  dot.className = `status-dot dot-${tone}`;
  label.textContent = text || defaultText;
  dom.status.title = label.textContent;
}

function bindStoreEvents() {
  store.on('connection', () => {
    dom.banner.hidden = store.connected;
    if (!store.connected) dom.bannerText.textContent = '与后端的连接已断开，正在自动重连…';
    pushUiState();
  });
  store.on('notice', (event) => {
    const notice = event.detail || {};
    if (!notice.text) return;
    toast(notice.text, {
      type: notice.level === 'error' || notice.level === 'warning' ? notice.level : 'info',
      timeout: notice.level === 'error' ? 9000 : 4200,
    });
    // 只有「错误」级才值得打断系统层面的注意力；警告在界面里提示即可
    if (notice.level === 'error') {
      window.dylr?.notify?.({
        title: 'DouyinLiveRecorder 需要处理',
        body: notice.text,
        kind: 'error',
      });
    }
  });
  store.on('stats', () => { scheduleShellRefresh(); pushUiState(); });
  store.on('tasks', () => { scheduleShellRefresh(); pushUiState(); watchTaskTransitions(); });
}

// ============================================================ 托盘 / 通知
/** 把界面状态同步给托盘：悬浮提示与菜单文案据此更新。 */
function pushUiState() {
  const stats = store.stats || {};
  window.dylr?.reportState?.({
    running: Boolean(stats.running),
    recording: stats.recording || 0,
    waiting: stats.waiting || 0,
    error: stats.error || 0,
    total: stats.total || 0,
    connected: store.connected,
  });
}

/**
 * 上一次见到的任务快照。
 *
 * 首次见到某个任务只登记、不通知——否则每次启动程序，正在录制的任务都会
 * 各弹一条「开始录制」，比不通知更打扰。
 */
const taskSnapshots = new Map();
const FINISHED_STATES = new Set(['waiting', 'idle', 'stopped', 'disabled']);

function watchTaskTransitions() {
  for (const task of store.tasks) {
    const previous = taskSnapshots.get(task.id);
    taskSnapshots.set(task.id, {
      state: task.state,
      elapsed: task.elapsed || 0,
      bytes: task.recorded_bytes || 0,
      name: task.display_name || task.url,
      platform: task.platform,
    });
    if (!previous || previous.state === task.state) continue;
    notifyTransition(task, previous);
  }
  // 清理已删除的任务，避免长跑后 Map 无限增长
  const alive = new Set(store.tasks.map((task) => task.id));
  for (const id of taskSnapshots.keys()) {
    if (!alive.has(id)) taskSnapshots.delete(id);
  }
}

function notifyTransition(task, previous) {
  const name = task.display_name || task.url;
  const stats = store.stats || {};

  if (task.state === 'recording') {
    // 如果已经有别的任务在录，说明用户知道自己在干什么，只报这一个即可
    window.dylr?.notify?.({
      title: `开始录制 · ${name}`,
      body: `${task.platform || '直播'}${task.quality ? ` · ${task.quality}` : ''}｜共 ${stats.recording ?? 1} 路在录`,
      kind: 'start',
    });
    return;
  }

  if (previous.state === 'recording' && FINISHED_STATES.has(task.state)) {
    const size = formatBytes(previous.bytes);
    const duration = formatDuration(previous.elapsed);
    window.dylr?.notify?.({
      title: `录制结束 · ${previous.name}`,
      body: `${duration} · ${size}｜已保存到录制目录`,
      kind: 'finish',
    });
    return;
  }

  if (task.state === 'error') {
    window.dylr?.notify?.({
      title: `任务异常 · ${name}`,
      body: task.last_error || task.message || '任务进入异常状态，请打开界面查看',
      kind: 'error',
    });
  }
}

/** 托盘菜单发来的指令，以及窗口显隐带来的状态同步。 */
function bindDesktopIntegration() {
  pushUiState();
  window.dylr?.onTrayCommand?.(async ({ running } = {}) => {
    try {
      await store.setRunning(Boolean(running));
    } catch (error) {
      toast(error.message, { type: 'error', title: '操作失败' });
    }
  });

  window.dylr?.onTrayOpenDownloads?.(() => {
    store.reveal(store.env?.downloads || null).catch((error) => {
      toast(error.message, { type: 'error', title: '打开目录失败' });
    });
  });

  /**
   * 窗口从托盘恢复时补拉一次状态。
   *
   * 窗口可能已经被收起来好几个小时，期间 SSE 若断过线，界面上的数字会停留在
   * 上次看到的时刻。用户点开托盘图标时最不想看到的就是一份过期数据。
   */
  window.dylr?.onWindowVisibility?.(({ visible } = {}) => {
    if (!visible) return;
    store.refreshState().catch(() => {});
    pushUiState();
  });
}

function scheduleShellRefresh() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    renderNav();
    renderHead();
  });
}

// ==================================================================== 侧边栏
function renderNav() {
  const stats = store.stats || {};
  const badges = {
    tasks: stats.total || 0,
    dashboard: stats.recording || 0,
    library: store.library?.count || 0,
  };
  dom.sidebar.replaceChildren(
    h('div.sidebar-inner',
      h('nav.sidebar-nav', ...ROUTES.map((route) => {
        const badge = badges[route.key];
        const isLive = route.key === 'dashboard' && badge > 0;
        return h('button.nav-entry', {
          class: route.key === currentRoute ? 'is-active' : '',
          onclick: () => navigate(route.key),
          title: route.subtitle,
        },
        svgIcon(route.icon, 17),
        h('span.nav-text', route.label),
        badge ? h('span.nav-badge', { class: isLive ? 'is-live' : '' }, String(badge)) : null,
        );
      })),
      h('div.sidebar-foot',
        h('div.foot-item',
          h('span.foot-label', '并发'),
          h('span.foot-value', String(stats.max_request ?? '—')),
        ),
        h('div.foot-item',
          h('span.foot-label', '磁盘'),
          h('span.foot-value', stats.disk_free != null ? `${stats.disk_free}G` : '—'),
        ),
        h('div.foot-item',
          h('span.foot-label', '代理'),
          h('span.foot-value', stats.global_proxy ? '已开' : '关'),
        ),
      ),
    ),
  );
}

function renderHead() {
  const route = ROUTES.find((item) => item.key === currentRoute);
  if (!route) return;
  const stats = store.stats || {};
  const running = Boolean(stats.running);
  const quick = h('button.btn.sm', {
    class: running ? 'ghost' : 'primary',
    onclick: async (event) => {
      event.currentTarget.disabled = true;
      try {
        await store.setRunning(!running);
      } finally {
        event.currentTarget.disabled = false;
      }
    },
  }, svgIcon(running ? 'pause' : 'play', 14), h('span', running ? '暂停全部' : '开始全部'));

  const actions = [quick];
  if (route.key === 'tasks') {
    actions.unshift(h('button.btn.sm.ghost', {
      onclick: () => currentView?.focusAdd?.(),
    }, svgIcon('plus', 14), h('span', '新增任务')));
  }
  if (route.key === 'logs') {
    actions.unshift(h('button.btn.sm.ghost', {
      onclick: () => currentView?.focusSearch?.(),
    }, svgIcon('search', 14), h('span', '搜索')));
  }
  if (route.key === 'settings') {
    // 桌面端分组的开关是即时落盘的，页头不该摆一个按不动的「保存」
    const desktopGroup = currentView?.isDesktopGroup?.();
    actions.unshift(...(desktopGroup ? [] : [
      h('button.btn.sm.ghost', {
        onclick: () => currentView?.save?.(),
      }, svgIcon('save', 14), h('span', '保存')),
    ]),
    h('button.btn.sm.ghost', {
      onclick: () => currentView?.focusSearch?.(),
    }, svgIcon('search', 14), h('span', '搜索')));
  }

  dom.head.replaceChildren(
    h('div.head-main',
      h('h1.page-title', route.title),
      h('p.page-sub', route.subtitle),
    ),
    h('div.head-actions', ...actions),
  );
}

// ==================================================================== 路由
function navigate(routeKey, { replace = false } = {}) {
  const route = ROUTES.find((item) => item.key === routeKey) || ROUTES[0];
  // 启动引导期间可能已经有 hash（深链接 / 外部改 hash），此时数据还没加载，
  // 先记下来，等 store 就绪后再切过去，避免用空数据渲染视图。
  if (!store.loaded) {
    pendingRoute = route.key;
    return;
  }
  if (currentRoute === route.key && currentView) return;
  if (!replace) location.hash = route.key;

  if (currentView?.destroy) currentView.destroy();
  currentRoute = route.key;

  // 视图保留实例，切换不丢筛选/滚动状态
  let view = viewCache.get(route.key);
  if (!view) {
    view = route.create();
    viewCache.set(route.key, view);
  }
  while (dom.viewRoot.firstChild) dom.viewRoot.removeChild(dom.viewRoot.firstChild);
  dom.viewRoot.append(view);
  currentView = view;
  view.mount?.();

  renderNav();
  renderHead();
}

window.addEventListener('hashchange', () => navigate(location.hash.replace('#', ''), { replace: true }));
window.addEventListener('navigate', (event) => {
  const detail = event.detail;
  if (typeof detail === 'string') navigate(detail);
  else if (detail?.route) navigate(detail.route);
});
// 设置页切换分组会改变页头该有哪些按钮，通知外壳重绘
window.addEventListener('settings:group-changed', renderHead);

// ==================================================================== 快捷键
window.addEventListener('keydown', (event) => {
  const meta = event.ctrlKey || event.metaKey;
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);

  if (meta && event.key.toLowerCase() === 's' && currentRoute === 'settings') {
    event.preventDefault();
    currentView?.save?.();
    return;
  }
  if (meta && event.key.toLowerCase() === 'n') {
    event.preventDefault();
    navigate('tasks');
    setTimeout(() => currentView?.focusAdd?.(), 60);
    return;
  }
  if (meta && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    currentView?.focusSearch?.();
    return;
  }
  if (meta && event.key === 'r') {
    event.preventDefault();
    store.refreshState().then(() => toast('状态已刷新', { type: 'success', timeout: 1400 }));
    return;
  }
  if (meta && /^[1-6]$/.test(event.key)) {
    event.preventDefault();
    navigate(ROUTES[Number(event.key) - 1].key);
    return;
  }
  if (event.key === 'Escape' && !typing) {
    document.querySelector('.modal-overlay.is-in')?.remove();
  }
});

// ==================================================================== 退出确认
/**
 * 关闭窗口时的确认。注意「最小化到托盘」那条路上根本不会走到这里
 * ——那种情况下窗口只是隐藏，录制继续，不该拿弹窗拦人。
 */
window.dylr?.onQuitRequest?.(async () => {
  const recording = store.tasks.filter((task) => task.state === 'recording');
  const cancel = () => window.dylr?.cancelQuit?.();

  if (currentRoute === 'settings' && currentView?.hasUnsaved?.()) {
    const ok = await confirmDialog({
      title: '有未保存的配置修改',
      message: '退出会丢失这些改动。确定要退出吗？',
      confirmText: '退出',
      danger: true,
    });
    if (!ok) return cancel();
    window.dylr.confirmQuit();
    return;
  }
  if (recording.length) {
    const ok = await confirmDialog({
      title: '还有任务正在录制',
      message: `当前有 ${recording.length} 个直播间正在录制。退出会先让 ffmpeg 正常收尾（已录内容保留），然后关闭服务。`,
      confirmText: '停止并退出',
      danger: true,
    });
    if (!ok) return cancel();
  }
  window.dylr.confirmQuit();
});

// ==================================================================== 标题栏
document.getElementById('tb-min').onclick = () => window.dylr?.window.minimize();
document.getElementById('tb-max').onclick = () => window.dylr?.window.toggleMaximize();
document.getElementById('tb-close').onclick = () => window.dylr?.window.close();
document.getElementById('titlebar').addEventListener('dblclick', (event) => {
  if (event.target.closest('.tb-btn')) return;
  window.dylr?.window.toggleMaximize();
});
if (window.dylr?.platform === 'darwin') document.body.dataset.platform = 'darwin';

// ==================================================================== 后端状态
window.dylr?.onBackendStatus?.((status) => {
  if (!status) return;
  if (status.state !== 'ready') setStatus(status.state, status.error);
  if (status.state === 'ready' && !store.loaded) {
    bootApp({ baseUrl: status.baseUrl, token: status.token });
  }
  if (['error', 'exited'].includes(status.state) && !store.loaded) {
    renderBoot(status.state, status);
  }
  if (status.state === 'starting' && store.loaded) {
    dom.banner.hidden = false;
    dom.bannerText.textContent = '后端正在重启，稍后自动恢复…';
  }
});

window.dylr?.onBackendLog?.((line) => {
  if (line?.stream === 'stderr' && /Traceback|Error|错误/i.test(line.text)) {
    console.warn('[backend]', line.text);
  }
});

/** 后端重启期间也要让托盘知道「暂时不可用」，否则菜单还显示着旧状态。 */
window.dylr?.onBackendStatus?.((status) => {
  if (status?.state === 'starting' && store.loaded) {
    window.dylr?.reportState?.({
      running: Boolean(store.stats?.running),
      recording: store.stats?.recording || 0,
      waiting: store.stats?.waiting || 0,
      error: store.stats?.error || 0,
      total: store.stats?.total || 0,
      connected: false,
    });
  }
});

// ==================================================================== 启动
renderBoot('starting', {});
attemptStart();
