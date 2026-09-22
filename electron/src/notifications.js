/**
 * 系统通知。
 *
 * 为什么这件事对录制工具重要：录制是「随机时刻发生」的事件——主播什么时候开播
 * 无法预测。没有系统通知时，用户只能把窗口开着盯，或者事后翻日志才知道漏了。
 *
 * 「要不要打扰用户」的判断放在主进程：只有主进程能可靠知道窗口是否在前台、
 * 是否被隐藏。渲染层只负责说出「发生了什么」，不负责决定打断与否。
 */
const { Notification } = require('electron');

class Notifier {
  /**
   * @param {{iconPath:string, prefs:object, onActivate:()=>void}} options
   */
  constructor(options) {
    this.iconPath = options.iconPath;
    this.prefs = options.prefs;
    this.onActivate = options.onActivate || (() => {});
    this.lastShown = new Map();
  }

  get supported() {
    return Notification.isSupported();
  }

  /**
   * @param {{title:string, body:string, kind?:'start'|'finish'|'error'|'info',
   *          windowFocused:boolean}} payload
   * @returns {boolean} 是否真的弹出了
   */
  show(payload) {
    const desktop = this.prefs.get('desktop') || {};
    if (!desktop.notifications) return false;
    if (!this.supported) return false;

    const kind = payload.kind || 'info';
    if (kind === 'finish' && desktop.notifyOnFinish === false) return false;
    if (kind === 'error' && desktop.notifyOnError === false) return false;
    if (!desktop.notifyWhenFocused && payload.windowFocused) return false;

    // 同内容 5 秒内只弹一次：轮询抖动或状态回弹时不该连环轰炸
    const key = `${kind}|${payload.title}|${payload.body}`;
    const now = Date.now();
    const previous = this.lastShown.get(key) || 0;
    if (now - previous < 5000) return false;
    this.lastShown.set(key, now);
    if (this.lastShown.size > 50) this.lastShown.clear();

    try {
      const notification = new Notification({
        title: payload.title || 'DouyinLiveRecorder',
        body: payload.body || '',
        icon: this.iconPath,
        silent: kind === 'info',
        urgency: kind === 'error' ? 'critical' : 'normal',
      });
      notification.on('click', () => this.onActivate());
      notification.show();
      return true;
    } catch (error) {
      console.warn('[dylr] 发送系统通知失败:', error.message);
      return false;
    }
  }
}

module.exports = { Notifier };
