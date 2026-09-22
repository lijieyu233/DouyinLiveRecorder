/**
 * 桌面偏好（渲染层视图）。
 *
 * 主进程持有真实数据（窗口创建前就要用到），这里只做一层镜像 + 变更通知，
 * 让设置页能像普通表单一样读写，也能在托盘/主进程改动后自动跟上。
 */
class PrefsStore extends EventTarget {
  constructor() {
    super();
    this.data = null;
    this.ready = false;
  }

  async load() {
    try {
      this.data = await window.dylr?.prefs?.get?.() || null;
    } catch (error) {
      console.warn('[dylr] 读取桌面偏好失败:', error);
      this.data = null;
    }
    this.ready = true;
    window.dylr?.onPrefsChanged?.((data) => {
      this.data = data;
      this.emitChange();
    });
    this.emitChange();
    return this.data;
  }

  emitChange() {
    this.dispatchEvent(new CustomEvent('change', { detail: this.data }));
  }

  onChange(handler) {
    this.addEventListener('change', handler);
    return () => this.removeEventListener('change', handler);
  }

  /** 取某个分组：get('desktop') */
  get(section) {
    return (this.data && this.data[section]) || {};
  }

  value(section, key, fallback = false) {
    const group = this.get(section);
    return key in group ? group[key] : fallback;
  }

  async set(section, key, value) {
    // 乐观更新：开关点击后立刻反映，写盘失败再回滚（与任务页开关一致的手感）
    const previous = this.data ? JSON.parse(JSON.stringify(this.data)) : null;
    if (this.data) {
      this.data[section] = { ...(this.data[section] || {}), [key]: value };
      this.emitChange();
    }
    try {
      const fresh = await window.dylr?.prefs?.set?.({ [section]: { [key]: value } });
      if (fresh) this.data = fresh;
      return { ok: true };
    } catch (error) {
      if (previous) this.data = previous;
      this.emitChange();
      return { ok: false, error: error.message };
    }
  }

  async reset() {
    try {
      this.data = await window.dylr?.prefs?.reset?.() || this.data;
    } catch (error) {
      console.warn('[dylr] 重置桌面偏好失败:', error);
    }
    this.emitChange();
    return this.data;
  }
}

export const prefs = new PrefsStore();
