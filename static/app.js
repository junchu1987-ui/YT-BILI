/* app.js — YT→Bilibili Web UI */
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  status: 'idle',
  candidates: [],
  downloaded: [],
  transcoded: [],
  uploaded: [],
  errors: [],
  progress: {},
  current_video: null,
  selectedFormats: {},   // videoID -> Set of formatIDs
  formatsExpanded: {},  // videoID -> bool, default false (collapsed)
  metaExpanded: {},     // videoID -> bool, default false (collapsed)
  videoMeta: {},       // videoID -> { title, tid, tags: [] }
  globalTid: 122,
  defaultTags: [],
};
let selectedIds = new Set();   // for download step checkbox selection

// ── Upload meta persistence ───────────────────────────────────────────────────
let _saveMetaTimer = null;
function scheduleMetaSave(vid) {
  clearTimeout(_saveMetaTimer);
  _saveMetaTimer = setTimeout(() => {
    const payload = vid ? { [vid]: S.videoMeta[vid] } : S.videoMeta;
    fetch('/api/video_meta/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meta: payload })
    }).catch(() => {});
  }, 500);
}

// ── Tab router ────────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    const tab = link.dataset.tab;
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    link.classList.add('active');
    document.getElementById('tab-' + tab).classList.add('active');
    if (tab === 'sources') loadSources();
    if (tab === 'history') loadHistory();
    if (tab === 'settings') loadSettings();
    if (tab === 'calendar') loadCalendar();
  });
});

// Quality selector dynamic size update
document.addEventListener('change', e => {
  if (e.target.classList.contains('quality-selector')) {
    const sel = e.target;
    const vidId = sel.dataset.id;
    const val = sel.value;
    const size = val === '4k' ? sel.dataset.s4k : sel.dataset.s1080;
    
    // Find video item and update the size display
    const videoItem = document.getElementById('vi-' + vidId);
    if (videoItem) {
        const sizeEl = videoItem.querySelector('.video-filesize');
        if (sizeEl) {
            sizeEl.textContent = (size / 1024 / 1024).toFixed(1) + ' MB';
        }
    }
    
    // Sync to global state so it persists in the "Processing Progress" view
    const candidate = S.candidates.find(c => c.id === vidId);
    if (candidate) {
        candidate.filesize = parseInt(size);
    }
  }
});

// ── SSE connection ────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/events');

  es.addEventListener('snapshot', e => {
    const d = JSON.parse(e.data);
    Object.assign(S, d);
    // Restore persisted videoMeta from backend (snake_case -> camelCase)
    if (d.video_meta && Object.keys(d.video_meta).length) {
      S.videoMeta = d.video_meta;
    }
    renderPipeline();
  });
  es.addEventListener('state', e => {
    const d = JSON.parse(e.data);
    const prevStatus = S.status;
    Object.assign(S, d);
    if (d.video_meta) S.videoMeta = d.video_meta;
    renderPipeline();
    if (d.status === 'scan_done' && prevStatus !== 'scan_done') {
      _triggerBiliCheck();
    }
  });
  es.addEventListener('bili_check', e => {
    const d = JSON.parse(e.data);
    S.candidates.forEach(c => {
      if (c.channel_id === d.channel_id || c.channel_name === d.channel_name) {
        c._bili_check = d.result;
        _updateBiliCheckBadge(c.id, d.result);
      }
    });
  });
  es.addEventListener('progress', e => {
    const d = JSON.parse(e.data);
    S.progress[d.id] = { pct: d.pct, message: d.message };
    updateProgress(d.id, d.pct, d.message);
  });
  es.addEventListener('log', e => {
    const d = JSON.parse(e.data);
    appendLog(d.ts, d.level, d.message);
  });
  es.onerror = () => {
    setTimeout(() => { es.close(); connectSSE(); }, 3000);
  };
}

// ── Log panel ─────────────────────────────────────────────────────────────────
const logBody = document.getElementById('log-body');

function appendLog(ts, level, msg) {
  const line = document.createElement('div');
  line.className = 'log-line';
  line.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg ${level}">${escHtml(msg)}</span>`;
  logBody.appendChild(line);
  logBody.scrollTop = logBody.scrollHeight;
  // Keep max 400 lines
  while (logBody.children.length > 400) logBody.removeChild(logBody.firstChild);
}

document.getElementById('btn-clear-log').addEventListener('click', () => {
  logBody.innerHTML = '';
});

document.getElementById('btn-reset').addEventListener('click', async () => {
  if (!confirm('重置流水线？已下载但未上传的进度不会丢失。')) return;
  await api('POST', '/api/reset');
});

document.getElementById('btn-cancel').addEventListener('click', async () => {
  if (!confirm('确定要终止当前任务吗？这可能留下临时断点文件。')) return;
  document.getElementById('btn-cancel').disabled = true;
  await api('POST', '/api/cancel');
});

// ── Pipeline renderer ─────────────────────────────────────────────────────────
const stepIds = ['scan', 'download', 'transcode', 'translate', 'upload'];
const stepMap = {
  idle:            { active: 'scan' },
  scanning:        { active: 'scan', busy: true },
  scan_done:       { done: ['scan'], active: 'download' },
  downloading:     { done: ['scan'], active: 'download', busy: true },
  download_done:   { done: ['scan','download'], active: 'transcode' },
  transcoding:     { done: ['scan','download'], active: 'transcode', busy: true },
  transcode_done:  { done: ['scan','download','transcode'], active: 'translate' },
  translating:     { done: ['scan','download','transcode'], active: 'translate', busy: true },
  translate_done:  { done: ['scan','download','transcode','translate'], active: 'upload' },
  uploading:       { done: ['scan','download','transcode','translate'], active: 'upload', busy: true },
  done:            { done: ['scan','download','transcode','translate','upload'] },
};

function renderPipeline() {
  const sm = stepMap[S.status] || stepMap.idle;
  
  const btnCancel = document.getElementById('btn-cancel');
  if (sm.busy) {
    btnCancel.style.display = '';
    btnCancel.disabled = false;
  } else {
    btnCancel.style.display = 'none';
  }

  // Step indicators — done steps are clickable (jump back); active/future steps are not
  stepIds.forEach(id => {
    const el = document.getElementById('step-' + id);
    el.className = 'step';
    const isDone = (sm.done || []).includes(id);
    const isActive = sm.active === id;
    if (isDone) el.classList.add('done');
    else if (isActive) el.classList.add('active');

    if (isDone && !sm.busy) {
      el.classList.add('clickable');
      el.onclick = () => jumpToStep(id);
    } else {
      el.classList.remove('clickable');
      el.onclick = null;
    }
  });

  renderActionPanel(sm);
  renderVideoSection();
}

