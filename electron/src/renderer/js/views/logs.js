/**
 * 日志页：实时滚动、级别过滤、关键字搜索、导出。
 */
import { store } from '../store.js';
import { debounce, emptyState, h, svgIcon, toast } from '../ui.js';

const LEVELS = [
  { key: 'ALL', label: '全部' },
  { key: 'INFO', label: '信息' },
  { key: 'WARNING', label: '警告' },
  { key: 'ERROR', label: '错误' },
];

const RANK = { DEBUG: 0, INFO: 1, SUCCESS: 2, WARNING: 3, ERROR: 4, CRITICAL: 5 };

export function createLogsView() {
  const el = h('div.view.view-logs');
  const unsubscribers = [];
  let level = 'ALL';
  let keyword = '';
  let follow = true;
  let paused = false;

  const listHost = h('div.log-list');
  const followToggle = h('label.switch.sm',
    h('input', {
      type: 'checkbox',
      checked: true,
      onchange: (event) => {
        follow = event.target.checked;
      },
    }),
    h('span.switch-track', h('span.switch-thumb')),
    h('span.switch-label', '自动滚动'),
  );

  const pauseButton = h('button.btn.sm.ghost', svgIcon('pause', 14), h('span', '暂停'));
  const levelBar = h('div.segmented');

  const toolbar = h('div.toolbar',
    h('input.input.search', {
      type: 'search',
      placeholder: '搜索日志内容',
      oninput: debounce((event) => {
        keyword = event.target.value.trim().toLowerCase();
        render();
      }, 160),
    }),
    levelBar,
    h('div.spacer'),
    followToggle,
    pauseButton,
    h('button.btn.sm.ghost', {
      onclick: () => navigator.clipboard.writeText(filtered().map(formatLine).join('\n'))
        .then(() => toast('日志已复制', { type: 'success', timeout: 1500 })),
    }, svgIcon('copy', 14), h('span', '复制')),
    h('button.btn.sm.ghost', {
      onclick: async () => {
        const content = filtered().map(formatLine).join('\n');
        const saved = await window.dylr?.saveTextFile?.({
          defaultName: `dylr-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.log`,
          content,
        });
        if (saved) toast('日志已导出', { type: 'success' });
      },
    }, svgIcon('download', 14), h('span', '导出')),
  );

  el.append(toolbar, listHost);

  function filtered() {
    const min = level === 'ALL' ? 0 : RANK[level];
    return store.logs.filter((log) => {
      if (RANK[log.level] < min) return false;
      if (!keyword) return true;
      return `${log.text} ${log.name || ''}`.toLowerCase().includes(keyword);
    });
  }

  function renderLevels() {
    levelBar.replaceChildren(...LEVELS.map((item) => h('button.seg', {
      class: level === item.key ? 'is-active' : '',
      onclick: () => {
        level = item.key;
        renderLevels();
        render();
      },
    }, h('span', item.label))));
  }

  function formatLine(log) {
    return `[${log.time}] ${String(log.level).padEnd(8)} ${log.text}`;
  }

  function render() {
    const rows = filtered();
    if (!rows.length) {
      listHost.replaceChildren(emptyState({
        iconName: 'logs',
        title: store.logs.length ? '没有符合条件的日志' : '暂无日志',
        description: store.logs.length
          ? '试试切换级别或清空搜索关键词。'
          : '任务开始轮询后，取流、录制、推送的日志会实时出现在这里。',
      }));
      return;
    }
    listHost.replaceChildren(...rows.slice(-800).map((log) => h(`div.log-row.level-${String(log.level).toLowerCase()}`,
      h('span.log-time', (log.time || '').slice(11) || log.time),
      h('span.log-level', log.level),
      h('span.log-text', log.text),
    )));
    if (follow) scrollToBottom();
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      listHost.scrollTop = listHost.scrollHeight;
    });
  }

  pauseButton.onclick = () => {
    paused = !paused;
    pauseButton.replaceChildren(
      svgIcon(paused ? 'play' : 'pause', 14),
      h('span', paused ? '继续' : '暂停'),
    );
    pauseButton.classList.toggle('is-active', paused);
    if (!paused) render();
  };

  el.mount = () => {
    renderLevels();
    render();
    scrollToBottom();
    unsubscribers.push(store.on('logs', () => {
      if (!paused) render();
    }));
  };

  el.destroy = () => unsubscribers.forEach((off) => off());
  el.focusSearch = () => toolbar.querySelector('input').focus();
  return el;
}
