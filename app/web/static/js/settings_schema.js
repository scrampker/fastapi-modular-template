/**
 * settings_schema.js
 *
 * Schema-driven settings UI renderer for the admin settings page.
 *
 * Adding a new setting requires only one object in SETTINGS_SCHEMA.
 * No HTML edits needed.
 *
 * Each descriptor:
 *   id          {string}   - Matches the GlobalSettings / UserSettings field name
 *   label       {string}   - Human-readable label
 *   description {string}   - Helper text shown below the widget
 *   type        {string}   - "toggle" | "select" | "number" | "text" | "password" | "button"
 *   tab         {string}   - "global" | "user"  (maps to the API tier)
 *   group       {string}   - Card heading within the tab
 *   visibility  {string}   - "superadmin" | "all"
 *   options     {Array}    - [{ value, label }] for type:"select"
 *   min         {number}   - For type:"number"
 *   max         {number}   - For type:"number"
 *   step        {number}   - For type:"number" (default 1)
 *   unit        {string}   - Optional unit suffix shown next to the input
 *   default     {*}        - Value used when the live setting is absent
 *   dangerous   {boolean}  - Renders a warning badge next to the label
 *   buttonLabel {string}   - For type:"button" — the button text
 *   buttonAction{string}   - For type:"button" — JS expression to eval (receives settings context)
 */

'use strict';

/* ── Schema ──────────────────────────────────────────────────────────────── */

const SETTINGS_SCHEMA = [

  // ── Global / Platform ─────────────────────────────────────────────────────

  {
    id: 'auth_providers',
    label: 'Authentication providers',
    description: 'Enabled sign-in methods. At least one must remain active.',
    type: 'select',
    tab: 'global',
    group: 'Authentication',
    visibility: 'superadmin',
    options: [
      { value: 'local',       label: 'Local password' },
      { value: 'cloudflare',  label: 'Cloudflare Zero Trust' },
      { value: 'azure',       label: 'Azure AD / Entra ID' },
    ],
    default: ['local'],
    dangerous: true,
  },

  {
    id: 'session_timeout_minutes',
    label: 'Session timeout',
    description: 'Idle sessions expire after this period.',
    type: 'number',
    tab: 'global',
    group: 'Security',
    visibility: 'superadmin',
    min: 5,
    max: 1440,
    step: 1,
    unit: 'min',
    default: 15,
  },

  {
    id: 'password_min_length',
    label: 'Minimum password length',
    description: 'Applies to new passwords and resets (6 – 128 chars).',
    type: 'number',
    tab: 'global',
    group: 'Security',
    visibility: 'superadmin',
    min: 6,
    max: 128,
    step: 1,
    unit: 'chars',
    default: 8,
  },

  {
    id: 'retention_days_default',
    label: 'Default data retention',
    description: 'How long records are kept before automatic purge.',
    type: 'number',
    tab: 'global',
    group: 'Security',
    visibility: 'superadmin',
    min: 1,
    max: 3650,
    step: 1,
    unit: 'days',
    default: 90,
  },

  {
    id: 'branding_app_name',
    label: 'Application name',
    description: 'Shown in the sidebar, browser tab, and system emails.',
    type: 'text',
    tab: 'global',
    group: 'Branding',
    visibility: 'superadmin',
    default: 'MyApp',
  },

  {
    id: 'smtp_host',
    label: 'SMTP host',
    description: 'Outbound mail server hostname.',
    type: 'text',
    tab: 'global',
    group: 'Email / SMTP',
    visibility: 'superadmin',
    default: '',
  },

  {
    id: 'smtp_port',
    label: 'SMTP port',
    description: 'Typically 587 (STARTTLS) or 465 (TLS).',
    type: 'number',
    tab: 'global',
    group: 'Email / SMTP',
    visibility: 'superadmin',
    min: 1,
    max: 65535,
    step: 1,
    default: 587,
  },

  {
    id: 'smtp_from',
    label: 'From address',
    description: 'Sender address shown on outgoing emails.',
    type: 'text',
    tab: 'global',
    group: 'Email / SMTP',
    visibility: 'superadmin',
    default: '',
  },

  {
    id: 'smtp_password',
    label: 'SMTP password',
    description: 'Leave blank to keep the current password.',
    type: 'password',
    tab: 'global',
    group: 'Email / SMTP',
    visibility: 'superadmin',
    default: '',
  },

  {
    id: '_smtp_test',
    label: 'Send test email',
    description: 'Sends a test message to the SMTP from address.',
    type: 'button',
    tab: 'global',
    group: 'Email / SMTP',
    visibility: 'superadmin',
    buttonLabel: 'Send test email',
    buttonAction: 'smtpTest',
    default: null,
  },

  // ── User preferences ─────────────────────────────────────────────────────

  {
    id: 'theme',
    label: 'Theme',
    description: 'Interface colour scheme.',
    type: 'select',
    tab: 'user',
    group: 'Appearance',
    visibility: 'all',
    options: [
      { value: 'dark',   label: 'Dark' },
      { value: 'light',  label: 'Light' },
      { value: 'system', label: 'Follow system' },
    ],
    default: 'system',
  },

  {
    id: 'timezone',
    label: 'Timezone',
    description: 'Used for timestamps displayed in the UI.',
    type: 'text',
    tab: 'user',
    group: 'Appearance',
    visibility: 'all',
    default: 'UTC',
  },

  {
    id: 'page_size',
    label: 'Default page size',
    description: 'Rows per page in tables and lists.',
    type: 'number',
    tab: 'user',
    group: 'Preferences',
    visibility: 'all',
    min: 10,
    max: 200,
    step: 5,
    unit: 'rows',
    default: 25,
  },

  {
    id: 'notifications_enabled',
    label: 'In-app notifications',
    description: 'Show notification badges and alerts in the UI.',
    type: 'toggle',
    tab: 'user',
    group: 'Preferences',
    visibility: 'all',
    default: true,
  },
];

