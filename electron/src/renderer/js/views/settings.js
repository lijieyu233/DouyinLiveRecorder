/**
 * 设置页：后端配置由字段元数据驱动，桌面端偏好是同页的一个本地分组。
 *
 * 为什么要让后端配置走元数据：新增/改名一个配置项，界面自动出现对应控件、
 * 分组、校验与说明，不会再出现「配置文件里有、界面里没有」或者前端写错 key。
 *
 * 为什么桌面端偏好单独成组：它存在 electron 的 userData 里而不是 config.ini
 * ——后者是录制核心的配置，会被命令行模式和上游逻辑读写，混入界面开关会污染它。
 * 因此这一组的开关是「改完立刻生效」，不进底部那条未保存队列。
 */
import { store } from '../store.js';
import { prefs } from '../prefs.js';
import {
  confirmDialog, debounce, fill, h, svgIcon, toast, withBusy,
} from '../ui.js';

const GROUP_ICONS = {
  桌面端: 'monitor',
  基础: 'spark',
  录制: 'film',
  转码: 'download',
  网络: 'globe',
  推送: 'bell',
  登录凭据: 'shield',
  平台账号: 'user',
};

/** 桌面端分组：只列出真正影响日常使用的几个开关，不做设置项堆砌。 */
const LOCAL_GROUP = '桌面端';
const DESKTOP_PREFS = [
  {
    key: 'closeToTray',
    label: '关闭窗口时最小化到托盘',
    help: '挂机推荐开启：点关闭只是收起窗口，录制继续跑。要彻底退出请用托盘菜单里的「退出」。',
  },
  {
    key: 'notifications',
    label: '发送系统通知',
    help: '主播开播、录制结束、任务异常时在系统通知中心提醒，不用一直盯着界面。开播时间无法预测，这是最容易漏录的一环。',
  },
  {
    key: 'notifyOnFinish',
    label: '录制结束时通知',
    parent: 'notifications',
    help: '一场直播录完后提示时长与文件大小。',
  },
  {
    key: 'notifyOnError',
    label: '任务异常时通知',
    parent: 'notifications',
    help: '取流失败、Cookie 失效等需要人工介入的情况。',
  },
  {
    key: 'notifyWhenFocused',
    label: '窗口在前台时也通知',
    parent: 'notifications',
    help: '默认关闭：正在看界面的时候不必再弹一条系统通知，界面内已有提示条。',
  },
];

const PUSH_PREFIX = '推送·';
const PUSH_CHANNEL_MAP = {
  '推送·钉钉': '钉钉',
  '推送·微信': '微信',
  '推送·TG': 'TG',
  '推送·邮箱': '邮箱',
  '推送·BARK': 'BARK',
  '推送·NTFY': 'NTFY',
  '推送·PUSHPLUS': 'PUSHPLUS',
};

