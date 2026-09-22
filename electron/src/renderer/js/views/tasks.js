/**
 * 任务页：添加 / 编辑 / 启停 / 排序 / 删除。
 *
 * 交互要点：
 * - 粘贴地址后自动预检（识别平台、提示缺 Cookie / 缺代理、提示重复），
 *   在用户点「添加」之前就把问题说清楚；
 * - 启用开关是乐观更新（先翻转，失败再回滚），避免等待网络造成卡顿感；
 * - 拖拽排序直接落盘到 URL_config.ini，序号与文件顺序一致。
 */
import { store } from '../store.js';
import {
  add, clear, confirmDialog, debounce, emptyState, fill, formatBytes, formatDuration, h,
  keyedList, platformTone, promptDialog, skeleton, svgIcon, toast, withBusy,
} from '../ui.js';

const STATE_META = {
  recording: { tone: 'live', label: '录制中' },
  live: { tone: 'live', label: '直播中' },
  waiting: { tone: 'idle', label: '等待开播' },
  checking: { tone: 'busy', label: '检测中' },
  error: { tone: 'error', label: '异常' },
  disabled: { tone: 'muted', label: '已暂停' },
  stopped: { tone: 'muted', label: '已停止' },
  idle: { tone: 'idle', label: '待启动' },
};

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'recording', label: '录制中' },
  { key: 'waiting', label: '等待中' },
  { key: 'error', label: '异常' },
  { key: 'disabled', label: '已暂停' },
];

