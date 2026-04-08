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
 *
 * Layout overrides (admin-only):
 *   Superadmins can drag-and-drop fields to reorder them, toggle visibility
 *   (superadmin-only vs. all users), and move fields between tabs via
 *   right-click context menu.  Overrides are stored server-side under the
 *   reserved key `_layout` in the global settings object so they survive
 *   page reloads.
 */

'use strict';

/* ── Layout override state ───────────────────────────────────────────────── */

/**
 * Persisted per-field overrides: { [id]: { visibility?, tab?, group?, order? } }
 * Loaded from / saved to the `_layout` key of the global settings endpoint.
 * Backward-compat: a plain string value is treated as { visibility: value }.
 */
let _settingsLayoutOverrides = {};

/** True while the superadmin is in layout-edit mode. */
let _settingsEditMode = false;

/** Field id currently being dragged (set during dragstart). */
let _settingsDragId = null;

/** Which container/tab is currently rendered (set by renderSettingsWidgets). */
let _settingsCurrentTab = null;
let _settingsCurrentContainerId = null;
let _settingsCurrentIsSuperadmin = false;
let _settingsCurrentValues = {};
let _settingsCurrentOnSave = null;

/* ── Layout override helpers ─────────────────────────────────────────────── */

function _getLayoutOverride(schema) {
  const o = _settingsLayoutOverrides[schema.id];
  if (!o) return null;
  // Backward-compat: old format was just a string ('superadmin' | 'all')
  if (typeof o === 'string') return { visibility: o };
  return o;
}

/**
 * Effective visibility for a field, considering admin layout overrides.
 * Returns 'superadmin' or 'all'.
 */
function _getEffectiveVisibility(schema) {
  const o = _getLayoutOverride(schema);
  return o?.visibility || schema.visibility || 'all';
}

/**
 * Effective tab for a field, considering admin layout overrides.
 * When a field is moved to a different visibility tier the tab follows
 * (e.g. moving from 'all' → 'superadmin' puts it on the 'global' tab).
 */
function _getEffectiveTab(schema) {
  const o = _getLayoutOverride(schema);
  if (o?.tab) return o.tab;
  const vis = _getEffectiveVisibility(schema);
  if (vis === 'all' && schema.tab === 'global') return schema._originalTab || 'user';
  if (vis === 'superadmin' && schema.tab !== 'global') return 'global';
  return schema.tab;
}

/** Effective group heading for a field. */
function _getEffectiveGroup(schema) {
  const o = _getLayoutOverride(schema);
  return o?.group || schema.group || 'Other';
}

/** Effective sort order within a group (lower = earlier). */
function _getEffectiveOrder(schema) {
  const o = _getLayoutOverride(schema);
  return o?.order ?? 999;
}

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
 * Layout overrides (superadmin only):
 *   - Call initSettingsLayoutOverrides(overrides) before the first render to
 *     pre-load persisted overrides (typically from the `_layout` key returned
 *     by GET /api/v1/settings/global).
 *   - Superadmins see a pencil (✎) button next to the tab heading that toggles
 *     layout-edit mode.  In edit mode every field gains a drag handle for
 *     reordering and a lock icon for toggling visibility tier.
 *   - Right-click any field (superadmin only, regardless of edit mode) to open
 *     a context menu with "Move to tab" and visibility-toggle options.
 *   - Changes are persisted to PATCH /api/v1/settings/global as `{ _layout: {…} }`.
 *
 * @param {string}   containerId   - DOM id of the target element
 * @param {string}   tab           - "global" | "user"
 * @param {boolean}  isSuperadmin  - Hide superadmin-only fields for non-superadmins
 * @param {Object}   currentValues - Map of id → current value (from GET response)
 * @param {Function} onSave        - Async callback(tab, values) called on save
 */