function renderActionPanel(sm) {
  const panel = document.getElementById('action-panel');

  const status = S.status;
  let html = '';

  if (status === 'idle') {
    html = `
      <div class="action-title">准备就绪</div>
      <div class="action-desc">点击"扫描"开始检测各来源的最新视频。</div>
      <div class="action-footer">
        <button class="btn btn-primary" onclick="doScan()">🔍 开始扫描</button>
      </div>`;

  } else if (status === 'scanning') {
    html = `
      <div class="action-title">正在扫描来源...</div>
      <div class="action-desc">正在检测各频道 / 播放列表 / 单视频的最新状态。</div>
      <div class="action-footer"><div class="spinner"></div><span style="color:var(--text2)">请稍候</span></div>`;

  } else if (status === 'scan_done') {
    const newCnt  = S.candidates.filter(c => !c.already_downloaded).length;
    const skipCnt = S.candidates.filter(c => c.already_downloaded).length;
    html = `
      <div class="action-title">扫描完成</div>
      <div class="result-summary">
        <div class="result-stat ok"><span class="num">${newCnt}</span>个新视频</div>
        <div class="result-stat skip"><span class="num">${skipCnt}</span>个已跳过</div>
      </div>
      <div class="action-desc">勾选要下载的视频，然后点击开始下载。</div>
      <div class="action-footer">
        <button class="btn btn-primary" onclick="doDownload()" ${newCnt===0?'disabled':''}>⬇ 开始下载</button>
        <button class="btn btn-ghost" onclick="doScan()">重新扫描</button>
        <label class="auto-transcode-label"><input type="checkbox" id="chk-auto-transcode"> 下载后自动转码</label>
        <label class="auto-transcode-label"><input type="checkbox" id="chk-subtitles" checked> 下载字幕</label>
      </div>`;

  } else if (status === 'downloading') {
    html = `
      <div class="action-title">正在下载...</div>
      <div class="action-desc" id="dl-status-text">准备中...</div>
      <div class="action-footer"><div class="spinner"></div></div>`;

  } else if (status === 'download_done') {
    const okCnt = S.downloaded.length;
    const errCnt = S.errors.filter(e => e.step==='download').length;
    html = `
      <div class="action-title">下载完成</div>
      <div class="result-summary">
        <div class="result-stat ok"><span class="num">${okCnt}</span>成功</div>
        ${errCnt?`<div class="result-stat fail"><span class="num">${errCnt}</span>失败</div>`:''}
      </div>
      <div class="action-desc">确认后开始转码（添加片头 + H264 标准化）。</div>
      <div class="action-footer">
        <button class="btn btn-primary" onclick="doTranscode()" ${okCnt===0?'disabled':''}>🎬 开始转码</button>
      </div>`;

  } else if (status === 'transcoding') {
    html = `
      <div class="action-title">正在转码...</div>
      <div class="action-desc">添加片头并统一编码为 H264，请耐心等待。</div>
      <div class="action-footer"><div class="spinner"></div></div>`;

  } else if (status === 'transcode_done') {
    const okCnt = S.transcoded.length;
    const errCnt = S.errors.filter(e => e.step==='transcode').length;
    html = `
      <div class="action-title">转码完成</div>
      <div class="result-summary">
        <div class="result-stat ok"><span class="num">${okCnt}</span>成功</div>
        ${errCnt?`<div class="result-stat fail"><span class="num">${errCnt}</span>失败</div>`:''}
      </div>
      <div class="action-desc">点击开始 AI 翻译视频标题，翻译完成后可上传。</div>
      <div class="action-footer">
        <button class="btn btn-primary" onclick="doTranslate()" ${okCnt===0?'disabled':''}>🌐 开始翻译</button>
        <button class="btn btn-ghost" onclick="doRescan()">🔍 重新扫描队列</button>
      </div>`;

  } else if (status === 'translating') {
    html = `
      <div class="action-title">正在翻译标题...</div>
      <div class="action-desc">调用 AI 翻译各视频标题，请稍候。</div>
      <div class="action-footer"><div class="spinner"></div><span style="color:var(--text2)">请稍候</span></div>`;

  } else if (status === 'translate_done') {
    const okCnt = S.transcoded.length;
    const errCnt = S.errors.filter(e => e.step==='translate').length;
    html = `
      <div class="action-title">翻译完成</div>
      <div class="result-summary">
        <div class="result-stat ok"><span class="num">${okCnt}</span>待上传</div>
        ${errCnt?`<div class="result-stat fail"><span class="num">${errCnt}</span>翻译失败</div>`:''}
      </div>
      <div class="action-desc">确认标题无误后上传至 B站，也可对单条视频重新翻译。</div>
      <div class="action-footer">
        <button class="btn btn-green" onclick="doUpload()" ${okCnt===0?'disabled':''}>🚀 上传至 B站</button>
        <button class="btn btn-ghost" onclick="doTranslate()">🔄 重新翻译全部</button>
        <button class="btn btn-ghost" onclick="doRescan()">🔍 重新扫描队列</button>
      </div>`;

  } else if (status === 'uploading') {
    html = `
      <div class="action-title">正在上传...</div>
      <div class="action-desc">视频上传中，请保持网络稳定。</div>
      <div class="action-footer"><div class="spinner"></div></div>`;

  } else if (status === 'done') {
    const okCnt = S.uploaded.length;
    const errCnt = S.errors.filter(e => e.step==='upload').length;
    html = `
      <div class="action-title">🎉 全部完成！</div>
      <div class="result-summary">
        <div class="result-stat ok"><span class="num">${okCnt}</span>视频上传成功</div>
        ${errCnt?`<div class="result-stat fail"><span class="num">${errCnt}</span>失败</div>`:''}
      </div>
      <div class="action-footer">
        <button class="btn btn-ghost" onclick="doReset()">↺ 开始新一轮</button>
      </div>`;
  }

  panel.innerHTML = html;
}