export function createSettingsView() {
  const el = h('div.view.view-settings');
  const unsubscribers = [];
  const dirty = new Map();
  const errors = new Map();
  let activeGroup = null;
  let keyword = '';
  let showAdvanced = false;

  const groupNav = h('nav.settings-nav');
  const fieldHost = h('div.settings-fields');
  const panelHead = h('header.settings-panel-head');
  const searchInput = h('input.input.search', {
    type: 'search',
    placeholder: '搜索配置项（名称 / 键名 / 说明）',
    oninput: debounce((event) => {
      keyword = event.target.value.trim().toLowerCase();
      renderFields();
    }, 160),
  });
  const saveButton = h('button.btn.primary', svgIcon('save', 16), h('span', '保存修改'));
  const resetButton = h('button.btn.ghost', h('span', '放弃修改'));
  const dirtyHint = h('span.dirty-hint');
  const footer = h('footer.settings-footer');

  const layout = h('div.settings-layout', groupNav,
    h('div.settings-panel',
      panelHead,
      h('div.settings-scroll', fieldHost),
    ));

  el.append(layout, footer);

  // ---------------------------------------------------------------- 数据
  function backendGroups() {
    return store.config.groups || [];
  }

  /** 桌面端分组排在第一位——它是最常改的。 */
  function groupNames() {
    return [LOCAL_GROUP, ...backendGroups()];
  }

  function isLocalGroup(group) {
    return group === LOCAL_GROUP;
  }

  function groupItems(group) {
    return (store.config.items || []).filter((item) => item.group === group);
  }

  function pendingValue(item) {
    return dirty.has(item.slug) ? dirty.get(item.slug) : item.value;
  }

  // ---------------------------------------------------------------- 导航
  function renderNav() {
    groupNav.replaceChildren(
      h('div.settings-nav-title', '配置分组'),
      ...groupNames().map((group) => {
        const local = isLocalGroup(group);
        const items = local ? DESKTOP_PREFS : groupItems(group);
        // 桌面端偏好改完立刻生效，所以不参与「未保存」标记
        const changed = local ? 0 : items.filter((item) => dirty.has(item.slug)).length;
        return h('button.nav-item', {
          class: group === activeGroup ? 'is-active' : '',
          onclick: () => selectGroup(group),
        },
        svgIcon(GROUP_ICONS[group] || 'settings', 16),
        h('span.nav-label', group),
        h('span.nav-count', String(items.length)),
        changed ? h('span.nav-dot', { title: `${changed} 项未保存` }) : null,
        );
      }),
      h('label.advanced-toggle',
        h('input', {
          type: 'checkbox',
          checked: showAdvanced,
          onchange: (event) => {
            showAdvanced = event.target.checked;
            renderFields();
          },
        }),
        h('span', '显示高级选项'),
      ),
      h('button.nav-item.ghost-item', {
        onclick: () => store.reveal(store.config.path),
        title: store.config.path || '',
      }, svgIcon('external', 16), h('span.nav-label', '打开 config.ini')),
    );
  }

  function selectGroup(group) {
    activeGroup = group;
    renderNav();
    renderFields();
    // 页头的「保存」按钮对桌面端分组没有意义（开关是即时生效的），
    // 通知外壳重绘页头，把按钮收起来。
    window.dispatchEvent(new CustomEvent('settings:group-changed', { detail: group }));
  }

  // ---------------------------------------------------------------- 字段
  function renderFields() {
    if (!activeGroup) activeGroup = groupNames()[0];
    if (isLocalGroup(activeGroup)) {
      renderDesktopFields();
      return;
    }
    if (!backendGroups().length) {
      panelHead.replaceChildren();
      fieldHost.replaceChildren(h('div.settings-empty', '正在加载配置…'));
      return;
    }
    const items = groupItems(activeGroup).filter((item) => {
      if (item.advanced && !showAdvanced) return false;
      if (!keyword) return true;
      return `${item.label} ${item.key} ${item.help} ${item.slug}`.toLowerCase().includes(keyword);
    });

    panelHead.replaceChildren();
    fill(panelHead,
      h('div.panel-head-main',
        h('h2.panel-title', activeGroup || ''),
        h('p.panel-desc', describeGroup(activeGroup)),
      ),
      h('div.panel-head-actions',
        searchInput,
        PUSH_CHANNEL_MAP[activeGroup] ? testPushButton(PUSH_CHANNEL_MAP[activeGroup]) : null,
      ),
    );

    if (!items.length) {
      fieldHost.replaceChildren(h('div.settings-empty', '没有匹配的配置项'));
      return;
    }
    fieldHost.replaceChildren(...items.map(fieldRow));
  }

  /** 桌面端分组：本地偏好，改完立刻生效，不进未保存队列。 */
  function renderDesktopFields() {
    const visible = DESKTOP_PREFS.filter((def) => {
      if (!keyword) return true;
      return `${def.label} ${def.help || ''}`.toLowerCase().includes(keyword);
    });

    panelHead.replaceChildren();
    fill(panelHead,
      h('div.panel-head-main',
        h('h2.panel-title', LOCAL_GROUP),
        h('p.panel-desc', describeGroup(LOCAL_GROUP)),
      ),
      h('div.panel-head-actions',
        searchInput,
        h('button.btn.sm.ghost', {
          onclick: async () => {
            const ok = await confirmDialog({
              title: '恢复桌面端默认设置？',
              message: '会重新开启「关闭到托盘」与系统通知，并让窗口下次居中打开。录制任务与 config.ini 不受影响。',
              confirmText: '恢复默认',
            });
            if (!ok) return;
            await prefs.reset();
            renderFields();
            renderFooter();
            toast('已恢复默认设置', { type: 'success' });
          },
        }, svgIcon('refresh', 14), h('span', '恢复默认')),
      ),
    );

    if (!visible.length) {
      fieldHost.replaceChildren(h('div.settings-empty', '没有匹配的设置项'));
      return;
    }
    fieldHost.replaceChildren(...visible.map(prefRow));
  }

  function prefRow(def) {
    // 依赖项：通知总开关关掉后，下面的细分开关置灰而不是消失——
    // 位置固定，用户不会因为开关消失而找不到东西。
    const gated = def.parent ? !prefs.value('desktop', def.parent, true) : false;
    const input = h('input', {
      type: 'checkbox',
      checked: Boolean(prefs.value('desktop', def.key, true)),
      disabled: gated,
    });
    const wrap = h('label.switch.lg', input, h('span.switch-track', h('span.switch-thumb')));
    input.addEventListener('change', async () => {
      const result = await prefs.set('desktop', def.key, input.checked);
      if (!result.ok) {
        input.checked = !input.checked;
        toast(result.error || '保存失败', { type: 'error' });
        return;
      }
      renderFields();      // 主开关会改变其它行的可用状态
      renderFooter();
    });

    return h('div.field-row', {
      class: [gated ? 'is-gated' : '', def.parent ? 'is-nested' : ''].filter(Boolean).join(' '),
      dataset: { pref: def.key },
    },
    h('div.field-info',
      h('div.field-label-row',
        h('span.field-label', def.label),
        h('span.instant-badge', '立即生效'),
      ),
      def.help ? h('div.field-help', def.help) : null,
    ),
    h('div.field-control', wrap),
    );
  }

  function describeGroup(group) {
    const map = {
      桌面端: '窗口与通知的行为偏好，保存在本机（不进 config.ini），改完立即生效。',
      基础: '保存路径、目录结构、文件名规则等；改完需要重启任务才会作用于已有任务。',
      录制: '画质、并发、循环间隔、分段录制与磁盘保护。',
      转码: '录制结束后的自动转码与后处理。',
      网络: '代理地址与哪些平台走代理。海外平台必须配置代理。',
      推送: '直播状态通知的渠道与文案。',
      登录凭据: '各平台的 Cookie。敏感字段仅在本机 config.ini 中保存，界面只显示掩码。',
      平台账号: 'SOOP / FlexTV / PopkonTV / TwitCasting 的登录账号，用于自动续期 Token。',
    };
    const name = String(group || '');
    if (name.startsWith(PUSH_PREFIX)) return `${name.slice(PUSH_PREFIX.length)} 渠道的具体参数。`;
    return map[name] || '';
  }

  function testPushButton(channel) {
    const button = h('button.btn.sm.ghost', svgIcon('bell', 14), h('span', '发送测试'));
    button.onclick = async () => {
      await withBusy(button, async () => {
        try {
          const result = await store.testPush(channel);
          toast(result.message || (result.ok ? '已发送' : '发送失败'), {
            type: result.ok ? 'success' : 'error',
            title: `${channel} 推送测试`,
          });
        } catch (error) {
          toast(error.message, { type: 'error', title: `${channel} 推送测试` });
        }
      });
    };
    return button;
  }

  function fieldRow(item) {
    const value = pendingValue(item);
    const error = errors.get(item.slug);
    const control = buildControl(item, value);
    const isDirty = dirty.has(item.slug);

    return h('div.field-row', {
      class: [isDirty ? 'is-dirty' : '', error ? 'has-error' : ''].filter(Boolean).join(' '),
      dataset: { slug: item.slug },
    },
    h('div.field-info',
      h('div.field-label-row',
        h('span.field-label', item.label),
        isDirty ? h('span.dirty-badge', '未保存') : null,
        item.kind === 'secret' && item.is_set ? h('span.set-badge', svgIcon('check', 11), '已配置') : null,
      ),
      item.help ? h('div.field-help', item.help) : null,
      h('code.field-key', item.key),
      error ? h('div.field-error', svgIcon('alert', 12), error) : null,
    ),
    h('div.field-control', control),
    );
  }

  function buildControl(item, value) {
    const commit = (next) => {
      const normalized = normalize(item, next);
      const original = store.configValues[item.slug];
      if (String(normalized) === String(original ?? '')) dirty.delete(item.slug);
      else dirty.set(item.slug, normalized);
      errors.delete(item.slug);
      renderFooter();
      renderNav();
      const row = fieldHost.querySelector(`[data-slug="${item.slug}"]`);
      row?.classList.toggle('is-dirty', dirty.has(item.slug));
      const badge = row?.querySelector('.dirty-badge');
      if (dirty.has(item.slug) && !badge) {
        row?.querySelector('.field-label-row')?.append(h('span.dirty-badge', '未保存'));
      } else if (!dirty.has(item.slug)) {
        badge?.remove();
      }
    };

    switch (item.kind) {
      case 'bool': {
        const input = h('input', {
          type: 'checkbox',
          checked: Boolean(value),
          onchange: (event) => commit(event.target.checked),
        });
        return h('label.switch.lg', input, h('span.switch-track', h('span.switch-thumb')));
      }
      case 'choice': {
        const select = h('select.select', {
          onchange: (event) => commit(event.target.value),
        }, ...(item.choices || []).map((choice) => h('option', { value: choice }, choice)));
        select.value = value ?? '';
        return select;
      }
      case 'multichoice': {
        const current = new Set(Array.isArray(value) ? value : []);
        const chips = (item.choices || []).map((choice) => {
          const chip = h('button.chip', { class: current.has(choice) ? 'is-on' : '' }, choice);
          chip.onclick = () => {
            if (current.has(choice)) current.delete(choice);
            else current.add(choice);
            chip.classList.toggle('is-on', current.has(choice));
            commit((item.choices || []).filter((c) => current.has(c)));
          };
          return chip;
        });
        return h('div.chip-group', ...chips);
      }
      case 'int':
      case 'float': {
        const input = h('input.input.num', {
          type: 'number',
          value: value ?? '',
          min: item.minimum ?? undefined,
          max: item.maximum ?? undefined,
          step: item.kind === 'float' ? 0.5 : 1,
          oninput: (event) => {
            const raw = event.target.value;
            if (raw === '') return;
            const num = Number(raw);
            if (Number.isNaN(num)) {
              errors.set(item.slug, '请输入数字');
              renderFields();
              return;
            }
            if (item.minimum != null && num < item.minimum) {
              errors.set(item.slug, `不能小于 ${item.minimum}`);
              renderFields();
              return;
            }
            if (item.maximum != null && num > item.maximum) {
              errors.set(item.slug, `不能大于 ${item.maximum}`);
              renderFields();
              return;
            }
            commit(item.kind === 'int' ? Math.round(num) : num);
          },
        });
        return h('div.num-wrap',
          input,
          item.minimum != null || item.maximum != null
            ? h('span.num-range', `${item.minimum ?? '-∞'} ~ ${item.maximum ?? '∞'}`)
            : null,
        );
      }
      case 'path': {
        const input = h('input.input', {
          type: 'text',
          value: value ?? '',
          placeholder: item.placeholder || '',
          oninput: (event) => commit(event.target.value),
        });
        const browse = h('button.btn.sm.ghost', {
          onclick: async () => {
            const picked = await window.dylr?.pickDirectory?.();
            if (picked) {
              input.value = picked;
              commit(picked);
            }
          },
        }, svgIcon('folderOpen', 14), h('span', '浏览'));
        return h('div.path-wrap', input, browse);
      }
      case 'secret': {
        const input = h('input.input.mono', {
          type: 'password',
          value: value ?? '',
          placeholder: item.placeholder || '未配置',
          spellcheck: false,
          oninput: (event) => commit(event.target.value),
        });
        const toggle = h('button.icon-btn', { title: '显示 / 隐藏' }, svgIcon('eye', 15));
        toggle.onclick = () => {
          const shown = input.type === 'text';
          input.type = shown ? 'password' : 'text';
          toggle.replaceChildren(svgIcon(shown ? 'eye' : 'eyeOff', 15));
        };
        return h('div.secret-wrap', input, toggle);
      }
      case 'text': {
        const area = h('textarea.input', {
          rows: 3,
          value: value ?? '',
          placeholder: item.placeholder || '',
          oninput: (event) => commit(event.target.value),
        });
        return area;
      }
      default: {
        const input = h('input.input', {
          type: 'text',
          value: value ?? '',
          placeholder: item.placeholder || '',
          oninput: (event) => commit(event.target.value),
        });
        return input;
      }
    }
  }

  function normalize(item, value) {
    if (item.kind === 'bool') return Boolean(value);
    if (item.kind === 'int') return Math.round(Number(value));
    if (item.kind === 'float') return Number(value);
    return value;
  }

  // ---------------------------------------------------------------- 底部栏
  function renderFooter() {
    // 桌面端分组的开关是即时落盘的，不该出现「保存/放弃」这类还没有意义的按钮
    if (isLocalGroup(activeGroup)) {
      dirtyHint.textContent = '桌面端偏好已即时生效';
      dirtyHint.className = 'dirty-hint';
      fill(footer,
        dirtyHint,
        h('div.spacer'),
        h('span.footer-tip', '保存在本机用户目录，不会写入 config.ini'),
      );
      return;
    }
    const count = dirty.size;
    dirtyHint.textContent = count ? `${count} 项修改未保存` : '所有修改已保存';
    dirtyHint.className = `dirty-hint ${count ? 'is-dirty' : ''}`;
    saveButton.disabled = !count;
    resetButton.disabled = !count;
    fill(footer,
      dirtyHint,
      h('div.spacer'),
      count ? h('span.footer-tip', '改动会直接写入 config/config.ini（保留原有注释）') : null,
      resetButton,
      saveButton,
    );
  }

  async function save() {
    if (!dirty.size) return;
    const patch = Object.fromEntries(dirty);
    await withBusy(saveButton, async () => {
      try {
        const result = await store.saveConfig(patch);
        dirty.clear();
        errors.clear();
        renderNav();
        renderFields();
        renderFooter();
        const restart = result.restart_required?.length || 0;
        toast(
          restart
            ? `已保存 ${result.changed.length} 项；其中 ${restart} 项需要重启任务才生效`
            : `已保存 ${result.changed.length} 项配置`,
          { type: restart ? 'warning' : 'success', title: '配置已写入' },
        );
        if (restart) {
          const ok = await confirmDialog({
            title: '立即重启任务生效？',
            message: '重启会让所有任务重新读取配置（正在录制的会先正常收尾）。',
            confirmText: '立即重启',
          });
          if (ok) await store.restartAll();
        }
      } catch (error) {
        const payload = error.payload || {};
        if (payload.error) {
          toast(payload.error, { type: 'error', title: '保存失败' });
        } else {
          toast(error.message, { type: 'error', title: '保存失败' });
        }
      }
    });
  }

  saveButton.onclick = save;
  resetButton.onclick = async () => {
    if (!(await confirmDialog({
      title: '放弃未保存的修改？',
      message: `${dirty.size} 项修改将恢复为配置文件里的当前值。`,
      confirmText: '放弃修改',
      danger: true,
    }))) return;
    dirty.clear();
    errors.clear();
    renderNav();
    renderFields();
    renderFooter();
  };

  el.mount = () => {
    renderNav();
    renderFields();
    renderFooter();
    unsubscribers.push(store.on('config', () => {
      renderNav();
      renderFields();
      renderFooter();
    }));
    // 配置拉取晚于视图挂载时（例如后端刚起来）也要补渲染一次
    unsubscribers.push(store.on('state', () => {
      if (!backendGroups().length) return;
      renderNav();
      renderFields();
      renderFooter();
    }));
    // 托盘改偏好、或别的视图改了偏好时保持同步
    unsubscribers.push(prefs.onChange(() => {
      if (isLocalGroup(activeGroup)) renderFields();
    }));
  };

  el.destroy = () => unsubscribers.forEach((off) => off());
  el.save = save;
  el.hasUnsaved = () => dirty.size > 0;
  el.focusSearch = () => searchInput.focus();
  /** 供页头判断：桌面端分组的开关即时生效，页头不该再出现「保存」。 */
  el.isDesktopGroup = () => isLocalGroup(activeGroup);
  return el;
}
