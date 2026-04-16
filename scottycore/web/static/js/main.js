/**
 * MyApp — main.js
 * Global utilities, HTMX lifecycle hooks, auth helpers.
 */

'use strict';

/* ── Auth helpers ──────────────────────────────────────────────────────── */

const Auth = {
  getToken() {
    return localStorage.getItem('app_token');
  },

  getHeaders() {
    const token = this.getToken();
    return token ? { 'Authorization': `Bearer ${token}` } : {};
  },

  isLoggedIn() {
    return !!this.getToken();
  },

  logout() {
    localStorage.removeItem('app_token');
    localStorage.removeItem('app_token_type');
    window.location.href = '/login';
  },
};

/* ── authFetch convenience wrapper ────────────────────────────────────── */

function authFetch(url, opts = {}) {
  return fetch(url, {
    ...opts,
    headers: { ...Auth.getHeaders(), ...(opts.headers || {}) },
  });
}

/* ── Global fetch interceptor: auto-attach Bearer token for same-origin ── */

(function installFetchInterceptor() {
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    let url = typeof input === 'string' ? input : (input && input.url) || '';
    // Only attach token for same-origin absolute/relative API paths
    const isSameOrigin = url.startsWith('/') || url.startsWith(window.location.origin);
    if (isSameOrigin) {
      const token = Auth.getToken();
      if (token) {
        const headers = new Headers(init.headers || (typeof input !== 'string' ? input.headers : undefined) || {});
        if (!headers.has('Authorization')) {
          headers.set('Authorization', 'Bearer ' + token);
        }
        init.headers = headers;
      }
    }
    return originalFetch.call(this, input, init).then(function (resp) {
      if (!isSameOrigin || url.indexOf('/api/') === -1) return resp;
      var path = window.location.pathname;
      // Auto-logout on 401 (avoid redirect loop from /login itself)
      if (resp.status === 401 && path !== '/login') {
        Auth.logout();
      }
      // Redirect to 2FA setup on 403 + totp_setup_required
      if (resp.status === 403 && path !== '/login' && path !== '/setup-2fa') {
        resp.clone().json().then(function (body) {
          if (body && body.totp_setup_required) {
            window.location.href = '/setup-2fa';
          }
        }).catch(function () {});
      }
      return resp;
    });
  };
})();

/* ── HTMX global request header injection ─────────────────────────────── */

document.addEventListener('htmx:configRequest', function (evt) {
  const token = Auth.getToken();
  if (token) {
    evt.detail.headers['Authorization'] = `Bearer ${token}`;
  }
});

/* ── HTMX 401 / 403 handling ──────────────────────────────────────────── */

document.addEventListener('htmx:responseError', function (evt) {
  const status = evt.detail.xhr.status;
  if (status === 401 || status === 403) {
    Auth.logout();
  }
});

/* ── Top progress bar ─────────────────────────────────────────────────── */

(function setupProgressBar() {
  const bar = document.createElement('div');
  bar.id = 'htmx-indicator';
  document.body.prepend(bar);

  document.addEventListener('htmx:beforeRequest',  () => { bar.style.display = 'block'; });
  document.addEventListener('htmx:afterRequest',    () => { bar.style.display = 'none';  });
})();

/* ── Global XSS-safe escape ───────────────────────────────────────────── */