/* ── Renderer ────────────────────────────────────────────────────────────── */

/**
 * Render settings widgets into the element matching `containerId`.
 *
 * @param {string} containerId   - DOM id of the target element
 * @param {string} tab           - "global" | "user"
 * @param {boolean} isSuperadmin - Hide superadmin-only fields for non-superadmins
 * @param {Object} currentValues - Map of id → current value (from GET response)
 * @param {Function} onSave      - Async callback(tab, id, value) called on auto-save
 */
function renderSettingsWidgets(containerId, tab, isSuperadmin, currentValues, onSave) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const visible = SETTINGS_SCHEMA.filter(s =>
    s.tab === tab &&
    (s.visibility === 'all' || (s.visibility === 'superadmin' && isSuperadmin))
  );

  // Group by `group`
  const groups = {};
  for (const schema of visible) {
    if (!groups[schema.group]) groups[schema.group] = [];
    groups[schema.group].push(schema);
  }

  const cards = Object.entries(groups).map(([groupName, fields]) => {
    const fieldHtml = fields.map((s, idx) => renderField(s, currentValues, idx === fields.length - 1)).join('');
    return `
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 settings-card" data-group="${escapeHtml(groupName)}">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-5">
          ${escapeHtml(groupName)}
        </h2>
        <div class="space-y-0">${fieldHtml}</div>
        <div class="flex items-center justify-end gap-3 mt-6 pt-4 border-t border-slate-700">
          <span class="save-msg text-sm hidden" data-group="${escapeHtml(groupName)}"></span>
          <button
            class="save-btn px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500
                   text-white transition-colors disabled:opacity-50"
            data-tab="${escapeHtml(tab)}"
            data-group="${escapeHtml(groupName)}">
            Save
          </button>
        </div>
      </div>`;
  });

  container.innerHTML = cards.join('');

  // Wire up Save buttons
  container.querySelectorAll('.save-btn').forEach(btn => {
    btn.addEventListener('click', () => _handleGroupSave(btn, tab, groups, onSave));
  });

  // Wire up toggle buttons
  container.querySelectorAll('[data-toggle-id]').forEach(toggleBtn => {
    toggleBtn.addEventListener('click', () => {
      const id = toggleBtn.dataset.toggleId;
      const isOn = toggleBtn.dataset.toggleValue === 'true';
      _setToggle(toggleBtn, !isOn);
    });
  });

  // Wire up visibility toggles (fields that show/hide dependents)
  _bindConditionalVisibility(container);
}

