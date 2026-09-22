/**
 * 录制文件页：按平台/主播分组浏览，内置播放器，支持批量删除。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import {
  add, confirmDialog, emptyState, fill, formatBytes, formatTime, h, platformTone, skeleton,
  svgIcon, toast, withBusy,
} from '../ui.js';

const KIND_LABEL = { video: '视频', audio: '音频', subtitle: '字幕', other: '其它' };

export function createLibraryView() {
  const el = h('div.view.view-library');
  const unsubscribers = [];
  const collapsed = new Set();
  const selected = new Map();

  const summary = h('div.lib-summary');
  const listHost = h('div.lib-body');
  const selectionBar = h('div.selection-bar', { style: { display: 'none' } });

  const toolbar = h('div.toolbar',
    h('div.lib-path', svgIcon('folderOpen', 14)),
    h('div.spacer'),
    h('button.btn.sm.ghost', { id: 'expand-all' }, h('span', '全部展开')),
    h('button.btn.sm.ghost', {
      onclick: (event) => withBusy(event.currentTarget, () => store.refreshLibrary())
        .then(() => toast('已刷新文件列表', { type: 'success', timeout: 1400 })),
    }, svgIcon('refresh', 15), h('span', '刷新')),
    h('button.btn.sm.ghost', { onclick: () => store.reveal(null) },
      svgIcon('external', 15), h('span', '打开目录')),
  );

  el.append(toolbar, summary, listHost, selectionBar);

  // ---------------------------------------------------------------- 渲染
  function renderSummary() {
    const lib = store.library || {};
    fill(summary,
      h('div.lib-stat', h('span.lib-stat-value', String(lib.count || 0)), h('span.lib-stat-label', '个文件')),
      h('div.lib-stat', h('span.lib-stat-value', lib.total_size_text || '0 B'), h('span.lib-stat-label', '占用空间')),
      h('div.lib-stat', h('span.lib-stat-value', String((lib.platforms || []).length)), h('span.lib-stat-label', '个平台')),
      store.disk
        ? h('div.lib-stat', h('span.lib-stat-value', `${store.disk.free} GB`), h('span.lib-stat-label', '磁盘剩余'))
        : null,
    );
    const pathHost = toolbar.querySelector('.lib-path');
    fill(pathHost, svgIcon('folderOpen', 14), h('span', lib.root || ''));
  }

  function renderList() {
    const lib = store.library || {};
    if (!store.loaded) {
      listHost.replaceChildren(skeleton(5));
      return;
    }
    if (lib.missing) {
      listHost.replaceChildren(emptyState({
        iconName: 'folderOpen',
        title: '还没有产生录制文件',
        description: '第一次录制成功后，文件会按「平台 / 主播」自动归类到这里。',
      }));
      return;
    }
    if (!lib.count) {
      listHost.replaceChildren(emptyState({
        iconName: 'film',
        title: '录制目录是空的',
        description: `目录位置：${lib.root || ''}`,
        actionLabel: '打开目录',
        onAction: () => store.reveal(null),
      }));
      return;
    }

    const nodes = [];
    for (const platform of lib.platforms || []) {
      const isCollapsed = collapsed.has(platform.name);
      const authorCount = platform.authors.length;
      nodes.push(h('section.lib-platform',
        h('header.lib-platform-head', {
          onclick: () => {
            if (isCollapsed) collapsed.delete(platform.name);
            else collapsed.add(platform.name);
            renderList();
          },
        },
        h('span.chev', { class: isCollapsed ? 'is-collapsed' : '' }, svgIcon('chevronDown', 15)),
        h(`span.badge.platform.${platformTone(platform.name)}`, platform.name),
        h('span.lib-count', `${authorCount} 位主播 · ${platform.count} 个文件`),
        h('div.spacer'),
        h('span.lib-size', platform.size_text),
        ),
        !isCollapsed ? h('div.lib-authors', ...platform.authors.map(authorBlock)) : null,
      ));
    }
    listHost.replaceChildren(...nodes);
  }

  function authorBlock(author) {
    const nodes = author.files.map(fileRow);
    return h('div.lib-author',
      h('div.lib-author-head',
        h('span.author-name', { title: author.name }, author.name || '（未分组）'),
        h('span.author-count', `${author.files.length} 个文件`),
      ),
      h('div.lib-files', ...nodes),
    );
  }

  function fileRow(file) {
    const isSelected = selected.has(file.rel);
    const row = h('div.lib-file', { class: isSelected ? 'is-selected' : '' });
    add(row,
      h('label.checkbox',
        h('input', {
          type: 'checkbox',
          checked: isSelected,
          onchange: (event) => {
            if (event.target.checked) selected.set(file.rel, file);
            else selected.delete(file.rel);
            row.classList.toggle('is-selected', event.target.checked);
            renderSelectionBar();
          },
        }),
        h('span.checkmark'),
      ),
      h('span.file-kind', { title: KIND_LABEL[file.kind] }, svgIcon(file.kind === 'audio' ? 'bell' : 'film', 15)),
      h('div.file-main',
        h('div.file-name', { title: file.name }, file.name),
        h('div.file-meta',
          h('span', file.size_text),
          h('span.dot-sep'),
          h('span', formatTime(file.mtime)),
          h('span.dot-sep'),
          h('span', KIND_LABEL[file.kind] || '文件'),
        ),
      ),
      h('div.file-actions',
        file.playable ? h('button.btn.sm.ghost', {
          onclick: () => openPlayer(file),
        }, svgIcon('play', 14), h('span', '播放')) : null,
        h('button.icon-btn', {
          title: '在文件夹中显示',
          onclick: () => store.reveal(file.path),
        }, svgIcon('external', 15)),
        h('button.icon-btn', {
          title: '复制路径',
          onclick: () => navigator.clipboard.writeText(file.path)
            .then(() => toast('路径已复制', { type: 'success', timeout: 1400 })),
        }, svgIcon('copy', 15)),
        h('button.icon-btn.danger', {
          title: '删除文件',
          onclick: async () => {
            const ok = await confirmDialog({
              title: '删除录制文件？',
              message: `将永久删除：\n${file.rel}\n\n（${file.size_text}）此操作不可撤销。`,
              confirmText: '删除',
              danger: true,
            });
            if (!ok) return;
            try {
              await store.deleteFiles([file.path]);
              toast('文件已删除', { type: 'success' });
            } catch (error) {
              toast(error.message, { type: 'error' });
            }
          },
        }, svgIcon('trash', 15)),
      ),
    );
    return row;
  }

  function renderSelectionBar() {
    const count = selected.size;
    if (!count) {
      selectionBar.style.display = 'none';
      selectionBar.replaceChildren();
      return;
    }
    const total = [...selected.values()].reduce((sum, file) => sum + (file.size || 0), 0);
    selectionBar.style.display = 'flex';
    selectionBar.replaceChildren(
      h('span.selection-info', `已选 ${count} 个文件 · ${formatBytes(total)}`),
      h('div.spacer'),
      h('button.btn.sm.ghost', {
        onclick: () => {
          selected.clear();
          renderList();
          renderSelectionBar();
        },
      }, h('span', '取消选择')),
      h('button.btn.sm.danger', {
        onclick: async (event) => {
          const ok = await confirmDialog({
            title: `删除 ${count} 个文件？`,
            message: `共 ${formatBytes(total)}，删除后无法恢复。`,
            confirmText: '全部删除',
            danger: true,
          });
          if (!ok) return;
          await withBusy(event.currentTarget, async () => {
            const result = await store.deleteFiles([...selected.values()].map((f) => f.path));
            selected.clear();
            renderSelectionBar();
            const failed = result.failed?.length || 0;
            toast(`已删除 ${result.removed.length} 个文件${failed ? `，${failed} 个失败` : ''}`,
              { type: failed ? 'warning' : 'success' });
          });
        },
      }, svgIcon('trash', 14), h('span', '批量删除')),
    );
  }

  // ---------------------------------------------------------------- 播放器
  function openPlayer(file) {
    const isAudio = file.kind === 'audio';
    const media = isAudio
      ? h('audio.media-player', { controls: true, autoplay: true, src: api.mediaUrl(file.path) })
      : h('video.media-player', {
        controls: true, autoplay: true, playsinline: true,
        src: api.mediaUrl(file.path),
      });

    const host = h('div.player-host', media);
    let dialog;
    dialog = (() => {
      const overlay = h('div.modal-overlay.is-in', { style: { position: 'fixed', inset: '0' } });
      const card = h('div.modal-card.player-card',
        h('div.modal-head',
          h('div.modal-title', file.name),
          h('button.icon-btn', { onclick: () => close() }, svgIcon('close', 16)),
        ),
        host,
        h('div.modal-foot',
          h('span.player-meta', `${file.size_text} · ${formatTime(file.mtime)}`),
          h('div.spacer'),
          h('button.btn.sm.ghost', { onclick: () => store.reveal(file.path) },
            svgIcon('external', 14), h('span', '在文件夹中显示')),
        ),
      );
      overlay.append(card);
      document.body.append(overlay);
      const onKey = (event) => { if (event.key === 'Escape') close(); };
      document.addEventListener('keydown', onKey);
      function close() {
        document.removeEventListener('keydown', onKey);
        media.pause?.();
        media.removeAttribute('src');
        overlay.remove();
      }
      return { close };
    })();
    return dialog;
  }

  // ---------------------------------------------------------------- 挂载
  el.mount = () => {
    renderSummary();
    renderList();
    renderSelectionBar();
    store.refreshLibrary().catch(() => {});
    store.refreshDisk().catch(() => {});
    unsubscribers.push(store.on('library', () => { renderSummary(); renderList(); }));
    unsubscribers.push(store.on('stats', renderSummary));
    toolbar.querySelector('#expand-all').onclick = () => {
      if (collapsed.size) collapsed.clear();
      else (store.library.platforms || []).forEach((p) => collapsed.add(p.name));
      renderList();
    };
  };

  el.destroy = () => unsubscribers.forEach((off) => off());
  return el;
}