function renderVideoSection() {
  const section = document.getElementById('video-list-section');
  const listEl  = document.getElementById('video-list');
  const titleEl = document.getElementById('video-list-title');
  const selWrap  = document.getElementById('select-all-wrap');
  const status = S.status;

  // Choose which list to show
  let items = [];
  let showCheckbox = false;

  if (['scan_done'].includes(status)) {
    items = S.candidates;
    titleEl.textContent = '发现的视频';
    showCheckbox = true;
    selWrap.style.display = '';
  } else if (['transcode_done', 'translating', 'translate_done', 'uploading', 'done'].includes(status)) {
    items = S.transcoded;
    titleEl.textContent = '处理进度';
    showCheckbox = false;
    selWrap.style.display = 'none';
  } else if (['downloading', 'download_done', 'transcoding'].includes(status)) {
    items = S.candidates.filter(c => !c.already_downloaded);
    titleEl.textContent = '处理进度';
    showCheckbox = false;
    selWrap.style.display = 'none';
  }

  if (items.length === 0 && !['scan_done','downloading','download_done','transcoding','transcode_done','uploading','done'].includes(status)) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';

  listEl.innerHTML = '';

  items.forEach(c => {
    const prog = S.progress[c.id] || {};
    const isDone = S.uploaded.some(u => u.id === c.id)
                || S.transcoded.some(t => t.id === c.id && ['uploading','done','transcode_done'].includes(status) === false);
    const isSkip = c.already_downloaded && status === 'scan_done';
    const errEntry = S.errors.find(e => e.id === c.id);
    const isDownloaded = S.downloaded.some(d => d.id === c.id);
    const isTranscoded = S.transcoded.some(d => d.id === c.id);
    const isUploaded = S.uploaded.some(d => d.id === c.id);

    let statusBadge = '';
    if (isUploaded) statusBadge = `<span class="badge badge-done">已上传</span>`;
    else if (errEntry) {
        const stepLabel = {
            'download': '下载',
            'transcode': '转码',
            'upload': '上传'
        }[errEntry.step] || '阶段';
        statusBadge = `
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
            <span class="badge badge-fail">${errEntry.step} 失败</span>
            <button class="btn-retry" onclick="doRetry('${c.id}')">重试${stepLabel}</button>
          </div>`;
    }
    else if (isTranscoded) statusBadge = `<span class="badge badge-done">转码✓</span>`;
    else if (isDownloaded) statusBadge = `<span class="badge badge-done">下载✓</span>`;
    else if (isSkip) statusBadge = `<span class="badge badge-skip">已有</span>`;
    else statusBadge = `<span class="badge badge-${c.url_type}">${labelType(c.url_type)}</span>`;

    const pct = prog.pct ?? 0;
    const progHtml = (!isSkip && status !== 'scan_done') ? `
      <div class="video-progress">
        <div class="progress-bar"><div class="progress-fill ${pct===100?'green':''}" id="pf-${c.id}" style="width:${pct}%"></div></div>
      </div>` : '';

    const chk = showCheckbox && !isSkip
      ? `<input type="checkbox" class="video-check" data-id="${c.id}" ${selectedIds.has(c.id)?'checked':''}>`
      : '';
      
    // Quality & Filesize
    let metaExtra = '';
    
    if (showCheckbox && !isSkip) {
        const s1080 = c.size_1080p || 0;
        const s4k = c.size_4k || 0;
        
        const label1080 = s1080 ? `1080p (~${(s1080/1024/1024).toFixed(0)}MB)` : '1080p';
        const label4k = s4k ? `4K (~${(s4k/1024/1024).toFixed(0)}MB)` : '4K / HighRes';
        
        let opt4k = c.has_4k ? `<option value="4k" ${c.quality==='4k'?'selected':''}>${label4k}</option>` : '';

        metaExtra += `<select class="quality-selector" data-id="${c.id}" data-s1080="${s1080}" data-s4k="${s4k}">
            <option value="1080p" ${c.quality!=='4k'?'selected':''}>${label1080}</option>
            ${opt4k}
        </select>`;
    } else if (c.filesize) {
        // Show actual downloaded size if processing
        metaExtra += `<span class="video-filesize">${(c.filesize/1024/1024).toFixed(1)} MB</span>`;
    }

    const isDraggable = status === 'transcode_done' && isTranscoded && !isUploaded;

    const row = document.createElement('div');
    row.className = 'video-item-container' + (isDraggable ? ' draggable' : '');
    row.id = 'vic-' + c.id;
    row.dataset.id = c.id;
    if (isDraggable) {
        row.draggable = true;
        row.addEventListener('dragstart', e => {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', c.id);
            row.classList.add('dragging');
        });
        row.addEventListener('dragend', () => row.classList.remove('dragging'));
        row.addEventListener('dragover', e => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            const dragging = listEl.querySelector('.dragging');
            if (dragging && dragging !== row) {
                const rect = row.getBoundingClientRect();
                const mid = rect.top + rect.height / 2;
                if (e.clientY < mid) listEl.insertBefore(dragging, row);
                else listEl.insertBefore(dragging, row.nextSibling);
            }
        });
        row.addEventListener('drop', e => {
            e.preventDefault();
            // Sync S.transcoded order to DOM order
            const newOrder = [...listEl.querySelectorAll('.video-item-container[data-id]')]
                .map(el => el.dataset.id);
            S.transcoded.sort((a, b) => newOrder.indexOf(a.id) - newOrder.indexOf(b.id));
        });
    }

    // Video Item Row
    const itemRow = document.createElement('div');
    itemRow.className = 'video-item' + (isSkip ? ' skipped' : '') + (errEntry ? ' error' : '') + (isUploaded ? ' done' : '');
    itemRow.id = 'vi-' + c.id;

    const thumbUrl = `/api/thumb/${c.id}`;
    const showFmtToggle = showCheckbox && !isSkip && c.formats && c.formats.length;
    const expanded = !!S.formatsExpanded[c.id];
    const selectedLabel = S.selectedFormats[c.id]
        ? [...S.selectedFormats[c.id]].join('+') : (c.rec_format_id || '');
    const fmtToggleBtn = showFmtToggle
        ? `<button class="btn-fmt-toggle" data-vid="${c.id}"><span class="fmt-sel-label">${escHtml(selectedLabel)}</span><span class="fmt-arrow">${expanded ? '▲' : '▼'}</span></button>`
        : '';
    const showMetaToggle = ['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded;
    const metaExp = !!S.metaExpanded[c.id];
    const metaToggleBtn = showMetaToggle
        ? `<button class="btn-meta-toggle" data-vid="${c.id}">编辑 <span class="fmt-arrow">${metaExp ? '▲' : '▼'}</span></button>`
        : '';
    const showDelBtn = ['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded;
    const showDoneBtn = ['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded;
    const showUploadBtn = ['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded;
    const showRetranslateBtn = ['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded;
    const showStagesBtn = ['transcode_done','translate_done'].includes(status) && isTranscoded;
    const displayTitle = (S.videoMeta[c.id] && S.videoMeta[c.id].title) ? S.videoMeta[c.id].title : (c.translated_title || c.title);
    const showOrigTitle = displayTitle !== c.title;
    itemRow.innerHTML = `
      ${isDraggable ? '<div class="drag-handle">⠿</div>' : ''}
      ${chk}
      <img class="video-thumb" src="${thumbUrl}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
      <div class="video-info">
        <div class="video-title" title="${escHtml(displayTitle)}">${escHtml(displayTitle)}</div>
        ${showOrigTitle ? `<div class="video-orig-title" title="${escHtml(c.title)}">${escHtml(c.title)}</div>` : ''}
        <div class="video-meta">${c.id} · ${labelType(c.url_type)}</div>
        <div id="bili-check-${c.id}" class="bili-check-wrap">${status === 'scan_done' && !isSkip ? (c._bili_check ? _biliCheckBadgeHtml(c._bili_check) : '<span class="badge badge-checking">B站核查中</span>') : ''}</div>
      </div>
      ${progHtml}
      ${statusBadge}
      ${fmtToggleBtn}
      ${metaToggleBtn}
      ${showDelBtn ? `<button class="btn-del-meta" data-vid="${c.id}" title="从上传队列移除">✕</button>` : ''}
      ${showDoneBtn ? `<button class="btn-mark-done" data-vid="${c.id}" title="标记为已手动上传">✓ 已上传</button>` : ''}
      ${showRetranslateBtn ? `<button class="btn-retranslate btn-retry" data-vid="${c.id}" title="重新翻译此视频标题">↺ 重翻</button>` : ''}
      ${showUploadBtn ? `<button class="btn-upload-single btn-retry" data-vid="${c.id}" title="立即上传此视频">⬆ 上传</button>` : ''}
      ${showStagesBtn ? `<button class="btn-stages-toggle btn-retry" data-vid="${c.id}" title="手动修改阶段状态">⚙ 状态</button>` : ''}`;

    if (showDelBtn) {
        itemRow.querySelector('.btn-del-meta').addEventListener('click', () => {
            fetch(`/api/video_meta/${c.id}`, { method: 'DELETE' }).catch(() => {});
        });
    }
    if (showDoneBtn) {
        itemRow.querySelector('.btn-mark-done').addEventListener('click', () => {
            fetch(`/api/video_meta/${c.id}/done`, { method: 'POST' }).catch(() => {});
        });
    }
    if (showRetranslateBtn) {
        itemRow.querySelector('.btn-retranslate').addEventListener('click', () => {
            fetch(`/api/translate/${c.id}`, { method: 'POST' }).catch(() => {});
        });
    }
    if (showUploadBtn) {
        itemRow.querySelector('.btn-upload-single').addEventListener('click', () => {
            fetch(`/api/upload/${c.id}`, { method: 'POST' }).catch(() => {});
        });
    }
    
    row.appendChild(itemRow);

    // Formats List (only for scan_done and not skipped)
    if (showFmtToggle) {
        const formatsWrap = document.createElement('div');
        formatsWrap.className = 'video-formats' + (expanded ? '' : ' collapsed');

        // Auto-preselect recommended formats on first render
        if (c.rec_format_id && !S.selectedFormats[c.id]) {
            S.selectedFormats[c.id] = new Set(c.rec_format_id.split('+'));
        }

        c.formats.forEach(f => {
            if (f.is_thumbnail) return; // Skip thumbnails in the UI
            const isSelected = (S.selectedFormats[c.id] || new Set()).has(f.format_id);
            const fmtItem = document.createElement('div');
            fmtItem.className = 'format-item' + (isSelected ? ' selected' : '') + (f.recommended ? ' recommended' : '');
            fmtItem.dataset.vid = c.id;
            fmtItem.dataset.fid = f.format_id;

            const isCombo = f.vcodec !== 'none' && f.acodec !== 'none';
            const isAudio = f.vcodec === 'none' && f.acodec !== 'none';
            const isVideo = f.acodec === 'none' && f.vcodec !== 'none';

            fmtItem.innerHTML = `
                <div class="format-check"></div>
                <div class="format-id">${f.format_id}</div>
                <div class="format-res">${f.resolution} <span style="opacity:0.6">${f.ext}</span></div>
                <div class="format-size">${f.filesize ? (f.filesize/1024/1024).toFixed(1)+'M' : ''}</div>
                <div class="format-tags">
                    ${isCombo ? '<span class="tag">Combo</span>' : ''}
                    ${isVideo ? '<span class="tag video">Video</span>' : ''}
                    ${isAudio ? '<span class="tag audio">Audio</span>' : ''}
                    ${f.recommended ? '<span class="tag rec">推荐</span>' : ''}
                </div>
            `;

            fmtItem.addEventListener('click', () => {
                if (!S.selectedFormats[c.id]) S.selectedFormats[c.id] = new Set();
                const set = S.selectedFormats[c.id];
                if (set.has(f.format_id)) set.delete(f.format_id);
                else set.add(f.format_id);

                fmtItem.classList.toggle('selected');
                // Sync label on toggle button
                const lbl = itemRow.querySelector('.fmt-sel-label');
                if (lbl) lbl.textContent = [...set].join('+');
                // Ensure parent checkbox is checked if any format is selected
                if (set.size > 0) {
                    selectedIds.add(c.id);
                    const pChk = itemRow.querySelector('.video-check');
                    if (pChk) pChk.checked = true;
                }
            });

            formatsWrap.appendChild(fmtItem);
        });

        // Toggle button event
        const toggleBtn = itemRow.querySelector('.btn-fmt-toggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', () => {
                S.formatsExpanded[c.id] = !S.formatsExpanded[c.id];
                formatsWrap.classList.toggle('collapsed');
                toggleBtn.querySelector('.fmt-arrow').textContent =
                    S.formatsExpanded[c.id] ? '▲' : '▼';
            });
        }

        row.appendChild(formatsWrap);
    }

    // Upload meta editor (only when waiting for upload)
    if (['transcode_done','translate_done'].includes(status) && isTranscoded && !isUploaded) {
        if (!S.videoMeta[c.id]) {
            S.videoMeta[c.id] = {
                title: c.translated_title || c.title || '',
                tid: String(S.globalTid),
                tags: [...S.defaultTags],
                schedule_time: null,
                copyright: 1,
                source: c.url || '',
                desc: '',
                cover_text: ''
            };
        }
        // Backfill desc from server-pushed video_meta if not yet set locally
        if (S.videoMeta[c.id].desc === undefined) {
            S.videoMeta[c.id].desc = '';
        }
        if (S.videoMeta[c.id].cover_text === undefined) {
            S.videoMeta[c.id].cover_text = '';
        }
        // Ensure tags is always an array
        if (!Array.isArray(S.videoMeta[c.id].tags)) {
            S.videoMeta[c.id].tags = S.videoMeta[c.id].tags
                ? String(S.videoMeta[c.id].tags).split(',').map(t => t.trim()).filter(Boolean)
                : [...S.defaultTags];
        }
        const m = S.videoMeta[c.id];
        // Default datetime for scheduled publish: now + 5 hours (biliup requires >4h)
        const defaultDt = new Date(Date.now() + 5 * 3600 * 1000);
        const defaultDtLocal = new Date(defaultDt - defaultDt.getTimezoneOffset() * 60000)
            .toISOString().slice(0, 16);
        const minDt = new Date(Date.now() + 4 * 3600 * 1000 + 60000);
        const minDtLocal = new Date(minDt - minDt.getTimezoneOffset() * 60000)
            .toISOString().slice(0, 16);
        const isScheduled = !!m.schedule_time;
        const editor = document.createElement('div');
        editor.className = 'upload-meta-editor' + (metaExp ? '' : ' collapsed');
        editor.innerHTML = `
          <div class="meta-field">
            <label>标题</label>
            <input type="text" class="meta-title" value="${m.title.replace(/"/g, '&quot;')}">
          </div>
          <div class="meta-field">
            <label>封面文字</label>
            <input type="text" class="meta-cover-text" maxlength="6"
                   value="${escHtml(m.cover_text || '')}"
                   placeholder="留空则不加文字（最多6字）">
          </div>
          <div class="meta-field">
            <label>分区</label>
            <select class="meta-tid">${TID_OPTIONS_HTML}</select>
          </div>
          <div class="meta-field full-row">
            <label>标签</label>
            <div class="tag-input-wrap meta-tags-wrap"></div>
            <div class="tag-add-row">
              <input type="text" class="meta-tag-input" placeholder="输入后按 Enter 添加" maxlength="30">
              <button type="button" class="btn-tag-add">添加</button>
            </div>
          </div>
          <div class="meta-field full-row">
            <label>版权</label>
            <div class="schedule-row">
              <label class="radio-inline">
                <input type="radio" name="cr-${c.id}" value="1" ${m.copyright === 2 ? '' : 'checked'}> 自制
              </label>
              <label class="radio-inline">
                <input type="radio" name="cr-${c.id}" value="2" ${m.copyright === 2 ? 'checked' : ''}> 转载
              </label>
              <input type="text" class="meta-source" placeholder="转载来源 URL"
                     value="${escHtml(m.source || '')}"
                     style="${m.copyright === 2 ? '' : 'display:none'}">
            </div>
          </div>
          <div class="meta-field full-row">
            <label>发布</label>
            <div class="schedule-row">
              <label class="radio-inline">
                <input type="radio" name="sched-${c.id}" value="now" ${isScheduled ? '' : 'checked'}> 立即发布
              </label>
              <label class="radio-inline">
                <input type="radio" name="sched-${c.id}" value="scheduled" ${isScheduled ? 'checked' : ''}> 定时发布
              </label>
              <div class="schedule-dt-wrap" style="${isScheduled ? '' : 'display:none'}">
                <input type="datetime-local" class="meta-schedule-dt"
                       value="${m.schedule_time ? m.schedule_time.slice(0,16) : defaultDtLocal}"
                       min="${minDtLocal}">
                <span class="schedule-hint">⚠ 需距现在4小时以上</span>
              </div>
            </div>
          </div>
          <div class="meta-field full-row">
            <label>简介</label>
            <textarea class="meta-desc" rows="5" maxlength="2000" placeholder="B站简介（最多2000字）">${escHtml(m.desc || '')}</textarea>
            <div class="desc-counter"><span class="desc-len">${(m.desc||'').length}</span>/2000</div>
          </div>`;
        // Set select value after insertion
        editor.querySelector('.meta-tid').value = m.tid;
        // Render tag chips
        function renderMetaTags() {
            const wrap = editor.querySelector('.meta-tags-wrap');
            wrap.innerHTML = m.tags.map((t, i) => `
                <span class="tag-chip">${escHtml(t)}<button type="button" class="tag-chip-del" data-i="${i}">×</button></span>
            `).join('');
            wrap.querySelectorAll('.tag-chip-del').forEach(btn => {
                btn.addEventListener('click', () => {
                    m.tags.splice(Number(btn.dataset.i), 1);
                    renderMetaTags();
                    scheduleMetaSave(c.id);
                });
            });
        }
        renderMetaTags();
        // Add tag on button click or Enter
        const tagInput = editor.querySelector('.meta-tag-input');
        const addTag = () => {
            const val = tagInput.value.trim();
            if (val && !m.tags.includes(val)) { m.tags.push(val); renderMetaTags(); scheduleMetaSave(c.id); }
            tagInput.value = '';
        };
        editor.querySelector('.btn-tag-add').addEventListener('click', addTag);
        tagInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } });
        // Sync other fields
        editor.querySelector('.meta-title').addEventListener('input', e => { S.videoMeta[c.id].title = e.target.value; scheduleMetaSave(c.id); });
        editor.querySelector('.meta-cover-text').addEventListener('input', e => { S.videoMeta[c.id].cover_text = e.target.value; scheduleMetaSave(c.id); });
        editor.querySelector('.meta-tid').addEventListener('change', e => { S.videoMeta[c.id].tid = e.target.value; scheduleMetaSave(c.id); });
        // Copyright radio toggle
        editor.querySelectorAll(`input[name="cr-${c.id}"]`).forEach(radio => {
            radio.addEventListener('change', () => {
                const sourceInput = editor.querySelector('.meta-source');
                S.videoMeta[c.id].copyright = Number(radio.value);
                sourceInput.style.display = radio.value === '2' ? '' : 'none';
                scheduleMetaSave(c.id);
            });
        });
        editor.querySelector('.meta-source').addEventListener('input', e => { S.videoMeta[c.id].source = e.target.value; scheduleMetaSave(c.id); });
        // Schedule radio toggle
        editor.querySelectorAll(`input[name="sched-${c.id}"]`).forEach(radio => {
            radio.addEventListener('change', () => {
                const dtWrap = editor.querySelector('.schedule-dt-wrap');
                if (radio.value === 'scheduled') {
                    dtWrap.style.display = '';
                    const dtInput = editor.querySelector('.meta-schedule-dt');
                    S.videoMeta[c.id].schedule_time = dtInput.value || defaultDtLocal;
                } else {
                    dtWrap.style.display = 'none';
                    S.videoMeta[c.id].schedule_time = null;
                }
                scheduleMetaSave(c.id);
            });
        });
        editor.querySelector('.meta-schedule-dt').addEventListener('change', e => {
            S.videoMeta[c.id].schedule_time = e.target.value || null;
            scheduleMetaSave(c.id);
        });
        editor.querySelector('.meta-desc').addEventListener('input', e => {
            S.videoMeta[c.id].desc = e.target.value;
            editor.querySelector('.desc-len').textContent = e.target.value.length;
            scheduleMetaSave(c.id);
        });
        // Meta toggle button event
        const metaToggle = itemRow.querySelector('.btn-meta-toggle');
        if (metaToggle) {
            metaToggle.addEventListener('click', () => {
                S.metaExpanded[c.id] = !S.metaExpanded[c.id];
                editor.classList.toggle('collapsed');
                metaToggle.querySelector('.fmt-arrow').textContent =
                    S.metaExpanded[c.id] ? '▲' : '▼';
            });
        }
        row.appendChild(editor);
    }

    // Stages editor
    if (showStagesBtn) {
        const stagesEditor = document.createElement('div');
        stagesEditor.className = 'stages-editor collapsed';
        stagesEditor.id = 'stages-' + c.id;
        const stageLabels = { scan: '扫描', download: '下载', transcode: '转码', translate: '翻译', upload: '上传' };
        const stageKeys = ['scan', 'download', 'transcode', 'translate', 'upload'];
        const currentStages = (S.videoMeta[c.id] && S.videoMeta[c.id].stages) || {};
        stagesEditor.innerHTML = `
          <div class="stages-editor-title">手动修改阶段状态</div>
          <div class="stages-rows">
            ${stageKeys.map(k => {
              const st = currentStages[k] ? currentStages[k].status : 'pending';
              return `<div class="stages-row">
                <span class="stages-label">${stageLabels[k]}</span>
                <select class="stages-select" data-stage="${k}">
                  <option value="pending" ${st==='pending'?'selected':''}>pending</option>
                  <option value="done"    ${st==='done'?'selected':''}>done</option>
                  <option value="failed"  ${st==='failed'?'selected':''}>failed</option>
                  <option value="skipped" ${st==='skipped'?'selected':''}>skipped</option>
                </select>
              </div>`;
            }).join('')}
          </div>`;
        stagesEditor.querySelectorAll('.stages-select').forEach(sel => {
            sel.addEventListener('change', () => {
                fetch(`/api/video_meta/${c.id}/stages`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stage: sel.dataset.stage, status: sel.value })
                }).catch(() => {});
            });
        });
        const stagesBtn = itemRow.querySelector('.btn-stages-toggle');
        if (stagesBtn) {
            stagesBtn.addEventListener('click', () => {
                stagesEditor.classList.toggle('collapsed');
            });
        }
        row.appendChild(stagesEditor);
    }

    listEl.appendChild(row);
  });

  // Initialize selectedIds if scan_done and empty
  if (status === 'scan_done' && selectedIds.size === 0) {
    S.candidates.filter(c => !c.already_downloaded).forEach(c => selectedIds.add(c.id));
  }
  // Bind checkboxes
  if (showCheckbox) {
    listEl.querySelectorAll('.video-check').forEach(chk => {
      chk.checked = selectedIds.has(chk.dataset.id);
      chk.addEventListener('change', () => {
        if (chk.checked) selectedIds.add(chk.dataset.id);
        else selectedIds.delete(chk.dataset.id);
      });
    });
    const chkAll = document.getElementById('chk-select-all');
    if (chkAll) {
        chkAll.addEventListener('change', function() {
          listEl.querySelectorAll('.video-check').forEach(chk => {
            chk.checked = this.checked;
            if (this.checked) selectedIds.add(chk.dataset.id);
            else selectedIds.delete(chk.dataset.id);
          });
        });
    }
  }
}

