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
  selectedFormats: {}, // videoID -> Set of formatIDs
  uploadMeta: {},      // videoID -> { title, tid, tags }
  globalTid: 171,
};
let selectedIds = new Set();   // for download step checkbox selection

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
    Object.assign(S, JSON.parse(e.data));
    renderPipeline();
  });
  es.addEventListener('state', e => {
    const d = JSON.parse(e.data);
    Object.assign(S, d);
    renderPipeline();
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
const stepIds = ['scan', 'download', 'transcode', 'upload'];
const stepMap = {
  idle:           { active: 'scan' },
  scanning:        { active: 'scan', busy: true },
  scan_done:       { done: ['scan'], active: 'download' },
  downloading:     { done: ['scan'], active: 'download', busy: true },
  download_done:   { done: ['scan','download'], active: 'transcode' },
  transcoding:     { done: ['scan','download'], active: 'transcode', busy: true },
  transcode_done:  { done: ['scan','download','transcode'], active: 'upload' },
  uploading:       { done: ['scan','download','transcode'], active: 'upload', busy: true },
  done:            { done: ['scan','download','transcode','upload'] },
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

  // Step indicators
  stepIds.forEach(id => {
    const el = document.getElementById('step-' + id);
    el.className = 'step';
    if ((sm.done || []).includes(id)) el.classList.add('done');
    else if (sm.active === id) el.classList.add('active');
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
      <div class="action-desc">确认后将视频上传至 B站。</div>
      <div class="action-footer">
        <button class="btn btn-green" onclick="doUpload()" ${okCnt===0?'disabled':''}>🚀 上传至 B站</button>
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
  } else if (['downloading', 'download_done', 'transcoding', 'transcode_done', 'uploading', 'done'].includes(status)) {
    // Show downloaded list with progress
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

    const row = document.createElement('div');
    row.className = 'video-item-container';
    row.id = 'vic-' + c.id;
    
    // Video Item Row
    const itemRow = document.createElement('div');
    itemRow.className = 'video-item' + (isSkip ? ' skipped' : '') + (errEntry ? ' error' : '') + (isUploaded ? ' done' : '');
    itemRow.id = 'vi-' + c.id;
    
    itemRow.innerHTML = `
      ${chk}
      <div class="video-info">
        <div class="video-title" title="${escHtml(c.title)}">${escHtml(c.title)}</div>
        <div class="video-meta">${c.id} · ${labelType(c.url_type)}</div>
      </div>
      ${progHtml}
      ${statusBadge}`;
    
    row.appendChild(itemRow);

    // Formats List (only for scan_done and not skipped)
    if (showCheckbox && !isSkip && c.formats && c.formats.length) {
        const formatsWrap = document.createElement('div');
        formatsWrap.className = 'video-formats';

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
                // Ensure parent checkbox is checked if any format is selected
                if (set.size > 0) {
                    selectedIds.add(c.id);
                    const pChk = itemRow.querySelector('.video-check');
                    if (pChk) pChk.checked = true;
                }
            });
            
            formatsWrap.appendChild(fmtItem);
        });
        row.appendChild(formatsWrap);
    }

    // Upload meta editor (only when waiting for upload)
    if (status === 'transcode_done' && isTranscoded && !isUploaded) {
        if (!S.uploadMeta[c.id]) {
            S.uploadMeta[c.id] = {
                title: c.title || '',
                tid: String(S.globalTid),
                tags: 'YouTube,搬运,AI翻译,中字'
            };
        }
        const m = S.uploadMeta[c.id];
        const editor = document.createElement('div');
        editor.className = 'upload-meta-editor';
        editor.innerHTML = `
          <div class="meta-field">
            <label>标题</label>
            <input type="text" class="meta-title" value="${m.title.replace(/"/g, '&quot;')}">
          </div>
          <div class="meta-field">
            <label>分区</label>
            <select class="meta-tid">${TID_OPTIONS_HTML}</select>
          </div>
          <div class="meta-field">
            <label>标签 <span style="opacity:.5;font-size:10px">逗号分隔</span></label>
            <input type="text" class="meta-tags" value="${m.tags}">
          </div>`;
        // Set select value after insertion
        editor.querySelector('.meta-tid').value = m.tid;
        // Sync changes back to S.uploadMeta
        editor.querySelector('.meta-title').addEventListener('input', e => { S.uploadMeta[c.id].title = e.target.value; });
        editor.querySelector('.meta-tid').addEventListener('change', e => { S.uploadMeta[c.id].tid = e.target.value; });
        editor.querySelector('.meta-tags').addEventListener('input', e => { S.uploadMeta[c.id].tags = e.target.value; });
        row.appendChild(editor);
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
async function doScan() { selectedIds.clear(); await api('POST', '/api/scan'); }
async function doDownload() {
  const ids = [...selectedIds];
  if (!ids.length) { alert('请至少选择一个视频'); return; }
  
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
  
  await api('POST', '/api/download', { video_ids: payload });
}
async function doTranscode() { await api('POST', '/api/transcode'); }
async function doUpload() {
  const meta = {};
  S.transcoded.forEach(c => {
    const m = S.uploadMeta[c.id];
    if (m) meta[c.id] = m;
  });
  await api('POST', '/api/upload', { meta });
}
async function doReset()     { await api('POST', '/api/reset'); }
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
async function loadSettings() {
  const cfg = await api('GET', '/api/config');
  if (!cfg) return;
  document.getElementById('cfg-proxy').value = cfg.proxy || '';
  S.globalTid = cfg.tid || 171;
  // Set the select to match current tid, default to 171
  const tidSel = document.getElementById('cfg-tid');
  tidSel.value = String(cfg.tid || 171);
  // If no matching option found, default to 171
  if (!tidSel.value) tidSel.value = '171';
  document.getElementById('cfg-intro').value = cfg.intro_path || '';
  document.getElementById('cfg-desc').value  = cfg.desc_prefix || '';
  document.getElementById('cfg-zhipu-key').value = cfg.zhipu_key || '';

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
    proxy:      document.getElementById('cfg-proxy').value.trim(),
    tid:        parseInt(document.getElementById('cfg-tid').value) || 171,
    intro_path: document.getElementById('cfg-intro').value.trim(),
    desc_prefix:document.getElementById('cfg-desc').value,
    zhipu_key:  document.getElementById('cfg-zhipu-key').value.trim(),
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
