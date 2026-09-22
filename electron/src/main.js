/**
 * Electron 主进程：窗口、托盘、IPC、Python 后端编排。
 */
const { app, BrowserWindow, dialog, ipcMain, screen, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const { BackendService } = require('./backend');
const { Notifier } = require('./notifications');
const { Prefs } = require('./prefs');
const { TrayController } = require('./tray');

const PROJECT_ROOT = process.env.DYLR_HOME
  ? path.resolve(process.env.DYLR_HOME)
  : path.resolve(__dirname, '..', '..');

const ASSET_DIR = path.join(__dirname, '..', 'assets');
const ICON_PATH = path.join(ASSET_DIR, 'icon.png');
const TRAY_ICON_PATH = path.join(ASSET_DIR, 'tray.png');

/**
 * 抓图模式（`--capture out.png`）：全程不显示窗口。
 * 这是开发/验收用的路径，绝不能把窗口弹到用户脸上——他在用电脑干别的事。
 */
const CAPTURE_MODE = process.argv.includes('--capture');

// Windows 上不设置 AppUserModelID，系统通知会挂在 electron.exe 名下甚至不显示
if (process.platform === 'win32') app.setAppUserModelId('com.dylr.desktop');

const prefs = new Prefs();
let notifier = null;
let tray = null;

let mainWindow = null;
let backend = null;
let forceQuit = false;
let quitPending = false;
let quitFallback = null;
let relaunching = false;
let captureTrigger = null;
/** 托盘与窗口共用的一份「界面在显示什么」，由渲染层推送。 */
let uiState = null;

/**
 * 受限环境（远程桌面 / 沙箱 / 无显卡驱动的虚拟机）里 GPU 进程可能无法启动，
 * Electron 会直接 FATAL 退出。这里统一处理：
 * - 命中 --disable-gpu 或 DYLR_DISABLE_GPU=1 时强制软件渲染；
 * - 运行中 GPU 进程反复崩溃时，自动带 --disable-gpu 重启一次。
 */
const SOFTWARE_RENDER = process.argv.includes('--disable-gpu')
  || process.env.DYLR_DISABLE_GPU === '1';
if (SOFTWARE_RENDER) {
  app.disableHardwareAcceleration();
}

app.on('child-process-gone', (_event, details) => {
  if (details?.type !== 'GPU' || SOFTWARE_RENDER || relaunching) return;
  relaunching = true;
  console.warn('[dylr] GPU 进程不可用，改用软件渲染重启');
  app.relaunch({ args: process.argv.slice(1).concat(['--disable-gpu']) });
  app.exit(0);
});

// ---------------------------------------------------------------- 单实例
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 窗口可能被收进托盘了，只 focus 是叫不出来的
    showWindow();
  });
}

// ---------------------------------------------------------------- 后端
function createBackend() {
  const service = new BackendService({
    root: PROJECT_ROOT,
    onStatus: (status) => {
      mainWindow?.webContents.send('backend:status', status);
      if (status.state === 'ready') {
        mainWindow?.setTitle('DouyinLiveRecorder');
        captureTrigger?.();
        captureTrigger = null;
      }
    },
    onLog: (line) => mainWindow?.webContents.send('backend:log', line),
    onStage: (text) => mainWindow?.webContents.send('backend:stage', text),
  });
  return service;
}

// ---------------------------------------------------------------- 窗口显隐
function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

/**
 * 收进托盘。
 *
 * Windows 上 hide() 之后任务栏条目会消失，用户很容易以为程序已经退出，
 * 所以隐藏时给一次明确反馈（气泡 + 托盘悬浮提示里始终带着录制状态）。
 */
function hideWindow({ announce = true } = {}) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  prefs.rememberWindow(mainWindow);
  mainWindow.hide();
  if (announce) tray?.notifyHidden();
  mainWindow.webContents.send('window:visibility', { visible: false });
  tray?.rebuild(true);
}

function toggleWindow() {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && !mainWindow.isMinimized()) {
    hideWindow();
  } else {
    showWindow();
  }
}