function updateProgress(videoId, pct, msg) {
  const fill = document.getElementById('pf-' + videoId);
  if (fill) {
    fill.style.width = pct + '%';
    if (pct >= 100) fill.classList.add('green');
  }
}

// ── Pipeline actions ──────────────────────────────────────────────────────────
async function jumpToStep(step) {
  const r = await api('POST', `/api/jump/${step}`);
  if (r && r.error) alert(r.error);
}
async function doScan() { selectedIds.clear(); await api('POST', '/api/scan'); }
async function doDownload() {
  const ids = [...selectedIds];
  if (!ids.length) { alert('请至少选择一个视频'); return; }
  const autoTranscode = document.getElementById('chk-auto-transcode')?.checked || false;
  const withSubtitles = document.getElementById('chk-subtitles')?.checked ?? true;

  const payload = ids.map(id => {
    const selFormats = S.selectedFormats[id];
    let format_id = null;
    if (selFormats && selFormats.size > 0) {
        format_id = [...selFormats].join('+');
    } else {
        // Fallback to server-recommended format
        const c = S.candidates.find(x => x.id === id);
        if (c && c.rec_format_id) format_id = c.rec_format_id;
    }

    return { id: id, format_id: format_id, quality: 'custom' };
  });
  
  await api('POST', '/api/download', { video_ids: payload, auto_transcode: autoTranscode, with_subtitles: withSubtitles });
}
async function doTranscode() { await api('POST', '/api/transcode'); }
async function doUpload() {
  const meta = {};
  S.transcoded.forEach(c => {
    const m = S.videoMeta[c.id];
    if (m) meta[c.id] = m;
  });
  await api('POST', '/api/upload', { meta });
}
async function doReset()     { await api('POST', '/api/reset'); }
async function doTranslate() {
  await api('POST', '/api/translate');
}

