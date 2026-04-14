/**
 * dragdrop.js — Global drag-and-drop file upload module.
 *
 * Listens for drag events anywhere on the page and shows a full-screen overlay.
 * On drop, uploads files to POST /api/v1/upload via multipart FormData with XHR
 * progress tracking. Uses the existing showToast() from main.js for notifications.
 *
 * Guard: drops inside elements matching .upload-modal are ignored so existing
 * upload modals handle their own drops without conflict.
 */

'use strict';

(function initDragDrop() {
  // ── State ──────────────────────────────────────────────────────────────────
  let _dragCounter = 0;
  const UPLOAD_URL = '/api/v1/upload';

  // ── Overlay element ────────────────────────────────────────────────────────
  const overlay = document.getElementById('dnd-overlay');
  if (!overlay) return;

  const fileListEl = overlay.querySelector('#dnd-file-list');
  const progressEl = overlay.querySelector('#dnd-progress');
  const progressBarEl = overlay.querySelector('#dnd-progress-bar');
  const statusEl = overlay.querySelector('#dnd-status');

  function showOverlay() {
    overlay.style.display = 'flex';
    // Reset progress UI
    if (fileListEl) fileListEl.innerHTML = '';
    if (progressEl) progressEl.style.display = 'none';
    if (statusEl) statusEl.textContent = 'Drop files here';
  }

  function hideOverlay() {
    overlay.style.display = 'none';
    _dragCounter = 0;
  }

  // ── Drag events ────────────────────────────────────────────────────────────

  document.addEventListener('dragenter', function (e) {
    e.preventDefault();
    // Ignore drags that originate inside an existing upload modal
    if (e.target && e.target.closest && e.target.closest('.upload-modal')) return;
    _dragCounter++;
    if (_dragCounter === 1) showOverlay();
  });

  document.addEventListener('dragleave', function (e) {
    if (e.target && e.target.closest && e.target.closest('.upload-modal')) return;
    _dragCounter--;
    if (_dragCounter <= 0) hideOverlay();
  });

  document.addEventListener('dragover', function (e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });

  document.addEventListener('drop', function (e) {
    e.preventDefault();
    // Let dedicated upload modals handle their own drops
    if (e.target && e.target.closest && e.target.closest('.upload-modal')) {
      hideOverlay();
      return;
    }

    const files = Array.from(e.dataTransfer.files || []);
    if (files.length === 0) {
      hideOverlay();
      return;
    }

    renderFileList(files);
    uploadFiles(files);
  });

  // Close overlay on Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && overlay.style.display === 'flex') {
      hideOverlay();
    }
  });

  // ── File list rendering ────────────────────────────────────────────────────

  function renderFileList(files) {
    if (!fileListEl) return;
    fileListEl.innerHTML = files.map(f => `
      <div class="flex items-center gap-2 text-sm text-slate-300">
        <svg class="w-4 h-4 flex-shrink-0 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round"
                d="M9 13h6m-3-3v6m5 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414A1 1 0 0119 9.414V19a2 2 0 01-2 2z"/>
        </svg>
        <span class="truncate max-w-xs">${escapeHtml(f.name)}</span>
        <span class="text-slate-500 flex-shrink-0">${formatBytes(f.size)}</span>
      </div>`).join('');

    if (statusEl) statusEl.textContent = `Uploading ${files.length} file${files.length !== 1 ? 's' : ''}…`;
    if (progressEl) progressEl.style.display = 'block';
    if (progressBarEl) progressBarEl.style.width = '0%';
  }

  // ── XHR upload with progress ───────────────────────────────────────────────

  function uploadFiles(files) {
    const formData = new FormData();
    for (const file of files) {
      formData.append('files', file, file.name);
    }

    const xhr = new XMLHttpRequest();

    xhr.upload.addEventListener('progress', function (e) {
      if (!e.lengthComputable || !progressBarEl) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      progressBarEl.style.width = pct + '%';
    });

    xhr.addEventListener('load', function () {
      hideOverlay();
      if (xhr.status >= 200 && xhr.status < 300) {
        let count = files.length;
        try {
          const data = JSON.parse(xhr.responseText);
          count = data.count || count;
        } catch (_) {}
        if (typeof showToast === 'function') {
          showToast(`Uploaded ${count} file${count !== 1 ? 's' : ''} successfully`, 'success');
        }
      } else if (xhr.status === 401 || xhr.status === 403) {
        if (typeof Auth !== 'undefined' && typeof Auth.logout === 'function') {
          Auth.logout();
        } else if (typeof showToast === 'function') {
          showToast('Not authorised — please log in', 'error');
        }
      } else {
        let msg = 'Upload failed';
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.detail) msg = data.detail;
        } catch (_) {}
        if (typeof showToast === 'function') {
          showToast(msg, 'error');
        }
      }
    });

    xhr.addEventListener('error', function () {
      hideOverlay();
      if (typeof showToast === 'function') {
        showToast('Upload failed — network error', 'error');
      }
    });

    xhr.addEventListener('abort', function () {
      hideOverlay();
    });

    xhr.open('POST', UPLOAD_URL);

    // Inject auth token if available
    const token = (typeof Auth !== 'undefined') ? Auth.getToken() : localStorage.getItem('app_token');
    if (token) {
      xhr.setRequestHeader('Authorization', 'Bearer ' + token);
    }

    xhr.send(formData);
  }
})();
