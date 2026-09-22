/**
 * 概览页：一眼看清「现在在录什么、有没有异常、磁盘够不够」。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import {
  confirmDialog, emptyState, formatBytes, formatDuration, h, platformTone, relativeTime,
  svgIcon, toast,
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

export function createDashboardView() {
  const el = h('div.view.view-dashboard');
  const unsubscribers = [];
  let ticking = null;

  // ---------------------------------------------------------- 顶部总控
  const powerButton = h('button.btn.lg.primary');
  const powerHint = h('div.power-hint');
  const quickStats = h('div.hero-stats');

  const hero = h('section.hero',
    h('div.hero-main',
      h('div.hero-left',
        h('div.hero-title', '录制总控'),
        powerHint,
      ),
      powerButton,
    ),
    quickStats,
  );

  const statsGrid = h('div.stat-grid');
  const recordingSection = h('section.section');
  const activitySection = h('section.section');
  el.append(hero, statsGrid, recordingSection, activitySection);

  // ---------------------------------------------------------- 渲染片段
  function renderPower() {
    const running = Boolean(store.stats.running);
    powerButton.className = `btn lg ${running ? 'ghost' : 'primary'}`;
    powerButton.replaceChildren(
      svgIcon(running ? 'pause' : 'play', 18),
      h('span', running ? '暂停全部录制' : '开始监听全部'),
    );
    powerButton.onclick = async () => {
      const next = !running;
      if (!next) {
        const recording = store.tasks.filter((t) => t.state === 'recording').length;
        if (recording > 0) {
          const ok = await confirmDialog({
            title: '暂停全部录制？',
            message: `当前有 ${recording} 个直播间正在录制。暂停会让 ffmpeg 正常收尾（已录内容会保留），不会留下孤儿进程。`,
            confirmText: '暂停录制',
            danger: true,
          });
          if (!ok) return;
        }
      }
      powerButton.disabled = true;
      try {
        await store.setRunning(next);
        toast(next ? '已开始监听全部任务' : '已暂停全部任务', { type: 'success' });
      } catch (error) {
        toast(error.message, { type: 'error', title: '操作失败' });
      } finally {
        powerButton.disabled = false;
      }
    };

    const total = store.tasks.length;
    const enabled = store.stats.enabled ?? 0;
    powerHint.textContent = running
      ? `正在值守 ${enabled} 个直播间（共 ${total} 个任务）`
      : total
        ? `${total} 个任务已就绪，点右侧开始监听`
        : '还没有任务，先去「任务」页添加一个直播间地址';
  }

  function renderQuickStats() {
    const stats = store.stats;
    const connection = store.connected;
    // 磁盘剩余优先用 /api/disk 的实时值；stats.disk_free 由后端每 30 秒巡检一次
    const diskFree = stats.disk_free ?? store.disk?.free ?? null;
    const items = [
      {
        icon: 'wifi',
        label: '后端连接',
        value: connection ? '已连接' : '重连中',
        tone: connection ? 'ok' : 'warn',
      },
      {
        icon: 'globe',
        label: '代理',
        value: stats.global_proxy ? '已检测到' : (store.configValues.use_proxy ? '按配置使用' : '未启用'),
        tone: stats.global_proxy ? 'ok' : 'muted',
      },
      {
        icon: 'list',
        label: '并发请求',
        value: `${stats.max_request ?? '—'} 路`,
        tone: 'muted',
      },
      {
        icon: 'disk',
        label: '磁盘剩余',
        value: diskFree != null ? `${diskFree} GB` : '—',
        tone: diskFree != null && diskFree < (stats.disk_threshold || 1) ? 'error' : 'muted',
      },
    ];
    quickStats.replaceChildren(...items.map((item) => h('div.hero-stat',
      h('span.hero-stat-icon', svgIcon(item.icon, 15)),
      h('div',
        h('div.hero-stat-label', item.label),
        h(`div.hero-stat-value.tone-${item.tone}`, item.value),
      ),
    )));
  }

  function renderStats() {
    const stats = store.stats;
    const cards = [
      { key: 'total', label: '监测任务', value: stats.total ?? 0, sub: `启用 ${stats.enabled ?? 0}`, icon: 'tasks', tone: 'neutral' },
      { key: 'recording', label: '正在录制', value: stats.recording ?? 0, sub: '实时写入中', icon: 'spark', tone: stats.recording ? 'live' : 'neutral' },
      { key: 'waiting', label: '等待开播', value: stats.waiting ?? 0, sub: '轮询检测中', icon: 'clock', tone: 'neutral' },
      { key: 'error', label: '异常任务', value: stats.error ?? 0, sub: `瞬时错误 ${stats.error_count ?? 0}`, icon: 'alert', tone: stats.error ? 'error' : 'neutral' },
      { key: 'bytes', label: '本轮写入', value: formatBytes(stats.recording_bytes || 0), sub: `运行 ${formatDuration(stats.uptime || 0)}`, icon: 'download', tone: 'neutral' },
    ];
    statsGrid.replaceChildren(...cards.map((card) => h(`div.stat-card.tone-${card.tone}`,
      h('div.stat-head',
        h('span.stat-icon', svgIcon(card.icon, 16)),
        h('span.stat-label', card.label),
      ),
      h('div.stat-value', card.value),
      h('div.stat-sub', card.sub),
    )));
  }

  // ---------------------------------------------------------- 录制卡片
  function recordingCard(task) {
    const meta = STATE_META[task.state] || STATE_META.idle;
    const card = h(`article.rec-card.state-${task.state}`,
      h('div.rec-top',
        h('div.rec-identity',
          h('span.state-dot', { class: `dot-${meta.tone}` }),
          h('div.rec-names',
            h('div.rec-name', task.display_name || task.url),
            h('div.rec-meta',
              task.platform ? h(`span.badge.platform.${platformTone(task.platform)}`, task.platform) : null,
              h('span.badge.quality', task.quality_effective || task.quality),
              task.title ? h('span.rec-title', task.title) : null,
            ),
          ),
        ),
        h('div.rec-actions',
          h('button.btn.sm.ghost', {
            title: '停止该任务',
            onclick: async () => {
              const ok = await confirmDialog({
                title: '停止录制？',
                message: `将停止「${task.display_name}」并让 ffmpeg 收尾，已录制的内容会保留。`,
                confirmText: '停止',
                danger: true,
              });
              if (!ok) return;
              try {
                await store.taskAction(task.id, 'stop');
                toast('已发送停止指令', { type: 'success' });
              } catch (error) {
                toast(error.message, { type: 'error' });
              }
            },
          }, svgIcon('stop', 15), h('span', '停止')),
        ),
      ),
      h('div.rec-metrics',
        metric('已录制', formatDuration(task.elapsed || 0), 'elapsed', task.id),
        metric('文件大小', formatBytes(task.recorded_bytes || 0), 'bytes', task.id),
        metric('画质', task.quality_effective || task.quality, 'quality', task.id),
      ),
      h('div.rec-file',
        svgIcon('film', 14),
        h('span.rec-file-name', {
          title: task.current_file || '',
          onclick: () => task.current_file && api.post('/api/files/reveal', { path: task.current_file }),
        }, shortPath(task.current_file) || '等待 ffmpeg 写入…'),
      ),
      task.state === 'recording' ? h('div.rec-progress', h('div.rec-progress-bar')) : null,
    );
    card.dataset.taskId = task.id;
    return card;
  }

  function metric(label, value, kind, id) {
    const node = h('div.rec-metric',
      h('span.rec-metric-label', label),
      h('span.rec-metric-value', { dataset: { kind, id } }, value),
    );
    return node;
  }

  function shortPath(path) {
    if (!path) return '';
    const parts = String(path).split('/');
    return parts.slice(-2).join('/');
  }

  function renderRecording() {
    const active = store.tasks
      .filter((task) => ['recording', 'live', 'checking'].includes(task.state))
      .sort((a, b) => (b.state === 'recording') - (a.state === 'recording'));

    const head = h('div.section-head',
      h('div.section-title',
        svgIcon('spark', 16),
        h('span', '正在录制'),
        active.length ? h('span.count-pill', String(active.length)) : null,
      ),
      h('button.btn.sm.ghost', {
        onclick: () => store.refreshState(),
        title: '刷新状态',
      }, svgIcon('refresh', 15)),
    );

    const body = active.length
      ? h('div.rec-list')
      : emptyState({
        iconName: 'spark',
        title: store.tasks.length ? '当前没有正在录制的直播' : '还没有添加直播间',
        description: store.tasks.length
          ? '任务正在后台轮询，主播开播后会自动开始录制。'
          : '把直播间地址粘到「任务」页，选中画质即可开始值守。',
      });

    recordingSection.replaceChildren(head, body);
    if (active.length) {
      const list = body;
      active.forEach((task) => list.append(recordingCard(task)));
    }
  }

  /** 每秒只更新数字，避免整块重绘。 */
  function tick() {
    for (const task of store.tasks) {
      const card = recordingSection.querySelector(`[data-task-id="${task.id}"]`);
      if (!card) continue;
      const elapsed = card.querySelector('[data-kind="elapsed"]');
      const bytes = card.querySelector('[data-kind="bytes"]');
      if (elapsed) elapsed.textContent = formatDuration(task.elapsed || 0);
      if (bytes) bytes.textContent = formatBytes(task.recorded_bytes || 0);
    }
  }

  // ---------------------------------------------------------- 动态流
  function renderActivity() {
    const rows = [];
    for (const notice of store.notices.slice(0, 5)) {
      rows.push(h(`div.activity-row.level-${notice.level || 'info'}`,
        h('span.activity-time', relativeTime(notice.at / 1000)),
        h('span.activity-text', notice.text || ''),
      ));
    }
    const logs = store.logs.slice(-8).reverse();
    for (const log of logs) {
      rows.push(h(`div.activity-row.level-${(log.level || 'INFO').toLowerCase()}`,
        h('span.activity-time', (log.time || '').slice(11, 19)),
        h('span.activity-text', log.text || ''),
      ));
    }

    activitySection.replaceChildren(
      h('div.section-head',
        h('div.section-title', svgIcon('bell', 16), h('span', '最近动态')),
        h('button.btn.sm.ghost', {
          onclick: () => { window.dispatchEvent(new CustomEvent('navigate', { detail: 'logs' })); },
        }, h('span', '查看全部日志')),
      ),
      rows.length ? h('div.activity-list', ...rows.slice(0, 10)) : emptyState({
        iconName: 'bell',
        title: '暂无动态',
        description: '任务状态变化、录制进度与错误都会实时出现在这里。',
      }),
    );
  }

  // ---------------------------------------------------------- 挂载
  function renderAll() {
    renderPower();
    renderQuickStats();
    renderStats();
    renderRecording();
    renderActivity();
  }

  el.mount = () => {
    renderAll();
    unsubscribers.push(store.on('stats', () => { renderPower(); renderQuickStats(); renderStats(); }));
    unsubscribers.push(store.on('tasks', renderRecording));
    unsubscribers.push(store.on('notice', renderActivity));
    unsubscribers.push(store.on('logs', renderActivity));
    unsubscribers.push(store.on('connection', renderQuickStats));
    unsubscribers.push(store.on('library', renderQuickStats));
    ticking = setInterval(tick, 1000);
    store.refreshDisk().catch(() => {});
  };

  el.destroy = () => {
    unsubscribers.forEach((off) => off());
    if (ticking) clearInterval(ticking);
  };

  return el;
}