async function doRescan() {
  const r = await api('POST', '/api/video_meta/rescan');
  const ts = new Date().toLocaleTimeString('zh-CN', {hour12: false});
  if (r && r.added > 0) appendLog(ts, 'info', `重新扫描完成，新增 ${r.added} 个视频到上传队列`);
  else appendLog(ts, 'info', '重新扫描完成，没有发现新视频');
}
async function doRetry(id)     { await api('POST', '/api/retry', { video_id: id }); }

// ── Sources tab ───────────────────────────────────────────────────────────────
async function loadSources() {
  const list = document.getElementById('source-list');
  list.innerHTML = '<div class="empty-state">加载中...</div>';
  const sources = await api('GET', '/api/sources');
  if (!sources || !sources.length) {
    list.innerHTML = '<div class="empty-state">暂无来源，请添加 YouTube 频道、视频或播放列表 URL</div>';
    return;
  }
  list.innerHTML = sources.map((s, i) => {
    let titleHtml = escHtml(s.title || s.url);
    if (s.title && s.title !== s.url) {
        titleHtml = `<div style="font-size: 14px; font-weight: 600; color: var(--text1); margin-bottom: 2px;">${escHtml(s.title)}</div>
                     <div style="font-size: 11px; color: var(--text3); word-break: break-all;">${escHtml(s.url)}</div>`;
    } else {
        titleHtml = `<strong style="word-break: break-all;">${escHtml(s.url)}</strong>`;
    }
    return `
    <div class="source-item" style="align-items: flex-start; padding: 12px;">
      <span class="badge badge-${s.type}" style="margin-top:2px;">${labelType(s.type)}</span>
      <div class="source-url" style="flex:1;">
        ${titleHtml}
      </div>
      <button class="btn-del" onclick="deleteSource(${i})" title="删除" style="margin-top:2px;">✕</button>
    </div>`;
  }).join('');
}