export function createTasksView() {
  const el = h('div.view.view-tasks');
  const unsubscribers = [];
  let filter = 'all';
  let keyword = '';
  let probeResult = null;
  let dragging = null;
  let ticking = null;

  // ---------------------------------------------------------- 添加表单
  const urlInput = h('input.input.grow', {
    type: 'text',
    placeholder: '粘贴直播间地址，例如 https://live.douyin.com/123456',
    spellcheck: false,
    autocomplete: 'off',
  });
  const qualitySelect = h('select.select');
  const addButton = h('button.btn.primary', svgIcon('plus', 16), h('span', '添加'));
  const probeBox = h('div.probe-box');

  const addForm = h('form.add-form', {
    onsubmit: (event) => {
      event.preventDefault();
      submitAdd();
    },
  },
    h('div.add-row',
      h('div.input-affix', svgIcon('plus', 16), urlInput),
      qualitySelect,
      addButton,
    ),
    probeBox,
  );

  function rebuildQualityOptions() {
    const current = qualitySelect.value;
    const defaultLabel = store.configValues.video_record_quality || '原画';
    qualitySelect.replaceChildren(
      h('option', { value: '' }, `跟随默认（${defaultLabel}）`),
      ...store.qualities.map((label) => h('option', { value: label }, label)),
    );
    qualitySelect.value = current || '';
  }

  const runProbe = debounce(async () => {
    const url = urlInput.value.trim();
    if (url.length < 8) {
      probeResult = null;
      renderProbe();
      return;
    }
    try {
      probeResult = await store.probe(url);
    } catch (error) {
      probeResult = { supported: false, warnings: [error.message] };
    }
    renderProbe();
  }, 450);

  urlInput.addEventListener('input', runProbe);
  urlInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitAdd();
    }
  });

  function renderProbe() {
    if (!probeResult) {
      probeBox.replaceChildren();
      return;
    }
    const chips = [];
    if (probeResult.platform) {
      chips.push(h(`span.badge.platform.${platformTone(probeResult.platform)}`,
        svgIcon(probeResult.overseas ? 'globe' : 'check', 12), probeResult.platform));
    } else {
      chips.push(h('span.badge.warn', svgIcon('alert', 12), '未识别平台'));
    }
    if (probeResult.audio_only) chips.push(h('span.badge', '仅音频'));
    if (probeResult.custom) chips.push(h('span.badge', '自定义直链'));
    if (probeResult.exists) chips.push(h('span.badge.warn', '已在列表中'));

    probeBox.replaceChildren(
      h('div.probe-chips', ...chips),
      ...(probeResult.warnings || []).map((text) => h('div.probe-warning',
        svgIcon('alert', 13), h('span', text))),
    );
  }

  async function submitAdd() {
    const url = urlInput.value.trim();
    if (!url) {
      urlInput.focus();
      toast('请先粘贴直播间地址', { type: 'warning' });
      return;
    }
    if (probeResult?.exists) {
      toast('这个地址已经在任务列表里了', { type: 'warning' });
      return;
    }
    try {
      await withBusy(addButton, async () => {
        const result = await store.addTask(url, qualitySelect.value);
        urlInput.value = '';
        probeResult = null;
        renderProbe();
        toast(`${result.task?.platform || '任务'} 已加入监控`, {
          type: 'success',
          title: result.task?.display_name || undefined,
        });
      });
    } catch (error) {
      toast(error.message, { type: 'error', title: '添加失败' });
    }
  }

  // ---------------------------------------------------------- 工具栏
  const searchInput = h('input.input.search', {
    type: 'search',
    placeholder: '搜索主播 / 平台 / 地址',
    oninput: debounce((event) => {
      keyword = event.target.value.trim().toLowerCase();
      renderList();
    }, 160),
  });
  const filterBar = h('div.segmented');

  function renderFilters() {
    filterBar.replaceChildren(...FILTERS.map((item) => h('button.seg', {
      class: filter === item.key ? 'is-active' : '',
      onclick: () => {
        filter = item.key;
        renderFilters();
        renderList();
      },
    },
    h('span', item.label),
    item.key === 'all' ? null : h('span.seg-count', String(countBy(item.key))),
    )));
  }

  function countBy(key) {
    return store.tasks.filter((task) => matchFilter(task, key)).length;
  }

  function matchFilter(task, key) {
    if (key === 'all') return true;
    if (key === 'recording') return task.state === 'recording';
    if (key === 'waiting') return ['waiting', 'checking', 'idle'].includes(task.state);
    if (key === 'error') return task.state === 'error';
    if (key === 'disabled') return !task.enabled;
    return true;
  }

  const toolbar = h('div.toolbar',
    searchInput,
    filterBar,
    h('button.btn.sm.ghost', {
      onclick: async () => {
        try {
          await withBusy(toolbar.querySelector('.btn.refresh'), () => store.refreshState());
        } catch (error) {
          toast(error.message, { type: 'error' });
        }
      },
      class: 'refresh',
      title: '重新读取 URL 文件',
    }, svgIcon('refresh', 15)),
    h('button.btn.sm.ghost', {
      onclick: () => store.reveal(null),
      title: '打开配置目录',
    }, svgIcon('folderOpen', 15)),
  );

  const listHost = h('div.task-list');
  el.append(addForm, toolbar, listHost);

  // ---------------------------------------------------------- 列表
  function renderList() {
    const tasks = store.tasks.filter((task) => {
      if (!matchFilter(task, filter)) return false;
      if (!keyword) return true;
      return [task.display_name, task.platform, task.url, task.label, task.title]
        .filter(Boolean).join(' ').toLowerCase().includes(keyword);
    });

    if (!store.loaded) {
      listHost.replaceChildren(skeleton(4));
      return;
    }
    if (!store.tasks.length) {
      listHost.replaceChildren(emptyState({
        iconName: 'tasks',
        title: '还没有监控任务',
        description: '在上方粘贴直播间地址，回车即可加入监控。地址会写入 config/URL_config.ini。',
      }));
      return;
    }
    if (!tasks.length) {
      listHost.replaceChildren(emptyState({
        iconName: 'search',
        title: '没有匹配的任务',
        description: '换个关键词，或把筛选切回「全部」。',
      }));
      return;
    }

    keyedList(listHost, tasks, {
      key: (task) => task.id,
      create: (task) => taskRow(task),
      update: (node, task) => updateTaskRow(node, task),
    });
  }

  function taskRow(task) {
    const row = h('div.task-row', { draggable: 'true' });
    row.append(
      h('span.drag-handle', { title: '拖动可调整顺序' }, svgIcon('drag', 16)),
      h('span.state-dot'),
      h('div.task-main',
        h('div.task-name-row',
          h('span.task-name'),
          h('span.task-badges'),
        ),
        h('div.task-sub'),
      ),
      h('div.task-live'),
      h('div.task-controls'),
    );

    row.addEventListener('dragstart', (event) => {
      dragging = task.id;
      row.classList.add('is-dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', task.id);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('is-dragging');
      dragging = null;
      listHost.querySelectorAll('.drop-target').forEach((n) => n.classList.remove('drop-target'));
    });
    row.addEventListener('dragover', (event) => {
      if (!dragging || dragging === task.id) return;
      event.preventDefault();
      row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', async (event) => {
      event.preventDefault();
      row.classList.remove('drop-target');
      const sourceId = dragging;
      if (!sourceId || sourceId === task.id) return;
      const urls = store.tasks.map((item) => item.url);
      const from = store.tasks.findIndex((item) => item.id === sourceId);
      const to = store.tasks.findIndex((item) => item.id === task.id);
      if (from === -1 || to === -1) return;
      const [moved] = urls.splice(from, 1);
      urls.splice(to, 0, moved);
      try {
        await store.reorderTasks(urls);
        toast('顺序已保存', { type: 'success', timeout: 1500 });
      } catch (error) {
        toast(error.message, { type: 'error' });
      }
    });
    return row;
  }

  function updateTaskRow(row, task) {
    // 停用中的任务一律显示「已暂停」，避免出现「待启动 + 开关关闭」这种自相矛盾的组合
    const meta = !task.enabled ? STATE_META.disabled : (STATE_META[task.state] || STATE_META.idle);
    row.classList.toggle('is-disabled', !task.enabled);
    row.classList.toggle('is-recording', task.state === 'recording');
    row.querySelector('.state-dot').className = `state-dot dot-${meta.tone}`;
    row.querySelector('.task-name').textContent = task.display_name || task.url;

    fill(row.querySelector('.task-badges'),
      task.platform ? h(`span.badge.platform.${platformTone(task.platform)}`, task.platform) : null,
      h(`span.badge.state-${meta.tone}`, meta.label),
      task.quality_explicit ? h('span.badge.quality', task.quality) : null,
    );

    fill(row.querySelector('.task-sub'),
      h('span.task-url', {
        title: '点击复制地址',
        onclick: () => navigator.clipboard.writeText(task.url).then(
          () => toast('地址已复制', { type: 'success', timeout: 1400 }),
          () => toast('复制失败', { type: 'warning' }),
        ),
      }, task.url),
      task.message && task.message !== meta.label ? h('span.task-message', task.message) : null,
      task.last_error ? h('span.task-error', svgIcon('alert', 12), task.last_error) : null,
    );

    const live = row.querySelector('.task-live');
    clear(live);
    if (task.state === 'recording') {
      add(live,
        h('div.task-live-item',
          h('span.task-live-value', { dataset: { kind: 'elapsed', task: task.id } },
            formatDuration(task.elapsed || 0)),
          h('span.task-live-label', '已录制'),
        ),
        h('div.task-live-item',
          h('span.task-live-value', { dataset: { kind: 'bytes', task: task.id } },
            formatBytes(task.recorded_bytes || 0)),
          h('span.task-live-label', '大小'),
        ),
      );
    } else if (task.next_check_at) {
      add(live, h('div.task-live-item',
        h('span.task-live-value', { dataset: { kind: 'countdown', task: task.id } }, '—'),
        h('span.task-live-label', '下次检测'),
      ));
    }

    fill(row.querySelector('.task-controls'),
      qualitySelectFor(task),
      switchFor(task),
      actionButtons(task),
    );
  }

  function qualitySelectFor(task) {
    const select = h('select.select.sm', {
      title: '该房间的录制画质',
      onchange: async (event) => {
        const value = event.target.value;
        if (!value) return;
        try {
          await store.updateTask(task.id, { quality: value });
          toast(`「${task.display_name}」画质已改为 ${value}`, { type: 'success', timeout: 1800 });
        } catch (error) {
          toast(error.message, { type: 'error' });
          event.target.value = task.quality;
        }
      },
    },
      ...store.qualities.map((label) => h('option', { value: label }, label)),
    );
    select.value = task.quality;
    return select;
  }

  function switchFor(task) {
    const input = h('input', {
      type: 'checkbox',
      role: 'switch',
      checked: task.enabled,
      'aria-label': '启用该任务',
    });
    const wrap = h('label.switch', input, h('span.switch-track', h('span.switch-thumb')));
    input.addEventListener('change', async () => {
      const next = input.checked;
      const previous = !next;
      // 乐观更新：先动 UI，失败再回滚
      task.enabled = next;
      if (!next) task.state = 'disabled';
      updateTaskRow(wrap.closest('.task-row'), task);
      try {
        await store.updateTask(task.id, { enabled: next });
        toast(next ? '已恢复监控' : '已暂停监控', { type: 'success', timeout: 1500 });
      } catch (error) {
        task.enabled = previous;
        updateTaskRow(wrap.closest('.task-row'), task);
        toast(error.message, { type: 'error' });
      }
    });
    return wrap;
  }

  function actionButtons(task) {
    const restart = h('button.icon-btn', {
      title: '重启该任务（重新读取配置）',
      onclick: async (event) => {
        try {
          await withBusy(event.currentTarget, () => store.taskAction(task.id, 'restart'));
          toast('任务已重启', { type: 'success', timeout: 1500 });
        } catch (error) {
          toast(error.message, { type: 'error' });
        }
      },
    }, svgIcon('refresh', 15));

    const edit = h('button.icon-btn', {
      title: '编辑备注名',
      onclick: async () => {
        const value = await promptDialog({
          title: '编辑备注',
          label: '备注会写回 URL_config.ini 的「主播:」字段，替换自动识别的主播名',
          value: (task.label || '').replace(/^主播:\s*/, ''),
          placeholder: '例如 老板的直播间',
        });
        if (value === null) return;
        try {
          await store.updateTask(task.id, { label: value ? `主播: ${value}` : '' });
          toast('备注已保存', { type: 'success', timeout: 1500 });
        } catch (error) {
          toast(error.message, { type: 'error' });
        }
      },
    }, svgIcon('edit', 15));

    const remove = h('button.icon-btn.danger', {
      title: '从列表删除',
      onclick: async () => {
        const ok = await confirmDialog({
          title: '删除任务？',
          message: `「${task.display_name}」会从 URL_config.ini 中移除，已录制的文件不受影响。`,
          confirmText: '删除',
          danger: true,
        });
        if (!ok) return;
        try {
          await store.deleteTask(task.id);
          toast('任务已删除', { type: 'success' });
        } catch (error) {
          toast(error.message, { type: 'error' });
        }
      },
    }, svgIcon('trash', 15));

    return h('div.row-actions', restart, edit, remove);
  }

  function tick() {
    for (const task of store.tasks) {
      if (task.state !== 'recording' && !task.next_check_at) continue;
      const elapsed = listHost.querySelector(`[data-kind="elapsed"][data-task="${task.id}"]`);
      const bytes = listHost.querySelector(`[data-kind="bytes"][data-task="${task.id}"]`);
      const countdown = listHost.querySelector(`[data-kind="countdown"][data-task="${task.id}"]`);
      if (elapsed) elapsed.textContent = formatDuration(task.elapsed || 0);
      if (bytes) bytes.textContent = formatBytes(task.recorded_bytes || 0);
      if (countdown && task.next_check_at) {
        const left = Math.max(0, Math.round(task.next_check_at - Date.now() / 1000));
        countdown.textContent = `${left}s`;
      }
    }
  }

  // ---------------------------------------------------------- 挂载
  el.mount = () => {
    rebuildQualityOptions();
    renderFilters();
    renderList();
    unsubscribers.push(store.on('tasks', () => { renderFilters(); renderList(); }));
    unsubscribers.push(store.on('state', () => { rebuildQualityOptions(); renderFilters(); renderList(); }));
    unsubscribers.push(store.on('stats', renderFilters));
    ticking = setInterval(tick, 1000);
    if (!store.tasks.length) urlInput.focus();
  };

  el.destroy = () => {
    unsubscribers.forEach((off) => off());
    if (ticking) clearInterval(ticking);
  };

  el.focusSearch = () => searchInput.focus();
  el.focusAdd = () => urlInput.focus();
  return el;
}