/* ── Field renderers ─────────────────────────────────────────────────────── */

function renderField(schema, currentValues, isLast) {
  const val = currentValues.hasOwnProperty(schema.id) ? currentValues[schema.id] : schema.default;
  const borderClass = isLast ? '' : 'border-b border-slate-700';
  const wrapper = `<div class="py-4 ${borderClass}" data-field-id="${escapeHtml(schema.id)}">`;
  const closeWrapper = '</div>';

  switch (schema.type) {
    case 'toggle':
      return wrapper + renderToggle(schema, val) + closeWrapper;
    case 'select':
      return wrapper + renderSelect(schema, val) + closeWrapper;
    case 'number':
      return wrapper + renderNumber(schema, val) + closeWrapper;
    case 'text':
      return wrapper + renderText(schema, val) + closeWrapper;
    case 'password':
      return wrapper + renderPassword(schema, val) + closeWrapper;
    case 'button':
      return wrapper + renderActionButton(schema) + closeWrapper;
    default:
      return '';
  }
}

function _labelHtml(schema) {
  const dangerBadge = schema.dangerous
    ? '<span class="ml-2 px-1.5 py-0.5 rounded text-xs font-medium bg-amber-900/60 text-amber-400 border border-amber-700/50">caution</span>'
    : '';
  return `<p class="text-sm font-medium text-slate-200">${escapeHtml(schema.label)}${dangerBadge}</p>`;
}

function _descriptionHtml(schema) {
  return schema.description
    ? `<p class="text-xs text-slate-500 mt-0.5">${escapeHtml(schema.description)}</p>`
    : '';
}

function renderToggle(schema, val) {
  const isOn = Boolean(val);
  const thumbX = isOn ? 'translate-x-4' : 'translate-x-0.5';
  const bg     = isOn ? 'bg-blue-600'    : 'bg-slate-600';
  return `
    <div class="flex items-center justify-between">
      <div>
        ${_labelHtml(schema)}
        ${_descriptionHtml(schema)}
      </div>
      <button
        data-toggle-id="${escapeHtml(schema.id)}"
        data-toggle-value="${isOn}"
        class="${bg} relative inline-flex h-5 w-9 rounded-full transition-colors
               focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2
               focus:ring-offset-slate-800 flex-shrink-0 ml-4">
        <span class="${thumbX} inline-block w-4 h-4 mt-0.5 rounded-full bg-white shadow transition-transform"></span>
      </button>
    </div>`;
}

function renderSelect(schema, val) {
  // Multi-value select (auth_providers is list[str])
  const isMulti = Array.isArray(schema.default);

  if (isMulti) {
    const currentArr = Array.isArray(val) ? val : (val ? [val] : []);
    const checkboxes = schema.options.map(opt => {
      const checked = currentArr.includes(opt.value) ? 'checked' : '';
      return `
        <label class="flex items-center gap-2 text-sm text-slate-300 cursor-pointer select-none">
          <input type="checkbox" value="${escapeHtml(opt.value)}" ${checked}
                 class="multi-select-cb rounded bg-slate-700 border-slate-500 text-blue-500
                        focus:ring-blue-500 focus:ring-offset-slate-800"
                 data-field-id="${escapeHtml(schema.id)}" />
          ${escapeHtml(opt.label)}
        </label>`;
    }).join('');
    return `
      <div>
        ${_labelHtml(schema)}
        ${_descriptionHtml(schema)}
        <div class="mt-2 space-y-1.5">${checkboxes}</div>
      </div>`;
  }

  const opts = schema.options.map(opt => {
    const sel = opt.value === val ? 'selected' : '';
    return `<option value="${escapeHtml(opt.value)}" ${sel}>${escapeHtml(opt.label)}</option>`;
  }).join('');

  return `
    <div>
      ${_labelHtml(schema)}
      ${_descriptionHtml(schema)}
      <select
        data-field-id="${escapeHtml(schema.id)}"
        class="mt-2 w-full max-w-xs px-3 py-2 rounded-lg bg-slate-900 border border-slate-600
               text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
        ${opts}
      </select>
    </div>`;
}