document.getElementById('btn-add-source').addEventListener('click', async () => {
  const input = document.getElementById('new-source-url');
  const btn = document.getElementById('btn-add-source');
  const url = input.value.trim();
  if (!url) return;
  
  // Provide loading feedback since fetching title takes a few seconds
  btn.disabled = true;
  btn.textContent = '获取信息...';
  
  const res = await api('POST', '/api/sources', { url });
  
  btn.disabled = false;
  btn.textContent = '➕ 添加';
  
  if (res && res.error) {
    alert('添加失败: ' + res.error);
    return;
  }
  
  input.value = '';
  loadSources();
});

document.getElementById('new-source-url').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('btn-add-source').click();
});

async function deleteSource(idx) {
  if (!confirm('确定删除该来源？')) return;
  await api('DELETE', `/api/sources/${idx}`);
  loadSources();
}

// ── History tab ───────────────────────────────────────────────────────────────
async function loadHistory() {
  const list = document.getElementById('history-list');
  const ids = await api('GET', '/api/history');
  document.getElementById('history-count').textContent = ids ? ids.length : 0;
  if (!ids || !ids.length) {
    list.innerHTML = '<div class="empty-state">暂无历史记录</div>';
    return;
  }
  // Show newest first
  list.innerHTML = [...ids].reverse().map(id => `
    <div class="history-item">
      <span class="history-id">${escHtml(id)}</span>
      <a href="https://www.youtube.com/watch?v=${escHtml(id)}" target="_blank"
         style="color:var(--accent);font-size:11px;text-decoration:none;">↗ YT</a>
    </div>`).join('');
}

