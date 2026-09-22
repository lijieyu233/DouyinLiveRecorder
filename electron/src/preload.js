/**
 * 预加载脚本：在隔离上下文里向渲染层暴露一组受控 API。
 *
 * 渲染层没有 Node 能力（contextIsolation: true, nodeIntegration: false），
 * 所有需要系统权限的动作都必须经过这里→主进程。
 */
const { contextBridge, ipcRenderer } = require('electron');

const listeners = new Map();

function subscribe(channel, callback) {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  listeners.set(callback, { channel, handler });
  return () => {
    ipcRenderer.removeListener(channel, handler);
    listeners.delete(callback);
  };
}

contextBridge.exposeInMainWorld('dylr', {
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },

  /** 取后端会话（地址 + 令牌）。后端未就绪时返回 null。 */
  getSession: () => ipcRenderer.invoke('backend:session'),
  /** 主动查询后端状态，避免错过启动期间的状态事件。 */
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),
  restartBackend: () => ipcRenderer.invoke('backend:restart'),
  stopBackend: () => ipcRenderer.invoke('backend:stop'),

  onBackendStatus: (callback) => subscribe('backend:status', callback),
  onBackendLog: (callback) => subscribe('backend:log', callback),
  onBackendStage: (callback) => subscribe('backend:stage', callback),
  onQuitRequest: (callback) => subscribe('app:quit-request', callback),
  onWindowVisibility: (callback) => subscribe('window:visibility', callback),
  onPrefsChanged: (callback) => subscribe('prefs:changed', callback),
  onTrayCommand: (callback) => subscribe('tray:set-running', callback),
  onTrayOpenDownloads: (callback) => subscribe('tray:open-downloads', callback),

  /** 用户确认后真正退出。 */
  confirmQuit: () => ipcRenderer.send('app:quit'),
  /** 用户在确认框里选了「取消」，必须回报，否则关闭流程会卡住。 */
  cancelQuit: () => ipcRenderer.send('app:quit-cancel'),

  /** 桌面偏好（窗口几何 + 行为开关），与主进程共用同一份文件。 */
  prefs: {
    get: () => ipcRenderer.invoke('prefs:get'),
    set: (patch) => ipcRenderer.invoke('prefs:set', patch),
    reset: () => ipcRenderer.invoke('prefs:reset'),
  },

  /** 发送系统通知。是否真的弹出由主进程按偏好与窗口状态决定。 */
  notify: (payload) => ipcRenderer.invoke('notify:show', payload),

  /** 上报界面状态给托盘（悬浮提示与菜单文案据此更新）。 */
  reportState: (state) => ipcRenderer.send('ui:state', state),

  window: {
    minimize: () => ipcRenderer.send('window:minimize'),
    toggleMaximize: () => ipcRenderer.send('window:toggle-maximize'),
    close: () => ipcRenderer.send('window:close'),
    hide: () => ipcRenderer.send('window:hide'),
    show: () => ipcRenderer.send('window:show'),
    isMaximized: () => ipcRenderer.invoke('window:is-maximized'),
    visibility: () => ipcRenderer.invoke('window:visibility'),
  },

  openExternal: (url) => ipcRenderer.invoke('shell:open', url),
  pickDirectory: () => ipcRenderer.invoke('dialog:directory'),
  saveTextFile: (options) => ipcRenderer.invoke('dialog:save-text', options),
  appInfo: () => ipcRenderer.invoke('app:info'),
});
