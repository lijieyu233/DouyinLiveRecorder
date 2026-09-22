/**
 * 桌面端偏好与窗口状态的持久化（主进程独占，渲染层只能通过 IPC 读写）。
 *
 * 为什么放在主进程：窗口的位置/尺寸、关闭行为、托盘开关这些必须在窗口创建
 * **之前**就拿到，渲染层那时候根本还没启动。渲染层只通过 preload 暴露的
 * get/set 访问，两边共用同一份文件，不会出现「界面显示开了、实际没生效」。
 *
 * 存储位置：app.getPath('userData')/desktop-prefs.json
 * 不放进 config/config.ini —— 那份文件是录制核心的配置，会被 CLI 模式和
 * 上游逻辑读写，混入界面偏好会污染它，也会让 git diff 变脏。
 */
const fs = require('node:fs');
const path = require('node:path');

const FILE_NAME = 'desktop-prefs.json';
const SAVE_DELAY_MS = 400;

/** 默认值。改这里就是改首次启动的行为，不需要同步改别处。 */
const DEFAULTS = {
  /** 窗口几何。x/y 为 null 表示让 Electron 居中。 */
  window: { x: null, y: null, width: 1360, height: 880, maximized: false },
  /** 桌面端行为开关，与设置页「桌面端」分组一一对应。 */
  desktop: {
    closeToTray: true,        // 关窗时最小化到托盘而不是退出
    notifications: true,      // 发送系统通知
    notifyWhenFocused: false, // 窗口在前台时也通知（默认关闭，避免打扰）
    notifyOnFinish: true,     // 一场录制结束时通知
    notifyOnError: true,      // 任务异常时通知
  },
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** 递归合并，只覆盖 patch 里显式给出的键。 */
function merge(base, patch) {
  const output = clone(base);
  for (const [key, value] of Object.entries(patch || {})) {
    if (value === undefined) continue;
    if (value && typeof value === 'object' && !Array.isArray(value)
      && output[key] && typeof output[key] === 'object' && !Array.isArray(output[key])) {
      output[key] = merge(output[key], value);
    } else {
      output[key] = value;
    }
  }
  return output;
}

class Prefs {
  constructor() {
    this.path = '';
    this.data = clone(DEFAULTS);
    this.listeners = new Set();
    this.timer = null;
    this.loaded = false;
  }

  /**
   * 读取磁盘上的偏好。必须在 app ready 之后调用（依赖 userData 路径）。
   * 文件损坏或字段缺失时静默回落到默认值——偏好文件不该让程序起不来。
   */
  load(userDataDir) {
    this.path = path.join(userDataDir, FILE_NAME);
    try {
      const raw = fs.readFileSync(this.path, 'utf8');
      this.data = merge(DEFAULTS, JSON.parse(raw));
    } catch (error) {
      if (error.code !== 'ENOENT') {
        console.warn('[dylr] 读取桌面偏好失败，使用默认值:', error.message);
      }
      this.data = clone(DEFAULTS);
    }
    this.loaded = true;
    return this.data;
  }

  all() {
    return clone(this.data);
  }

  /** 支持 'desktop.closeToTray' 这样的点路径。 */
  get(key, fallback) {
    if (!key) return this.all();
    let node = this.data;
    for (const part of String(key).split('.')) {
      if (node === null || typeof node !== 'object' || !(part in node)) {
        return fallback === undefined ? undefined : fallback;
      }
      node = node[part];
    }
    return node === undefined ? fallback : node;
  }

  set(patch) {
    const before = JSON.stringify(this.data);
    this.data = merge(this.data, patch);
    const after = JSON.stringify(this.data);
    if (before === after) return this.all();

    this.scheduleSave();
    const changed = this.all();
    for (const listener of this.listeners) {
      try {
        listener(changed);
      } catch (error) {
        console.error('[dylr] 偏好变更回调异常:', error);
      }
    }
    return changed;
  }

  onChange(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 合并短时间内的多次写入（拖窗口会高频触发）。 */
  scheduleSave() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, SAVE_DELAY_MS);
  }

  flush() {
    if (!this.path) return;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    try {
      fs.mkdirSync(path.dirname(this.path), { recursive: true });
      fs.writeFileSync(this.path, `${JSON.stringify(this.data, null, 2)}\n`, 'utf8');
    } catch (error) {
      console.warn('[dylr] 写入桌面偏好失败:', error.message);
    }
  }

  // ------------------------------------------------------------ 窗口几何
  /**
   * 记下窗口几何。最小化/全屏状态下的 bounds 是无意义的（Windows 上会得到
   * 负坐标），所以只在正常状态时记录，并单独保存 maximized 标记。
   */
  rememberWindow(win) {
    if (!win || win.isDestroyed()) return;
    const maximized = win.isMaximized();
    const patch = { window: { maximized } };
    if (!maximized && !win.isMinimized() && !win.isFullScreen()) {
      const bounds = win.getNormalBounds ? win.getNormalBounds() : win.getBounds();
      patch.window.x = bounds.x;
      patch.window.y = bounds.y;
      patch.window.width = bounds.width;
      patch.window.height = bounds.height;
    }
    this.set(patch);
  }

  /**
   * 计算窗口初始几何。
   *
   * 直接套用上次的坐标在多显示器拔插后会跑到屏幕外（窗口「消失」），
   * 所以这里必须校验与当前任意显示器可见区域的交集是否够大。
   */
  windowBounds(screen) {
    const saved = this.get('window') || {};
    const width = clamp(saved.width, 900, 10000, DEFAULTS.window.width);
    const height = clamp(saved.height, 600, 10000, DEFAULTS.window.height);
    const bounds = { width, height };

    if (typeof saved.x === 'number' && typeof saved.y === 'number') {
      const visible = screen.getAllDisplays().some((display) => {
        const area = display.workArea;
        const overlapX = Math.min(saved.x + width, area.x + area.width) - Math.max(saved.x, area.x);
        const overlapY = Math.min(saved.y + height, area.y + area.height) - Math.max(saved.y, area.y);
        // 至少要留下可抓取的一块（标题栏 + 一小段内容），否则视为不可见
        return overlapX >= 200 && overlapY >= 80;
      });
      if (visible) {
        bounds.x = Math.round(saved.x);
        bounds.y = Math.round(saved.y);
      }
    }
    return bounds;
  }
}

function clamp(value, min, max, fallback) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.min(max, Math.max(min, Math.round(num)));
}

module.exports = { Prefs, DEFAULTS };