function renderNumber(schema, val) {
  const unit = schema.unit ? `<span class="text-slate-500 text-sm ml-2">${escapeHtml(schema.unit)}</span>` : '';
  const numVal = val !== null && val !== undefined ? val : (schema.default ?? '');
  return `
    <div>
      ${_labelHtml(schema)}
      <div class="flex items-center mt-1.5">
        <input type="number"
               data-field-id="${escapeHtml(schema.id)}"
               value="${escapeHtml(String(numVal))}"
               min="${schema.min ?? ''}"
               max="${schema.max ?? ''}"
               step="${schema.step ?? 1}"
               class="w-32 px-3 py-2 rounded-lg bg-slate-900 border border-slate-600 text-slate-100
                      text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
        ${unit}
      </div>
      ${_descriptionHtml(schema)}
    </div>`;
}

function renderText(schema, val) {
  const textVal = val !== null && val !== undefined ? val : '';
  return `
    <div>
      ${_labelHtml(schema)}
      <input type="text"
             data-field-id="${escapeHtml(schema.id)}"
             value="${escapeHtml(String(textVal))}"
             class="mt-1.5 w-full max-w-sm px-3 py-2 rounded-lg bg-slate-900 border border-slate-600
                    text-slate-100 text-sm placeholder-slate-500 focus:outline-none
                    focus:ring-2 focus:ring-blue-500" />
      ${_descriptionHtml(schema)}
    </div>`;
}

function renderPassword(schema, val) {
  return `
    <div>
      ${_labelHtml(schema)}
      <input type="password"
             data-field-id="${escapeHtml(schema.id)}"
             value=""
             placeholder="Leave blank to keep current"
             class="mt-1.5 w-full max-w-sm px-3 py-2 rounded-lg bg-slate-900 border border-slate-600
                    text-slate-100 text-sm placeholder-slate-500 focus:outline-none
                    focus:ring-2 focus:ring-blue-500" />
      ${_descriptionHtml(schema)}
    </div>`;
}

function renderActionButton(schema) {
  return `
    <div class="flex items-center justify-between">
      <div>
        ${_labelHtml(schema)}
        ${_descriptionHtml(schema)}
      </div>
      <button
        data-action="${escapeHtml(schema.buttonAction)}"
        class="action-btn px-4 py-2 rounded-lg text-sm bg-slate-700 hover:bg-slate-600
               text-slate-300 transition-colors ml-4 flex-shrink-0">
        ${escapeHtml(schema.buttonLabel)}
      </button>
    </div>`;
}

/* ── Toggle state helper ─────────────────────────────────────────────────── */

function _setToggle(btn, newValue) {
  btn.dataset.toggleValue = String(newValue);
  if (newValue) {
    btn.classList.remove('bg-slate-600');
    btn.classList.add('bg-blue-600');
  } else {
    btn.classList.remove('bg-blue-600');
    btn.classList.add('bg-slate-600');
  }
  const thumb = btn.querySelector('span');
  if (thumb) {
    thumb.classList.toggle('translate-x-4',   newValue);
    thumb.classList.toggle('translate-x-0.5', !newValue);
  }
}

/* ── Value extraction ────────────────────────────────────────────────────── */

/**
 * Read current values for all fields belonging to a group from the DOM.
 * Returns a plain object ready to PATCH.
 */
function _extractGroupValues(container, groupName) {
  const card = container.querySelector(`[data-group="${CSS.escape(groupName)}"].settings-card`);
  if (!card) return {};

  const result = {};

  // Toggles
  card.querySelectorAll('[data-toggle-id]').forEach(btn => {
    result[btn.dataset.toggleId] = btn.dataset.toggleValue === 'true';
  });

  // Single selects
  card.querySelectorAll('select[data-field-id]').forEach(sel => {
    result[sel.dataset.fieldId] = sel.value;
  });

  // Multi-select checkboxes (grouped by field id)
  const multiGroups = {};
  card.querySelectorAll('.multi-select-cb[data-field-id]').forEach(cb => {
    const fid = cb.dataset.fieldId;
    if (!multiGroups[fid]) multiGroups[fid] = [];
    if (cb.checked) multiGroups[fid].push(cb.value);
  });
  Object.assign(result, multiGroups);

  // Number + text inputs
  card.querySelectorAll('input[data-field-id]:not([type="checkbox"])').forEach(inp => {
    const fid = inp.dataset.fieldId;
    if (!fid || inp.type === 'password') {
      // Password: only include if non-empty
      if (inp.type === 'password' && inp.value) {
        result[fid] = inp.value;
      }
      return;
    }
    result[fid] = inp.type === 'number' ? Number(inp.value) : inp.value;
  });

  // Remove button pseudo-fields (ids starting with _)
  for (const k of Object.keys(result)) {
    if (k.startsWith('_')) delete result[k];
  }

  return result;
}