function renderSettingsWidgets(containerId, tab, isSuperadmin, currentValues, onSave) {
  const container = document.getElementById(containerId);
  if (!container) return;

  // Persist context for re-renders triggered by layout edits
  _settingsCurrentTab = tab;
  _settingsCurrentContainerId = containerId;
  _settingsCurrentIsSuperadmin = isSuperadmin;
  _settingsCurrentValues = currentValues;
  _settingsCurrentOnSave = onSave;

  // Filter fields visible for this tab (respecting layout overrides)
  const visible = SETTINGS_SCHEMA.filter(s => {
    const effTab = _getEffectiveTab(s);
    const effVis = _getEffectiveVisibility(s);
    if (effVis === 'superadmin' && !isSuperadmin) return false;
    return effTab === tab;
  });

  // Sort by effective order, then group
  visible.sort((a, b) => _getEffectiveOrder(a) - _getEffectiveOrder(b));

  const groups = {};
  for (const schema of visible) {
    const g = _getEffectiveGroup(schema);
    if (!groups[g]) groups[g] = [];
    groups[g].push(schema);
  }

  const editBanner = _settingsEditMode ? `
    <div class="flex items-center gap-2 mb-4 px-4 py-2 rounded-lg bg-amber-900/20 border border-amber-700/40 text-amber-400 text-xs">
      <span>&#9998; Layout edit mode — drag &#9776; to reorder, click lock to toggle visibility, right-click to move tabs.</span>
      <button onclick="settingsToggleEditMode()"
              class="ml-auto underline cursor-pointer bg-transparent border-0 text-amber-400 text-xs">
        Done
      </button>
    </div>` : '';

  const cards = Object.entries(groups).map(([groupName, fields]) => {
    const fieldHtml = fields.map((s, idx) => renderField(s, currentValues, idx === fields.length - 1, isSuperadmin)).join('');
    return `
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 settings-card" data-group="${escapeHtml(groupName)}">
        <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-5"
            data-settings-group="${escapeHtml(groupName)}">
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

  // Edit-mode toggle button (superadmin only, shown above the first tab's content)
  const editToggle = isSuperadmin ? `
    <div class="flex justify-end mb-2">
      <button onclick="settingsToggleEditMode()"
              id="settingsEditBtn-${escapeHtml(tab)}"
              title="Edit layout — change visibility and order of settings"
              class="text-xs px-2 py-1 rounded border transition-colors
                     ${_settingsEditMode
                       ? 'border-amber-600 text-amber-400 bg-amber-900/20'
                       : 'border-slate-600 text-slate-400 hover:text-slate-200 hover:border-slate-500'}">
        &#9998; Edit layout
      </button>
    </div>` : '';

  container.innerHTML = editToggle + editBanner + (cards.join('') || '<p class="text-slate-500 text-sm">No settings in this section.</p>');

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

  // Wire up right-click context menus (superadmin only)
  if (isSuperadmin) {
    container.querySelectorAll('[data-field-id]').forEach(fieldEl => {
      fieldEl.addEventListener('contextmenu', e => {
        e.preventDefault();
        _settingsWidgetContextMenu(e, fieldEl.dataset.fieldId, tab);
      });
    });
  }

  // Initialise drag-and-drop if in edit mode
  if (_settingsEditMode) _initSettingsDrag(container);
}

/**
 * Pre-load persisted layout overrides before calling renderSettingsWidgets.
 * Typically called once after fetching settings:
 *   const settings = await fetchSettings('global');
 *   if (settings._layout) initSettingsLayoutOverrides(settings._layout);
 *
 * @param {Object} overrides - { [fieldId]: { visibility?, tab?, group?, order? } }
 */
function initSettingsLayoutOverrides(overrides) {
  _settingsLayoutOverrides = overrides && typeof overrides === 'object' ? overrides : {};
}

/**
 * Toggle layout-edit mode on/off and re-render the current tab.
 * Called by the "Edit layout" button rendered in renderSettingsWidgets.
 */
function settingsToggleEditMode() {
  _settingsEditMode = !_settingsEditMode;
  if (_settingsCurrentContainerId) {
    renderSettingsWidgets(
      _settingsCurrentContainerId,
      _settingsCurrentTab,
      _settingsCurrentIsSuperadmin,
      _settingsCurrentValues,
      _settingsCurrentOnSave,
    );
  }
}

/* ── Field renderers ─────────────────────────────────────────────────────── */

/**
 * @param {Object}  schema        - SETTINGS_SCHEMA entry
 * @param {Object}  currentValues - Live value map
 * @param {boolean} isLast        - Whether this is the last field in its group
 * @param {boolean} isSuperadmin  - Show edit controls for superadmins
 */
function renderField(schema, currentValues, isLast, isSuperadmin) {
  const val = currentValues.hasOwnProperty(schema.id) ? currentValues[schema.id] : schema.default;
  const borderClass = isLast ? '' : 'border-b border-slate-700';
  const effVis = _getEffectiveVisibility(schema);
  const isSuperadminField = effVis === 'superadmin';

  // Left accent border for superadmin-only fields
  const accentStyle = isSuperadminField ? ' style="border-left:3px solid rgb(245 158 11 / 0.6);padding-left:12px;"' : '';

  // Edit controls: drag handle + visibility lock toggle
  let editControls = '';
  if (_settingsEditMode && isSuperadmin) {
    const lockIcon  = isSuperadminField ? '&#128274;' : '&#128275;';
    const lockColor = isSuperadminField ? 'text-amber-400' : 'text-green-400';
    const lockTitle = isSuperadminField
      ? 'Superadmin-only — click to make visible to all users'
      : 'Visible to all — click to make superadmin-only';
    editControls = `
      <span class="settings-drag-handle cursor-grab text-slate-500 text-sm px-1 flex-shrink-0
                   select-none hover:text-slate-300 transition-colors"
            draggable="true"
            data-drag-id="${escapeHtml(schema.id)}"
            title="Drag to reorder">&#9776;</span>
      <button type="button"
              onclick="_settingsToggleVisibility('${escapeHtml(schema.id)}')"
              class="${lockColor} bg-transparent border-0 text-sm px-1 flex-shrink-0 cursor-pointer
                     hover:opacity-70 transition-opacity"
              title="${escapeHtml(lockTitle)}">${lockIcon}</button>`;
  }

  const wrapper = `<div class="py-4 ${borderClass} ${_settingsEditMode ? 'flex items-center gap-2' : ''}"
                        data-field-id="${escapeHtml(schema.id)}"${accentStyle}>`;
  const closeWrapper = '</div>';

  let fieldHtml;
  switch (schema.type) {
    case 'toggle':   fieldHtml = renderToggle(schema, val); break;
    case 'select':   fieldHtml = renderSelect(schema, val); break;
    case 'number':   fieldHtml = renderNumber(schema, val); break;
    case 'text':     fieldHtml = renderText(schema, val);   break;
    case 'password': fieldHtml = renderPassword(schema, val); break;
    case 'button':   fieldHtml = renderActionButton(schema); break;
    default:         return '';
  }

  return wrapper + editControls + `<div class="${_settingsEditMode ? 'flex-1 min-w-0' : ''}">${fieldHtml}</div>` + closeWrapper;
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

/* ── Layout edit helpers ─────────────────────────────────────────────────── */

/**
 * Merge `changes` into the override record for `fieldId` and persist to the
 * server as PATCH /api/v1/settings/global with body `{ _layout: { … } }`.
 * Re-renders the current container afterwards.
 *
 * @param {string} fieldId  - SETTINGS_SCHEMA id
 * @param {Object} changes  - Partial override { visibility?, tab?, group?, order? }
 */
async function _settingsUpdateLayout(fieldId, changes) {
  const existing = _settingsLayoutOverrides[fieldId];
  const base = (existing && typeof existing === 'object')
    ? existing
    : (typeof existing === 'string' ? { visibility: existing } : {});
  _settingsLayoutOverrides[fieldId] = { ...base, ...changes };

  try {
    await fetch('/api/v1/settings/global', {
      method: 'PATCH',
      headers: { ...Auth.getHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ _layout: _settingsLayoutOverrides }),
    });
  } catch (_) {
    showToast('Failed to save layout', 'error');
  }

  if (_settingsCurrentContainerId) {
    renderSettingsWidgets(
      _settingsCurrentContainerId,
      _settingsCurrentTab,
      _settingsCurrentIsSuperadmin,
      _settingsCurrentValues,
      _settingsCurrentOnSave,
    );
  }
}

/**
 * Toggle a field between 'superadmin' and 'all' visibility.
 * Also adjusts the effective tab to match: superadmin → global, all → original.
 */
function _settingsToggleVisibility(fieldId) {
  const schema = SETTINGS_SCHEMA.find(s => s.id === fieldId);
  if (!schema) return;
  const current = _getEffectiveVisibility(schema);
  const newVis = current === 'superadmin' ? 'all' : 'superadmin';
  const newTab = newVis === 'superadmin'
    ? 'global'
    : (schema.tab === 'global' ? (schema._originalTab || 'user') : schema.tab);
  _settingsUpdateLayout(fieldId, { visibility: newVis, tab: newTab });
}

/**
 * Move a field to a different tab (and set visibility accordingly).
 * Called from the context menu.
 */
function _settingsMoveWidget(fieldId, targetTab) {
  document.querySelectorAll('.settings-ctx-menu').forEach(el => el.remove());
  const vis = targetTab === 'global' ? 'superadmin' : 'all';
  _settingsUpdateLayout(fieldId, { tab: targetTab, visibility: vis });
  showToast(`Moved to ${targetTab}`, 'success');
}

/**
 * Show a right-click context menu for a settings field.
 * Available to superadmins regardless of edit mode.
 *
 * @param {MouseEvent} event
 * @param {string}     fieldId
 * @param {string}     currentTab - active tab id
 */
function _settingsWidgetContextMenu(event, fieldId, currentTab) {
  document.querySelectorAll('.settings-ctx-menu').forEach(el => el.remove());
  const schema = SETTINGS_SCHEMA.find(s => s.id === fieldId);
  if (!schema) return;

  const isAdminVis = _getEffectiveVisibility(schema) === 'superadmin';

  // Build "Move to tab" options (exclude the tab the field already lives on)
  const availableTabs = ['global', 'user'];
  const moveOpts = availableTabs
    .filter(t => t !== currentTab)
    .map(t => `<button data-move-to="${escapeHtml(t)}">&rarr; ${escapeHtml(t.charAt(0).toUpperCase() + t.slice(1))}</button>`)
    .join('');

  const menu = document.createElement('div');
  menu.className = 'settings-ctx-menu';
  menu.innerHTML = `
    <button data-action="toggleVis">
      ${isAdminVis ? '&#128275; Make visible to all users' : '&#128274; Make superadmin-only'}
    </button>
    <div style="border-top:1px solid rgba(148,163,184,0.15);margin:3px 0"></div>
    <div style="padding:3px 10px;font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em">Move to tab</div>
    ${moveOpts}
  `;

  const x = Math.min(event.clientX, window.innerWidth - 230);
  const y = Math.min(event.clientY, window.innerHeight - 160);
  Object.assign(menu.style, {
    position: 'fixed',
    left: `${x}px`,
    top: `${y}px`,
    zIndex: '9500',
    background: 'rgb(30 41 59)',       // slate-800
    border: '1px solid rgb(51 65 85)', // slate-700
    borderRadius: '8px',
    padding: '4px',
    boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
    minWidth: '200px',
  });

  menu.querySelectorAll('button').forEach(btn => {
    Object.assign(btn.style, {
      display: 'block',
      width: '100%',
      textAlign: 'left',
      background: 'none',
      border: 'none',
      color: 'rgb(226 232 240)', // slate-200
      padding: '7px 10px',
      fontSize: '12px',
      cursor: 'pointer',
      borderRadius: '4px',
    });
    btn.addEventListener('mouseover', () => { btn.style.background = 'rgba(148,163,184,0.1)'; });
    btn.addEventListener('mouseout',  () => { btn.style.background = 'none'; });

    if (btn.dataset.action === 'toggleVis') {
      btn.addEventListener('click', () => _settingsToggleVisibility(fieldId));
    } else if (btn.dataset.moveTo) {
      btn.addEventListener('click', () => _settingsMoveWidget(fieldId, btn.dataset.moveTo));
    }
  });

  document.body.appendChild(menu);
  const close = e => {
    if (!menu.contains(e.target)) {
      menu.remove();
      document.removeEventListener('click', close);
    }
  };
  setTimeout(() => document.addEventListener('click', close), 10);
}

/* ── Drag-and-drop reordering ────────────────────────────────────────────── */

/**
 * Attach HTML5 drag-and-drop listeners to all field rows and group headings
 * inside `container`.  Called at the end of renderSettingsWidgets when
 * edit mode is active.
 *
 * @param {HTMLElement} container
 */
function _initSettingsDrag(container) {
  container.querySelectorAll('.settings-drag-handle').forEach(handle => {
    handle.addEventListener('dragstart', e => {
      _settingsDragId = handle.dataset.dragId;
      e.dataTransfer.effectAllowed = 'move';
      const row = handle.closest('[data-field-id]');
      if (row) row.style.opacity = '0.4';
    });

    handle.addEventListener('dragend', () => {
      _settingsDragId = null;
      container.querySelectorAll('[data-field-id]').forEach(el => {
        el.style.opacity = '';
        el.style.outline = '';
      });
    });
  });

  container.querySelectorAll('[data-field-id]').forEach(row => {
    row.addEventListener('dragover', e => {
      if (!_settingsDragId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      row.style.outline = '2px solid rgb(59 130 246)'; // blue-500
    });
    row.addEventListener('dragleave', () => { row.style.outline = ''; });
    row.addEventListener('drop', e => {
      e.preventDefault();
      row.style.outline = '';
      if (!_settingsDragId) return;
      const targetId = row.dataset.fieldId;
      if (targetId && targetId !== _settingsDragId) {
        _settingsReorder(_settingsDragId, targetId);
      }
    });
  });

  // Dropping onto a group heading moves the field into that group at position 0
  container.querySelectorAll('[data-settings-group]').forEach(heading => {
    heading.addEventListener('dragover', e => {
      if (!_settingsDragId) return;
      e.preventDefault();
      heading.style.outline = '2px solid rgb(59 130 246)';
    });
    heading.addEventListener('dragleave', () => { heading.style.outline = ''; });
    heading.addEventListener('drop', e => {
      e.preventDefault();
      heading.style.outline = '';
      if (!_settingsDragId) return;
      const groupName = heading.dataset.settingsGroup || heading.textContent.trim();
      _settingsUpdateLayout(_settingsDragId, { group: groupName, order: 0 });
    });
  });
}

/**
 * Reorder the dragged field to appear before `targetId` within the current tab,
 * placing it into the same group as the target.
 */
function _settingsReorder(draggedId, targetId) {
  const tab = _settingsCurrentTab;
  const widgets = SETTINGS_SCHEMA.filter(s => _getEffectiveTab(s) === tab);
  widgets.sort((a, b) => _getEffectiveOrder(a) - _getEffectiveOrder(b));

  const ids = widgets.map(w => w.id);
  const fromIdx = ids.indexOf(draggedId);
  const toIdx   = ids.indexOf(targetId);
  if (fromIdx < 0 || toIdx < 0) return;

  ids.splice(fromIdx, 1);
  ids.splice(toIdx, 0, draggedId);

  const targetSchema = SETTINGS_SCHEMA.find(s => s.id === targetId);
  const targetGroup  = _getEffectiveGroup(targetSchema);

  // Write new order values back into _settingsLayoutOverrides
  ids.forEach((id, i) => {
    const existing = _settingsLayoutOverrides[id];
    const base = (existing && typeof existing === 'object')
      ? existing
      : (typeof existing === 'string' ? { visibility: existing } : {});
    _settingsLayoutOverrides[id] = { ...base, order: i };
  });
  // Also adopt the target group for the dragged field
  _settingsLayoutOverrides[draggedId] = { ..._settingsLayoutOverrides[draggedId], group: targetGroup };

  // Batch-persist the entire layout map
  fetch('/api/v1/settings/global', {
    method: 'PATCH',
    headers: { ...Auth.getHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ _layout: _settingsLayoutOverrides }),
  }).catch(() => showToast('Failed to save layout', 'error'));

  // Re-render immediately
  if (_settingsCurrentContainerId) {
    renderSettingsWidgets(
      _settingsCurrentContainerId,
      _settingsCurrentTab,
      _settingsCurrentIsSuperadmin,
      _settingsCurrentValues,
      _settingsCurrentOnSave,
    );
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
