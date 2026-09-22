/**
 * 关于 / 环境页：把「为什么录不了」这类问题需要的全部信息集中在一屏。
 */
import { store } from '../store.js';
import { confirmDialog, h, platformTone, svgIcon, toast, withBusy } from '../ui.js';

export function createAboutView() {
  const el = h('div.view.view-about');
  const unsubscribers = [];
  let platformFilter = '';

  const envHost = h('div.env-grid');
  const pathHost = h('div.path-list');
  const platformHost = h('div.platform-grid');

  el.append(
    h('section.about-hero',
      h('div.about-logo', svgIcon('spark', 30)),
      h('div.about-title-wrap',
        h('h2.about-title', 'DouyinLiveRecorder 桌面端'),
        h('p.about-sub', '40+ 平台直播监控与录制 · 录制核心来自上游 ihmily/DouyinLiveRecorder'),
      ),
      h('div.about-actions',
        h('button.btn.sm.ghost', {
          onclick: () => window.dylr?.openExternal?.('https://github.com/ihmily/DouyinLiveRecorder'),
        }, svgIcon('external', 14), h('span', '上游项目')),
        h('button.btn.sm.ghost', {
          onclick: () => window.dylr?.openExternal?.('https://github.com/ihmily/DouyinLiveRecorder/blob/main/README.md'),
        }, svgIcon('about', 14), h('span', '使用文档')),
      ),
    ),
    section('运行环境', iconName('shield'), envHost, '录制依赖 ffmpeg 与 Node.js（用于平台签名脚本），缺失时无法取流。'),
    section('相关目录', iconName('folderOpen'), pathHost, '所有产物都在项目目录内，配置改动会直接写入 config/config.ini。'),
    section('支持的平台', iconName('globe'), platformHost, '海外平台需要在「网络」里配置代理后才能取流。',
      h('input.input.search.inline', {
        type: 'search',
        placeholder: '筛选平台',
        oninput: (event) => {
          platformFilter = event.target.value.trim().toLowerCase();
          renderPlatforms();
        },
      }),
    ),
  );

  function section(title, iconEl, body, desc, extra) {
    return h('section.section',
      h('div.section-head',
        h('div.section-title', iconEl, h('span', title)),
        extra || null,
      ),
      desc ? h('p.section-desc', desc) : null,
      body,
    );
  }

  function iconName(name) {
    return svgIcon(name, 16);
  }

  function renderEnv() {
    const env = store.env || {};
    const items = [
      { label: '后端版本', value: env.version, tone: 'ok' },
      { label: 'Python', value: env.python, tone: 'neutral' },
      { label: 'ffmpeg', value: env.ffmpeg_ok ? env.ffmpeg : '未检测到', tone: env.ffmpeg_ok ? 'ok' : 'error' },
      { label: 'Node.js', value: env.node || '未检测到', tone: env.node ? 'ok' : 'error' },
      { label: '系统', value: env.platform, tone: 'neutral' },
      { label: '支持平台数', value: `${env.platform_count} 个`, tone: 'neutral' },
    ];
    envHost.replaceChildren(...items.map((item) => h('div.env-item',
      h('span.env-label', item.label),
      h(`span.env-value.tone-${item.tone}`, { title: item.value }, item.value || '—'),
    )));
  }

  function renderPaths() {
    const env = store.env || {};
    const rows = [
      { label: '项目根目录', value: env.root },
      { label: '录制保存目录', value: env.downloads },
      { label: '日志目录', value: env.logs },
      { label: '配置文件', value: env.config_file },
      { label: '地址列表', value: env.url_file },
    ];
    pathHost.replaceChildren(...rows.map((row) => h('div.path-row',
      h('span.path-label', row.label),
      h('code.path-value', { title: row.value || '' }, row.value || '—'),
      h('button.icon-btn', {
        title: '打开位置',
        onclick: () => store.reveal(row.value),
      }, svgIcon('external', 15)),
      h('button.icon-btn', {
        title: '复制路径',
        onclick: () => navigator.clipboard.writeText(row.value || '')
          .then(() => toast('路径已复制', { type: 'success', timeout: 1400 })),
      }, svgIcon('copy', 15)),
    )));
  }

  function renderPlatforms() {
    const list = (store.platforms || []).filter((item) => {
      if (!platformFilter) return true;
      return `${item.name} ${item.key}`.toLowerCase().includes(platformFilter);
    });
    platformHost.replaceChildren(...list.map((item) => h('div.platform-card',
      h(`span.badge.platform.${platformTone(item.name)}`, item.name),
      h('span.platform-tags',
        item.overseas ? h('span.tag.warn', '需代理') : null,
        item.audio_only ? h('span.tag', '仅音频') : null,
        item.force_flv ? h('span.tag', 'FLV') : null,
        item.custom ? h('span.tag', '直链') : null,
      ),
      item.cookie_key
        ? h('button.tag.link', {
          onclick: () => window.dispatchEvent(new CustomEvent('navigate', {
            detail: { route: 'settings', group: '登录凭据' },
          })),
        }, store.configValues[`cookie_${item.cookie_key}`] ? 'Cookie 已填' : 'Cookie 未填')
        : null,
    )));
  }

  const restartButton = h('button.btn.danger.sm', svgIcon('power', 14), h('span', '重启后端服务'));

  el.mount = () => {
    renderEnv();
    renderPaths();
    renderPlatforms();
    el.prepend(restartButton);
    restartButton.onclick = async () => {
      const ok = await confirmDialog({
        title: '重启后端服务？',
        message: '正在录制的任务会先让 ffmpeg 正常收尾，随后后端重新启动并恢复监听。',
        confirmText: '重启',
        danger: true,
      });
      if (!ok) return;
      await withBusy(restartButton, async () => {
        toast('正在重启后端…', { type: 'info' });
        await window.dylr?.restartBackend?.();
      });
    };
    unsubscribers.push(store.on('state', () => {
      renderEnv();
      renderPaths();
    }));
  };

  el.destroy = () => unsubscribers.forEach((off) => off());
  return el;
}