/**
 * 走「确认后退出」的流程：把窗口拉出来，交给渲染层弹确认框
 * （它才知道有没有未保存的配置、有没有任务在录）。
 */
function requestQuit() {
  if (forceQuit) return;
  if (!mainWindow || mainWindow.isDestroyed()) {
    forceQuit = true;
    app.quit();
    return;
  }
  if (quitPending) return;
  quitPending = true;
  showWindow();
  mainWindow.webContents.send('app:quit-request');
  // 渲染层 3 秒内没响应就直接退出，避免出现关不掉的窗口
  quitFallback = setTimeout(() => {
    forceQuit = true;
    app.quit();
  }, 3000);
}

function resolveQuit(confirmed) {
  if (quitFallback) {
    clearTimeout(quitFallback);
    quitFallback = null;
  }
  quitPending = false;
  if (confirmed) {
    forceQuit = true;
    app.quit();
  }
}

// ---------------------------------------------------------------- 窗口
function createWindow() {
  const bounds = prefs.windowBounds(screen);
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 1040,
    minHeight: 680,
    show: false,
    icon: ICON_PATH,
    backgroundColor: '#0b0e14',
    title: 'DouyinLiveRecorder',
    frame: false,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden',
    trafficLightPosition: { x: 14, y: 14 },
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      // 挂到托盘后窗口是隐藏状态，Chromium 默认会给隐藏窗口的后台计时器降频。
      // 但这是一个「常驻值守」的应用：收在托盘里时仍然要按秒更新任务状态、
      // 及时收到开播事件并弹系统通知。所以要显式关掉降频。
      backgroundThrottling: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // 恢复上次的最大化状态（放在 ready-to-show 之后，否则 Windows 上会有一次跳动）
  if (prefs.get('window.maximized')) {
    mainWindow.once('ready-to-show', () => mainWindow.maximize());
  }

  mainWindow.once('ready-to-show', () => {
    // 抓图模式不 show()：窗口一旦显示出来就会抢焦点、盖住用户正在做的事。
    // 隐藏窗口同样能 capturePage（见下方 backgroundThrottling: false）。
    if (!CAPTURE_MODE) mainWindow.show();
    if (process.argv.includes('--dev')) mainWindow.webContents.openDevTools({ mode: 'detach' });
    scheduleCapture();
  });

  // 记窗口几何：拖动/缩放结束才落盘（move/resize 事件高频触发）
  const rememberSoon = () => {
    if (rememberTimer) clearTimeout(rememberTimer);
    rememberTimer = setTimeout(() => prefs.rememberWindow(mainWindow), 500);
  };
  let rememberTimer = null;
  for (const event of ['resize', 'move', 'maximize', 'unmaximize']) {
    mainWindow.on(event, rememberSoon);
  }
  mainWindow.on('show', () => {
    mainWindow.webContents.send('window:visibility', { visible: true });
    tray?.rebuild(true);
  });

  // 外部链接一律走系统浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 渲染层异常转发到主进程控制台，方便无人值守排查（--dev 时连 info 一起转发）
  const devMode = process.argv.includes('--dev');
  mainWindow.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    const minLevel = devMode ? 1 : 2;
    if (level >= minLevel) {
      const tag = level >= 3 ? 'error' : level >= 2 ? 'warn' : 'info';
      console.error(`[renderer:${tag}] ${message} (${sourceId}:${line})`);
    }
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    console.error('[dylr] 渲染进程退出:', details.reason);
  });
  mainWindow.webContents.on('did-fail-load', (_event, code, description, url) => {
    console.error(`[dylr] 页面加载失败 ${code} ${description} ${url}`);
  });

  /**
   * 关闭按钮的语义取决于偏好：
   * - 开着「最小化到托盘」→ 只是隐藏，录制继续（这是录制工具该有的默认行为）；
   * - 关掉该选项 → 走确认后退出，让用户明确知道录制会停。
   */
  mainWindow.on('close', (event) => {
    if (forceQuit) return;
    event.preventDefault();
    if (prefs.get('desktop.closeToTray')) {
      hideWindow();
      return;
    }
    requestQuit();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function windowFrom(event) {
  return BrowserWindow.fromWebContents(event.sender);
}

/**
 * 开发/验收用：`electron . --capture out.png [--capture-delay 6000]`
 * 启动后自动截图再退出，便于在无人值守环境下检查界面。
 */
function scheduleCapture() {
  const index = process.argv.indexOf('--capture');
  if (index === -1) return;
  const target = process.argv[index + 1] || 'capture.png';
  const delayIndex = process.argv.indexOf('--capture-delay');
  const rawDelay = delayIndex === -1 ? 6000 : Number(process.argv[delayIndex + 1]);
  const delay = Number.isFinite(rawDelay) && rawDelay >= 0 ? rawDelay : 6000;
  const routeIndex = process.argv.indexOf('--capture-route');
  const route = routeIndex === -1 ? '' : process.argv[routeIndex + 1];
  // --capture-wait-ready：等后端真正就绪后再计时，避免在引导页截到空图
  const waitReady = process.argv.includes('--capture-wait-ready');
  let fired = false;

  const fire = async () => {
    if (fired) return;
    fired = true;
    try {
      if (route) {
        await mainWindow.webContents.executeJavaScript(`location.hash = ${JSON.stringify('#' + route)};`);
        await new Promise((resolve) => setTimeout(resolve, 2200));
      }
      const image = await mainWindow.webContents.capturePage();
      fs.writeFileSync(target, image.toPNG());
      console.log(`[dylr] ${new Date().toISOString()} 截图已保存: ${target}`);
    } catch (error) {
      console.error('[dylr] 截图失败:', error.message);
    } finally {
      forceQuit = true;
      app.quit();
    }
  };

  captureTrigger = () => setTimeout(fire, delay);
  console.log(`[dylr] ${new Date().toISOString()} 计划在 ${delay}ms 后截图: ${target}`
    + (waitReady ? '（等待后端就绪）' : ''));
  if (!waitReady) captureTrigger();
}

// ---------------------------------------------------------------- IPC
function registerIpc() {
  ipcMain.handle('backend:session', () => (
    backend && backend.baseUrl ? { baseUrl: backend.baseUrl, token: backend.token } : null
  ));

  ipcMain.handle('backend:status', () => (backend ? backend.snapshot() : { state: 'idle' }));

  ipcMain.handle('backend:restart', async () => {
    backend._setState('starting', { python: backend.py, reason: 'manual-restart' });
    try {
      await backend.stop();
      const session = await backend.start();
      return session;
    } catch (error) {
      return { error: error.message };
    }
  });

  ipcMain.handle('backend:stop', async () => {
    await backend?.stop();
    return { ok: true };
  });

  ipcMain.on('window:minimize', (event) => windowFrom(event)?.minimize());
  ipcMain.on('window:toggle-maximize', (event) => {
    const win = windowFrom(event);
    if (!win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('window:close', (event) => windowFrom(event)?.close());
  ipcMain.on('window:hide', () => hideWindow());
  ipcMain.on('window:show', () => showWindow());
  ipcMain.handle('window:is-maximized', (event) => Boolean(windowFrom(event)?.isMaximized()));
  ipcMain.handle('window:visibility', () => ({
    visible: Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()),
    focused: Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()),
  }));

  // 渲染层确认退出 / 取消退出。取消必须回报，否则 quitPending 会一直挂着，
  // 用户第二次点关闭会毫无反应。
  ipcMain.on('app:quit', () => resolveQuit(true));
  ipcMain.on('app:quit-cancel', () => resolveQuit(false));

  // ------------------------------------------------------------ 桌面偏好
  ipcMain.handle('prefs:get', () => prefs.all());
  ipcMain.handle('prefs:set', (_event, patch) => prefs.set(patch || {}));
  ipcMain.handle('prefs:reset', () => prefs.set({
    desktop: {
      closeToTray: true,
      notifications: true,
      notifyWhenFocused: false,
      notifyOnFinish: true,
      notifyOnError: true,
    },
  }));

  /**
   * 渲染层上报「界面现在显示什么」，供托盘菜单与悬浮提示使用。
   * 用 send 而不是 invoke：这是高频单向同步，不需要回执。
   */
  ipcMain.on('ui:state', (_event, state) => {
    uiState = state && typeof state === 'object' ? state : null;
    tray?.update(uiState);
  });

  ipcMain.handle('ui:state', () => uiState);

  // ------------------------------------------------------------ 系统通知
  ipcMain.handle('notify:show', (_event, payload) => {
    if (!notifier) return false;
    const focused = Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused());
    const visible = Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible());
    return notifier.show({ ...payload, windowFocused: focused && visible });
  });

  ipcMain.handle('shell:open', async (_event, url) => {
    if (typeof url !== 'string' || !/^https?:/i.test(url)) return false;
    await shell.openExternal(url);
    return true;
  });

  ipcMain.handle('dialog:directory', async (event) => {
    const win = windowFrom(event);
    const result = await dialog.showOpenDialog(win, {
      title: '选择录制保存目录',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });

  ipcMain.handle('dialog:save-text', async (event, options = {}) => {
    const win = windowFrom(event);
    const result = await dialog.showSaveDialog(win, {
      title: '导出文件',
      defaultPath: options.defaultName || 'export.txt',
      filters: [{ name: '文本文件', extensions: ['txt', 'log'] }],
    });
    if (result.canceled || !result.filePath) return null;
    try {
      fs.writeFileSync(result.filePath, options.content ?? '', 'utf8');
      return result.filePath;
    } catch (error) {
      return { error: error.message };
    }
  });

  ipcMain.handle('app:info', () => ({
    root: PROJECT_ROOT,
    version: app.getVersion(),
    platform: process.platform,
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    dev: process.argv.includes('--dev'),
  }));
}

// ---------------------------------------------------------------- 生命周期
app.whenReady().then(async () => {
  prefs.load(app.getPath('userData'));
  notifier = new Notifier({
    iconPath: ICON_PATH,
    prefs,
    onActivate: () => showWindow(),
  });

  registerIpc();
  createWindow();
  createTray();

  backend = createBackend();
  backend.start().catch((error) => {
    console.error('[dylr] 后端启动失败:', error.message);
    notifier?.show({
      title: '录制服务启动失败',
      body: error.message,
      kind: 'error',
      windowFocused: false,
    });
  });

  // 托盘与渲染层共用偏好：托盘改开关后，设置页要立刻跟上
  prefs.onChange((data) => {
    mainWindow?.webContents.send('prefs:changed', data);
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else showWindow();
  });
});

function createTray() {
  tray = new TrayController({
    iconPath: TRAY_ICON_PATH,
    actions: {
      isWindowVisible: () => Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()),
      showWindow,
      hideWindow,
      toggleWindow,
      // 转发给渲染层执行：它才持有 API 客户端与任务数据，避免主进程再实现一套
      setRunning: (running) => {
        if (!uiState) showWindow();           // 界面还没就绪，拉出来让用户自己操作
        mainWindow?.webContents.send('tray:set-running', { running });
      },
      openDownloads: () => {
        if (!uiState) showWindow();
        mainWindow?.webContents.send('tray:open-downloads');
      },
      quit: () => requestQuit(),
    },
  });
  if (!tray.available) {
    console.warn('[dylr] 托盘不可用，关闭窗口将直接退出');
  }
}

app.on('window-all-closed', () => {
  forceQuit = true;
  app.quit();
});

let backendStopped = false;

app.on('before-quit', (event) => {
  prefs.rememberWindow(mainWindow);
  prefs.flush();
  tray?.destroy();
  if (backendStopped || !backend || !backend.process) return;
  event.preventDefault();
  forceQuit = true;
  backendStopped = true;
  backend.stop()
    .catch((error) => console.error('[dylr] 停止后端失败:', error))
    .finally(() => app.quit());
});

process.on('uncaughtException', (error) => {
  console.error('[dylr] 未捕获异常:', error);
});