// ── Tid options HTML (cloned from settings select, so we only maintain one copy) ──
function getTidOptionsHTML() {
  const src = document.getElementById('cfg-tid');
  if (src) return src.innerHTML;
  return '<option value="171">171 · 电子竞技</option>';
}
let TID_OPTIONS_HTML = '';
document.addEventListener('DOMContentLoaded', () => { TID_OPTIONS_HTML = getTidOptionsHTML(); });

// ── Tag chip helpers ──────────────────────────────────────────────────────────
function renderCfgTags(tags) {
  const wrap = document.getElementById('cfg-tags-wrap');
  if (!wrap) return;
  wrap.innerHTML = tags.map((t, i) => `
    <span class="tag-chip">${escHtml(t)}<button type="button" class="tag-chip-del" data-i="${i}">×</button></span>
  `).join('');
  wrap.querySelectorAll('.tag-chip-del').forEach(btn => {
    btn.addEventListener('click', () => {
      S.defaultTags.splice(Number(btn.dataset.i), 1);
      renderCfgTags(S.defaultTags);
    });
  });
}

function addCfgTag() {
  const input = document.getElementById('cfg-tag-input');
  const val = input.value.trim();
  if (!val) return;
  if (!S.defaultTags.includes(val)) {
    S.defaultTags.push(val);
    renderCfgTags(S.defaultTags);
  }
  input.value = '';
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-tag-add').addEventListener('click', addCfgTag);
  document.getElementById('cfg-tag-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addCfgTag(); }
  });
});
async function loadSettings() {
  const cfg = await api('GET', '/api/config');
  if (!cfg) return;
  document.getElementById('cfg-proxy').value = cfg.proxy || '';
  S.globalTid = cfg.tid || 122;
  // Set the select to match current tid, default to 122
  const tidSel = document.getElementById('cfg-tid');
  tidSel.value = String(cfg.tid || 122);
  // If no matching option found, default to 171
  if (!tidSel.value) tidSel.value = '122';
  document.getElementById('cfg-intro').value = cfg.intro_path || '';
  document.getElementById('cfg-desc').value  = cfg.desc_prefix || '';
  document.getElementById('cfg-zhipu-key').value = cfg.zhipu_key || '';
  document.getElementById('cfg-upload-interval').value = cfg.upload_interval ?? 30;
  document.getElementById('cfg-bili-check-sim').value = cfg.bili_check_similarity ?? 0.75;
  S.defaultTags = cfg.default_tags || ['YouTube', '搬运', 'AI翻译', '中字'];
  renderCfgTags(S.defaultTags);

  const bs = await api('GET', '/api/bilibili/status');
  const dot = document.getElementById('bili-dot');
  const label = document.getElementById('bili-label');
  const info  = document.getElementById('bili-login-info');
  if (bs && bs.logged_in) {
    dot.className = 'status-dot ' + (bs.warning ? 'warn' : 'ok');
    label.textContent = '已登录 B站';
    info.textContent = `最后登录: ${bs.last_login}（${bs.age_days} 天前）${bs.warning ? ' ⚠️ 建议刷新' : ''}`;
  } else {
    dot.className = 'status-dot err';
    label.textContent = '未登录 B站';
    info.textContent = '未找到 cookies.json，请先登录。';
  }
}

document.getElementById('btn-save-config').addEventListener('click', async () => {
  const cfg = {
    proxy:        document.getElementById('cfg-proxy').value.trim(),
    tid:          parseInt(document.getElementById('cfg-tid').value) || 122,
    intro_path:   document.getElementById('cfg-intro').value.trim(),
    desc_prefix:  document.getElementById('cfg-desc').value,
    zhipu_key:    document.getElementById('cfg-zhipu-key').value.trim(),
    default_tags: S.defaultTags,
    upload_interval: parseInt(document.getElementById('cfg-upload-interval').value) || 0,
    bili_check_similarity: parseFloat(document.getElementById('cfg-bili-check-sim').value) || 0.75,
  };
  const res = await api('POST', '/api/config', cfg);
  if (res && res.ok) {
    const ok = document.getElementById('save-ok');
    ok.style.display = '';
    setTimeout(() => { ok.style.display = 'none'; }, 2500);
  }
});