/* ── Save handler ────────────────────────────────────────────────────────── */

async function _handleGroupSave(btn, tab, groups, onSave) {
  const groupName = btn.dataset.group;
  const container = btn.closest('[id]');
  if (!container) return;

  const values = _extractGroupValues(container, groupName);
  if (!Object.keys(values).length) return;

  const msgEl = container.querySelector(`.save-msg[data-group="${CSS.escape(groupName)}"]`);
  btn.disabled = true;
  btn.textContent = 'Saving…';
  if (msgEl) { msgEl.textContent = ''; msgEl.classList.add('hidden'); }

  try {
    await onSave(tab, values);
    if (msgEl) {
      msgEl.textContent = 'Saved.';
      msgEl.className = 'save-msg text-sm text-green-400';
      setTimeout(() => { msgEl.classList.add('hidden'); }, 3000);
    }
    showToast('Settings saved.', 'success');
  } catch (err) {
    const msg = err.message || 'Failed to save.';
    if (msgEl) {
      msgEl.textContent = msg;
      msgEl.className = 'save-msg text-sm text-red-400';
    }
    showToast(msg, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

/* ── Conditional visibility ──────────────────────────────────────────────── */

function _bindConditionalVisibility(container) {
  // No conditional visibility rules in current schema — placeholder for future use.
  // Pattern: [data-show-when-id="<id>"][data-show-when-value="<value>"]
}

/* ── Cross-tab search ────────────────────────────────────────────────────── */

/**
 * Filter the rendered widgets by a query string.
 * Matches against label, description, and id (case-insensitive).
 *
 * @param {string} containerId
 * @param {string} query
 */
function filterSettingsWidgets(containerId, query) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const q = query.trim().toLowerCase();

  container.querySelectorAll('[data-field-id]').forEach(fieldEl => {
    if (!q) {
      fieldEl.classList.remove('hidden');
      return;
    }
    const id    = (fieldEl.dataset.fieldId || '').toLowerCase();
    const label = (fieldEl.querySelector('.text-slate-200')?.textContent || '').toLowerCase();
    const desc  = (fieldEl.querySelector('.text-slate-500')?.textContent || '').toLowerCase();
    const match = id.includes(q) || label.includes(q) || desc.includes(q);
    fieldEl.classList.toggle('hidden', !match);
  });

  // Hide cards that have all fields hidden; show cards that have at least one match
  container.querySelectorAll('.settings-card').forEach(card => {
    const visible = card.querySelectorAll('[data-field-id]:not(.hidden)');
    card.classList.toggle('hidden', q !== '' && visible.length === 0);
  });
}

/* ── API save helpers ────────────────────────────────────────────────────── */

/**
 * Returns the PATCH URL for a given tab.
 *   global → /api/v1/settings/global
 *   user   → /api/v1/settings/users/me
 */
function _settingsApiUrl(tab) {
  return tab === 'global' ? '/api/v1/settings/global' : '/api/v1/settings/users/me';
}

/**
 * PATCH the settings endpoint for the given tab.
 * Throws an Error with a user-friendly message on failure.
 *
 * @param {string} tab    - "global" | "user"
 * @param {Object} values - Field values to merge
 */
async function patchSettings(tab, values) {
  const url = _settingsApiUrl(tab);
  const res = await fetch(url, {
    method: 'PATCH',
    headers: { ...Auth.getHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(values),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * GET settings for the given tab.
 * Returns an object (may be empty on network failure).
 */
async function fetchSettings(tab) {
  const url = _settingsApiUrl(tab);
  try {
    const res = await fetch(url, { headers: Auth.getHeaders() });
    if (!res.ok) return {};
    return await res.json();
  } catch (_) {
    return {};
  }
}