function escapeHtml(str) {
  if (!str && str !== 0) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/* ── Tenant dropdown HTMX response formatter ──────────────────────────── */

document.addEventListener('htmx:afterRequest', function (evt) {
  const target = evt.detail.target;
  if (!target) return;

  // Tenant dropdown — raw API JSON -> rendered list items
  if (target.id === 'tenant-dropdown-list') {
    try {
      const data = JSON.parse(evt.detail.xhr.responseText);
      if (!data.items || data.items.length === 0) {
        target.innerHTML = '<div class="px-3 py-2 text-xs text-slate-500">No tenants found.</div>';
        return;
      }

      const slug = window.location.pathname.split('/')[2] || '';

      target.innerHTML = data.items.map(c => `
        <a href="/c/${escapeHtml(c.slug)}/dashboard"
           class="block px-3 py-2 text-sm transition-colors ${c.slug === slug ? 'bg-blue-600/20 text-blue-300' : 'text-slate-300 hover:bg-slate-700 hover:text-white'}">
          <span class="font-medium">${escapeHtml(c.name)}</span>
          <span class="text-xs text-slate-500 ml-1">${escapeHtml(c.slug)}</span>
        </a>`).join('');
    } catch (e) {
      // Response was already HTML (server-side rendered) — leave it
    }
  }
});

/* ── Utility: format bytes ────────────────────────────────────────────── */

function formatBytes(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(1)} ${units[i]}`;
}

/* ── Utility: relative time ───────────────────────────────────────────── */

function timeAgo(dateStr) {
  if (!dateStr) return '—';
  const diff = Date.now() - new Date(dateStr).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/* ── Command Palette ──────────────────────────────────────────────────── */

/**
 * Alpine.js component for the Ctrl+K / Cmd+K command palette.
 * Registered as a global function so Alpine can pick it up via x-data.
 */
function commandPalette() {
  return {
    open: false,
    query: '',
    results: {},
    loading: false,
    selectedIndex: 0,

    get hasResults() {
      return Object.values(this.results).some(arr => arr && arr.length > 0);
    },

    /** Flattened ordered list of all result items for keyboard navigation. */
    get flatItems() {
      const order = ['items', 'tenants', 'settings'];
      const flat = [];
      for (const key of order) {
        if (this.results[key]) {
          for (const item of this.results[key]) {
            flat.push(item);
          }
        }
      }
      return flat;
    },

    /** Return the flat index for a given item (for highlight comparison). */
    flatIndex(item) {
      return this.flatItems.indexOf(item);
    },

    toggle() {
      if (this.open) {
        this.close();
      } else {
        this.open = true;
        this.$nextTick(() => {
          const input = document.getElementById('command-palette-input');
          if (input) input.focus();
        });
      }
    },

    close() {
      this.open = false;
      this.query = '';
      this.results = {};
      this.selectedIndex = 0;
      this.loading = false;
    },

    async search() {
      const q = this.query.trim();
      if (!q) {
        this.results = {};
        this.selectedIndex = 0;
        return;
      }

      this.loading = true;
      try {
        // Detect current tenant slug from URL (/c/<slug>/...)
        const slugMatch = window.location.pathname.match(/^\/c\/([^/]+)/);
        const slug = slugMatch ? slugMatch[1] : null;
        const params = new URLSearchParams({ q });
        if (slug) params.set('tenant', slug);

        const res = await fetch(`/api/v1/search?${params.toString()}`, {
          headers: Auth.getHeaders(),
        });

        if (res.status === 401 || res.status === 403) {
          Auth.logout();
          return;
        }

        if (!res.ok) {
          this.results = {};
          return;
        }

        const data = await res.json();
        this.results = data.results || {};
        this.selectedIndex = 0;
      } catch (_err) {
        this.results = {};
      } finally {
        this.loading = false;
      }
    },

    moveDown() {
      const total = this.flatItems.length;
      if (total === 0) return;
      this.selectedIndex = (this.selectedIndex + 1) % total;
      this._scrollSelectedIntoView();
    },

    moveUp() {
      const total = this.flatItems.length;
      if (total === 0) return;
      this.selectedIndex = (this.selectedIndex - 1 + total) % total;
      this._scrollSelectedIntoView();
    },

    navigate() {
      const item = this.flatItems[this.selectedIndex];
      if (item && item.url) {
        window.location.href = item.url;
        this.close();
      }
    },

    _scrollSelectedIntoView() {
      this.$nextTick(() => {
        const container = document.getElementById('command-palette-results');
        if (!container) return;
        const highlighted = container.querySelector('[data-selected]');
        if (highlighted) highlighted.scrollIntoView({ block: 'nearest' });
      });
    },
  };
}

/* ── Global Ctrl+K / Cmd+K shortcut ──────────────────────────────────── */

document.addEventListener('keydown', function (e) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    // Delegate to the Alpine component if it exists
    const el = document.getElementById('command-palette');
    if (el && el._x_dataStack) {
      const component = el._x_dataStack[0];
      if (component && typeof component.toggle === 'function') {
        component.toggle();
      }
    }
  }
});

/* ── Toast notifications ──────────────────────────────────────────────── */

/**
 * Show a toast notification.
 * @param {string} message  - Text to display
 * @param {'success'|'error'|'info'} [type] - Visual variant (default: 'info')
 * @param {number} [duration] - Auto-dismiss after ms (default: 4000)
 */
function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const colors = {
    success: 'bg-green-900/90 border-green-700 text-green-200',
    error:   'bg-red-900/90   border-red-700   text-red-200',
    info:    'bg-slate-800    border-slate-600  text-slate-200',
  };

  const icons = {
    success: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>',
    error:   '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>',
    info:    '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
  };

  const toast = document.createElement('div');
  toast.className = `flex items-start gap-3 px-4 py-3 rounded-lg border shadow-lg text-sm pointer-events-auto
                     transition-all duration-300 opacity-0 translate-y-2 ${colors[type] || colors.info}`;

  toast.innerHTML = `
    <svg class="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">${icons[type] || icons.info}</svg>
    <span class="flex-1">${escapeHtml(message)}</span>
    <button class="flex-shrink-0 ml-1 opacity-60 hover:opacity-100 transition-opacity" onclick="this.closest('[data-toast]').remove()">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
      </svg>
    </button>`;
  toast.dataset.toast = '';

  container.appendChild(toast);

  // Animate in
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      toast.classList.remove('opacity-0', 'translate-y-2');
    });
  });

  // Auto-dismiss
  const timer = setTimeout(() => dismissToast(toast), duration);
  toast.addEventListener('mouseenter', () => clearTimeout(timer));
  toast.addEventListener('mouseleave', () => setTimeout(() => dismissToast(toast), 1500));
}

function dismissToast(toast) {
  if (!toast || !toast.parentNode) return;
  toast.classList.add('opacity-0', 'translate-y-2');
  setTimeout(() => toast.remove(), 300);
}
