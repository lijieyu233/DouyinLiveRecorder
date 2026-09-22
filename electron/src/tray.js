/**
 * 系统托盘。
 *
 * 存在的理由：这是一个「挂机」类工具——用户把直播间加进列表后，真正期望的
 * 是关掉窗口让它自己录。没有托盘时关窗就是退出，录制直接中断，这是原桌面端
 * 最影响实际使用的问题。
 *
 * 托盘提供三件事：
 * 1. 窗口关掉后仍能看到「还在不在录」（图标 + 悬浮提示 + 菜单首行）；
 * 2. 不打开窗口就能开始/暂停全部录制、打开录制目录；
 * 3. 一个不会误触的退出入口。
 *
 * 状态由渲染层通过 `ui:state` 推上来（它掌握最全的任务数据），这里只缓存展示。
 */
const { Menu, Tray, nativeImage } = require('electron');

/** 把统计数字整理成一句人能读的话。 */
function describe(state) {
  if (!state) return '正在启动…';
  if (!state.connected) return '与录制服务断开，正在重连';
  const parts = [];
  if (state.recording) parts.push(`录制中 ${state.recording}`);
  if (state.waiting) parts.push(`等待 ${state.waiting}`);
  if (state.error) parts.push(`异常 ${state.error}`);
  if (!parts.length) parts.push(state.running ? `值守中 ${state.total || 0} 个直播间` : '已暂停');
  return parts.join(' · ');
}

class TrayController {
  /**
   * @param {{iconPath:string, actions:{
   *   toggleWindow:()=>void, showWindow:()=>void, setRunning:(running:boolean)=>void,
   *   openDownloads:()=>void, quit:()=>void}}} options
   */
  constructor(options) {
    this.actions = options.actions;
    this.state = null;
    this.menuKey = '';
    this.tray = null;

    const image = nativeImage.createFromPath(options.iconPath);
    if (image.isEmpty()) {
      console.warn('[dylr] 托盘图标加载失败:', options.iconPath);
      return;
    }
    // Windows 上不缩放会导致图标被系统拉伸模糊；这里显式压到 16px 逻辑尺寸
    this.tray = new Tray(image.resize({ width: 16, height: 16 }));
    this.tray.setToolTip('DouyinLiveRecorder');
    // 左键单击切换窗口，和绝大多数常驻工具一致
    this.tray.on('click', () => this.actions.toggleWindow());
    this.tray.on('double-click', () => this.actions.showWindow());
    this.rebuild(true);
  }

  get available() {
    return Boolean(this.tray);
  }

  /** 渲染层推来新状态。只在展示内容真的变了时重建菜单。 */
  update(state) {
    this.state = state || null;
    if (this.tray) this.tray.setToolTip(`DouyinLiveRecorder\n${describe(this.state)}`);
    this.rebuild();
  }

  rebuild(force = false) {
    if (!this.tray) return;
    const state = this.state || {};
    const running = Boolean(state.running);
    const visible = Boolean(this.actions.isWindowVisible());

    // 只在关键项变化时重建：菜单重建本身有开销，且会打断用户正在浏览的菜单
    const key = [running, visible, Boolean(state.connected), describe(state)].join('|');
    if (!force && key === this.menuKey) return;
    this.menuKey = key;

    const menu = Menu.buildFromTemplate([
      { label: describe(state), enabled: false },
      { type: 'separator' },
      {
        label: visible ? '隐藏主窗口' : '显示主窗口',
        click: () => (visible ? this.actions.hideWindow() : this.actions.showWindow()),
      },
      { type: 'separator' },
      {
        label: running ? '暂停全部录制' : '开始监听全部',
        click: () => this.actions.setRunning(!running),
      },
      { label: '打开录制目录', click: () => this.actions.openDownloads() },
      { type: 'separator' },
      { label: '退出 DouyinLiveRecorder', click: () => this.actions.quit() },
    ]);
    this.tray.setContextMenu(menu);
  }

  /**
   * 关窗到托盘后的一次性提醒。Windows 的气泡通知只在一定条件下显示，
   * 用系统通知更可靠；这里做成「每次关窗都提示一次」，因为用户很容易
   * 以为程序已经退出而反复点关闭。
   */
  notifyHidden() {
    if (this.tray && process.platform === 'win32') {
      try {
        this.tray.displayBalloon?.({
          title: 'DouyinLiveRecorder 仍在后台运行',
          content: '录制会继续。单击托盘图标可重新打开窗口。',
          iconType: 'info',
        });
      } catch {
        /* 气泡通知不是所有环境都支持，失败无妨 */
      }
    }
  }

  destroy() {
    if (this.tray) {
      this.tray.destroy();
      this.tray = null;
    }
  }
}

module.exports = { TrayController, describe };