// ── Bilibili status (sidebar) ─────────────────────────────────────────────────
async function checkBiliStatus() {
  const bs = await api('GET', '/api/bilibili/status');
  const dot = document.getElementById('bili-dot');
  const label = document.getElementById('bili-label');
  if (!bs) return;
  if (bs.logged_in) {
    dot.className = 'status-dot ' + (bs.warning ? 'warn' : 'ok');
    label.textContent = bs.warning ? 'B站 ⚠️ 需刷新' : 'B站 已登录';
  } else {
    dot.className = 'status-dot err';
    label.textContent = 'B站 未登录';
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function labelType(t) {
  return { channel: '频道', video: '单视频', playlist: '播放列表' }[t] || t;
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function api(method, url, body) {
  try {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    return await r.json();
  } catch(e) {
    console.error('API error', method, url, e);
    return null;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
connectSSE();
checkBiliStatus();

// ── Calendar ──────────────────────────────────────────────────────────────────

const Cal = { weekOffset: 0, meta: null };
const CAL_START_HOUR = 6;
const CAL_END_HOUR   = 23;
const CAL_DAY_NAMES  = ['周日','周一','周二','周三','周四','周五','周六'];
const CAL_MONTHS     = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function calWeekSunday(offset) {
  const now = new Date();
  const sun = new Date(now);
  sun.setHours(0, 0, 0, 0);
  sun.setDate(now.getDate() - now.getDay() + offset * 7);
  return sun;
}

function calWeekLabel(sun) {
  const sat = new Date(sun); sat.setDate(sun.getDate() + 6);
  const fmt = d => `${d.getMonth()+1}月${d.getDate()}日`;
  const year = sun.getFullYear();
  const jan4 = new Date(year, 0, 4);
  const weekNo = Math.ceil(((sun - jan4) / 86400000 + jan4.getDay() + 1) / 7);
  return `${year}年 第${weekNo}周  (${fmt(sun)} – ${fmt(sat)})`;
}

async function loadCalendar() {
  Cal.meta = await api('GET', '/api/video_meta');
  renderCalendar();
  document.getElementById('cal-prev').onclick  = () => { Cal.weekOffset--; renderCalendar(); };
  document.getElementById('cal-next').onclick  = () => { Cal.weekOffset++; renderCalendar(); };
  document.getElementById('cal-today').onclick = () => { Cal.weekOffset = 0; renderCalendar(); };
}

function renderCalendar() {
  const sun = calWeekSunday(Cal.weekOffset);
  document.getElementById('cal-week-label').textContent = calWeekLabel(sun);

  const todayStr = (() => { const t = new Date(); t.setHours(0,0,0,0); return t.toDateString(); })();
  const grid = document.getElementById('cal-grid');
  grid.innerHTML = '';

  // Header
  grid.appendChild(Object.assign(document.createElement('div'), { className: 'cal-header-gutter' }));
  const days = [];
  for (let d = 0; d < 7; d++) {
    const dt = new Date(sun); dt.setDate(sun.getDate() + d);
    days.push(dt);
    const isToday = dt.toDateString() === todayStr;
    const hdr = document.createElement('div');
    hdr.className = 'cal-header-day' + (isToday ? ' today' : '');
    hdr.innerHTML = `<span class="cal-date-num">${dt.getDate()}</span>${CAL_DAY_NAMES[d]} · ${CAL_MONTHS[dt.getMonth()]}`;
    grid.appendChild(hdr);
  }

  // Build event map  "YYYY-MM-DD|H" -> [{vid, m, dt, minute}]
  const eventMap = {};
  const unscheduled = [];

  for (const [vid, m] of Object.entries(Cal.meta || {})) {
    let dt = null;
    if (m.schedule_time) {
      dt = new Date(m.schedule_time);
    } else if (m.uploaded && m.uploaded_at) {
      dt = new Date(m.uploaded_at * 1000);
    }
    if (!dt || isNaN(dt)) { unscheduled.push({vid, m}); continue; }

    const dateKey = `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
    const key = `${dateKey}|${dt.getHours()}`;
    if (!eventMap[key]) eventMap[key] = [];
    eventMap[key].push({ vid, m, dt, minute: dt.getMinutes() });
  }

  // Hour rows
  for (let h = CAL_START_HOUR; h <= CAL_END_HOUR; h++) {
    const label = document.createElement('div');
    label.className = 'cal-hour-label';
    label.textContent = `${String(h).padStart(2,'0')}:00`;
    grid.appendChild(label);

    for (let d = 0; d < 7; d++) {
      const dt = days[d];
      const isToday = dt.toDateString() === todayStr;
      const cell = document.createElement('div');
      cell.className = 'cal-day-cell' + (isToday ? ' today-col' : '');

      const dateKey = `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
      (eventMap[`${dateKey}|${h}`] || []).forEach(ev => {
        const card = document.createElement('div');
        let cls, badge;
        if (ev.m.uploaded)         { cls = 'cal-card uploaded';      badge = '✓ 已上传'; }
        else if (ev.m.schedule_time) { cls = 'cal-card scheduled';   badge = '⏰ 定时';  }
        else                         { cls = 'cal-card pending-upload'; badge = '⬆ 待传'; }
        card.className = cls;
        card.style.top = `${ev.minute}px`;
        const timeStr = `${String(ev.dt.getHours()).padStart(2,'0')}:${String(ev.dt.getMinutes()).padStart(2,'0')}`;
        card.innerHTML = `
          <img src="/api/thumb/${ev.vid}" alt="" loading="lazy">
          <span class="cal-card-time">${timeStr}</span>
          <span class="cal-card-title">${escHtml(ev.m.title)}</span>
          <span class="cal-card-badge">${badge}</span>`;
        cell.appendChild(card);
      });
      grid.appendChild(cell);
    }
  }

  // Unscheduled tray
  const tray = document.getElementById('cal-tray');
  if (!unscheduled.length) { tray.style.display = 'none'; return; }
  tray.style.display = '';
  document.getElementById('cal-tray-count').textContent = unscheduled.length;
  const trayItems = document.getElementById('cal-tray-items');
  trayItems.innerHTML = '';
  unscheduled.forEach(({vid, m}) => {
    const card = document.createElement('div');
    card.className = 'cal-tray-card';
    card.innerHTML = `
      <img src="/api/thumb/${vid}" alt="" loading="lazy">
      <div class="cal-tray-card-body">
        <div class="cal-tray-card-title">${escHtml(m.title)}</div>
      </div>`;
    trayItems.appendChild(card);
  });
}

// ── B站作者核查 ───────────────────────────────────────────────────────────────
function _biliCheckBadgeHtml(result) {
  if (!result) return '';
  if (result.status === 'found') {
    const pct = Math.round((result.similarity || 0) * 100);
    const name = escHtml(result.match_name || '');
    const url = escHtml(result.bili_url || '');
    return `<a class="badge badge-warn bili-check-link" href="${url}" target="_blank" rel="noopener"
        title="B站疑似同名UP主：${name}（相似度${pct}%）&#10;点击查看主页">⚠ 疑似有B站号</a>`;
  }
  if (result.status === 'error') {
    return `<span class="badge badge-fail" title="B站核查请求失败">查询失败</span>`;
  }
  return '';
}

function _updateBiliCheckBadge(vid, result) {
  const el = document.getElementById('bili-check-' + vid);
  if (!el) return;
  el.innerHTML = _biliCheckBadgeHtml(result);
}

function _triggerBiliCheck() {
  const seen = new Set();
  const channels = [];
  S.candidates.forEach(c => {
    const key = c.channel_id || c.channel_name;
    if (key && !seen.has(key)) {
      seen.add(key);
      channels.push({ channel_id: c.channel_id || '', channel_name: c.channel_name || '' });
    }
  });
  if (!channels.length) return;
  fetch('/api/bili_check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channels }),
  }).catch(() => {});
}
