/*
 * SPDX-FileCopyrightText: 2026 yvesll
 * SPDX-License-Identifier: Apache-2.0
 */

const defaults = JSON.parse(document.getElementById("app-defaults").textContent);
const CHECK_FILTER_OPTIONS = ["all", "passed", "other"];
const REVIEW_FILTER_OPTIONS = ["all", "changes_requested", "approved", "commented", "none"];
const REASON_FILTER_OPTIONS = ["all", "assign", "author", "comment", "invitation", "manual", "mention", "review_requested", "security_alert", "state_change", "subscribed", "team_mention"];
const COMBOBOX_SEARCH_THRESHOLD = 8;
const STATUS_FILTER_OPTIONS = ["all", "open", "closed", "merged"];
const MULTI_COMBOBOX_KEYS = new Set(["label_include", "label_exclude"]);

const state = {
  filters: { ...defaults },
  items: [],
  counts: { inbox: 0, done: 0, all: 0 },
  facets: { repositories: [], reasons: [], statuses: [], checksStates: [], reviewStates: [], labels: [] },
  filterSearch: {
    repository: "",
    label_include: "",
    label_exclude: "",
    reason: "",
  },
  openCombobox: null,
  meta: {},
  summaries: new Map(),
  expandedSummaries: new Set(),
  refreshTimer: null,
  keywordTimer: null,
};

const refs = {
  list: document.getElementById("notification-list"),
  keywordSearch: document.getElementById("keyword-search"),
  resultsSummary: document.getElementById("results-summary"),
  refreshButton: document.getElementById("refresh-button"),
  resetFilters: document.getElementById("reset-filters"),
  statusFilterChips: document.getElementById("status-filter-chips"),
  checksFilterChips: document.getElementById("checks-filter-chips"),
  reasonCombobox: document.getElementById("reason-combobox"),
  reviewFilterChips: document.getElementById("review-filter-chips"),
  repositoryCombobox: document.getElementById("repository-combobox"),
  labelIncludeFilterField: document.getElementById("label-include-filter-field"),
  labelIncludeCombobox: document.getElementById("label-include-combobox"),
  labelExcludeFilterField: document.getElementById("label-exclude-filter-field"),
  labelExcludeCombobox: document.getElementById("label-exclude-combobox"),
  countInbox: document.getElementById("count-inbox"),
  countDone: document.getElementById("count-done"),
  countAll: document.getElementById("count-all"),
  metaLastSync: document.getElementById("meta-last-sync"),
  metaLastSource: document.getElementById("meta-last-source"),
  metaPolling: document.getElementById("meta-polling"),
  metaAI: document.getElementById("meta-ai"),
  viewSwitch: document.getElementById("view-switch"),
};

function init() {
  state.filters.label_include = normalizeMultiValue(state.filters.label_include);
  state.filters.label_exclude = normalizeMultiValue(state.filters.label_exclude);
  state.filters.keyword ||= "";
  bindEvents();
  loadNotifications({ showLoading: true });
}

function bindEvents() {
  refs.refreshButton.addEventListener("click", handleManualRefresh);
  refs.resetFilters.addEventListener("click", resetAllFilters);
  refs.keywordSearch.addEventListener("input", () => {
    if (state.keywordTimer) {
      window.clearTimeout(state.keywordTimer);
    }
    state.keywordTimer = window.setTimeout(() => {
      state.filters.keyword = refs.keywordSearch.value.trim();
      loadNotifications();
    }, 180);
  });
  refs.viewSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view]");
    if (!button) return;
    state.filters.view = button.dataset.view;
    syncViewButtons();
    loadNotifications();
  });
  document.addEventListener("click", handleDocumentClick);
  document.addEventListener("input", handleDocumentInput);
  document.addEventListener("keydown", handleDocumentKeydown);
  refs.statusFilterChips.addEventListener("click", handleChipFilterClick);
  refs.checksFilterChips.addEventListener("click", handleChipFilterClick);
  refs.reviewFilterChips.addEventListener("click", handleChipFilterClick);
  refs.list.addEventListener("click", async (event) => {
    const actionTarget = event.target.closest("[data-action]");
    if (actionTarget) {
      const { action, threadId } = actionTarget.dataset;
      if (!threadId) return;

      if (action === "toggle-done") {
        event.preventDefault();
        await toggleDone(threadId, actionTarget.dataset.done === "true");
        return;
      }

      if (action === "summary") {
        event.preventDefault();
        await loadSummary(threadId);
        return;
      }
    }
  });
}

function handleChipFilterClick(event) {
  const button = event.target.closest("[data-chip-filter]");
  if (!button) return;
  const filter = button.dataset.chipFilter;
  const value = button.dataset.value || "all";
  state.filters[filter] = value;
  renderFilters();
  loadNotifications();
}

function handleDocumentClick(event) {
  const toggle = event.target.closest("[data-combobox-toggle]");
  if (toggle) {
    const key = toggle.dataset.comboboxToggle;
    state.openCombobox = state.openCombobox === key ? null : key;
    renderFilters();
    if (state.openCombobox === key) {
      focusComboboxSearch(key);
    }
    return;
  }

  const option = event.target.closest("[data-combobox-option]");
  if (option) {
    const key = option.dataset.comboboxOption;
    const value = option.dataset.value || "all";
    if (MULTI_COMBOBOX_KEYS.has(key)) {
      state.filters[key] = toggleMultiSelection(state.filters[key], value);
      state.openCombobox = key;
      renderFilters();
      focusComboboxSearch(key);
      loadNotifications();
      return;
    }

    state.filters[key] = value;
    state.openCombobox = null;

    if (key === "repository") {
      state.filters.label_include = [];
      state.filters.label_exclude = [];
      state.filterSearch.label_include = "";
      state.filterSearch.label_exclude = "";
    }

    loadNotifications();
    return;
  }

  const bulkAction = event.target.closest("[data-combobox-bulk]");
  if (bulkAction) {
    const key = bulkAction.dataset.comboboxBulk;
    const action = bulkAction.dataset.action;
    if (action === "clear") {
      state.filters[key] = [];
    } else if (action === "select-matching") {
      const matches = getFilteredComboboxOptions(key).map((option) => option.value);
      state.filters[key] = Array.from(new Set([...normalizeMultiValue(state.filters[key]), ...matches]));
    }
    state.openCombobox = key;
    renderFilters();
    focusComboboxSearch(key);
    loadNotifications();
    return;
  }

  if (state.openCombobox && !event.target.closest(".combobox")) {
    state.openCombobox = null;
    renderFilters();
  }
}

function handleDocumentInput(event) {
  const input = event.target.closest("[data-combobox-search]");
  if (!input) return;
  const key = input.dataset.comboboxSearch;
  const cursorPosition = input.selectionStart ?? input.value.length;
  state.filterSearch[key] = input.value;
  renderFilters();
  focusComboboxSearch(key, cursorPosition);
}

function handleDocumentKeydown(event) {
  if (event.key === "Escape" && state.openCombobox) {
    state.openCombobox = null;
    renderFilters();
  }
}

async function loadNotifications({ showLoading = false, silent = false } = {}) {
  if (showLoading) {
    refs.list.innerHTML = renderMessageCard("Loading queue", "Pulling the latest notifications<span class='loading-dot'></span>");
  }

  if (!silent) {
    setBanner("Loading notifications from the local cache and GitHub sync state.");
  }

  try {
    const response = await fetch(`/api/notifications?${buildQueryParams(state.filters)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to load notifications");

    state.items = payload.items;
    state.counts = payload.counts;
    state.facets = {
      repositories: payload.filters.repositories || [],
      labels: payload.filters.labels || [],
      reasons: payload.filters.reasons || [],
      statuses: payload.filters.statuses || [],
      checksStates: payload.filters.checks_states || [],
      reviewStates: payload.filters.review_states || [],
    };
    state.meta = payload.meta || {};

    renderFilters();
    renderMeta();
    renderResultsSummary(payload.items.length);
    renderList();
    scheduleRefresh();

    if (!silent) {
      setBanner("");
    }
  } catch (error) {
    refs.list.innerHTML = renderMessageCard("Could not load notifications", escapeHtml(error.message));
    renderResultsSummary(0);
    setBanner(error.message);
  }
}

async function handleManualRefresh() {
  refs.refreshButton.disabled = true;
  setBanner("Refreshing from GitHub Notifications API. This can take a moment for larger queues.");

  try {
    const response = await fetch("/api/notifications/refresh", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Refresh failed");
    await loadNotifications({ silent: true });
    setBanner(payload.warning || `Synced ${payload.synced} notifications via ${payload.source}.`);
  } catch (error) {
    setBanner(error.message);
  } finally {
    refs.refreshButton.disabled = false;
  }
}

async function toggleDone(threadId, isDone) {
  try {
    const response = await fetch(`/api/notifications/${threadId}/done`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_done: isDone }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to update item");
    await loadNotifications({ silent: true });
    setBanner(isDone ? "Marked thread done on GitHub and moved it out of Inbox." : "Updated thread state.");
  } catch (error) {
    setBanner(error.message);
  }
}

async function loadSummary(threadId) {
  const existing = state.summaries.get(threadId);
  if (existing && !existing.loading && !existing.error) {
    if (state.expandedSummaries.has(threadId)) {
      state.expandedSummaries.delete(threadId);
    } else {
      state.expandedSummaries.add(threadId);
    }
    renderList();
    return;
  }

  state.summaries.set(threadId, { loading: true });
  state.expandedSummaries.add(threadId);
  renderList();

  try {
    const response = await fetch(`/api/notifications/${threadId}/summary`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to generate AI summary");
    state.summaries.set(threadId, payload);
    renderList();
    setBanner(
      payload.source === "fallback"
        ? "Generated a local summary."
        : payload.cache_hit
          ? "Loaded cached AI summary."
          : "Generated a fresh AI summary."
    );
  } catch (error) {
    state.summaries.set(threadId, { error: error.message });
    renderList();
    setBanner(error.message);
  }
}

function renderFilters() {
  syncViewButtons();
  refs.keywordSearch.value = state.filters.keyword || "";
  renderChipFilter(refs.statusFilterChips, "status", mergeFilterOptions(STATUS_FILTER_OPTIONS, state.facets.statuses), state.filters.status, prettifyToken);
  renderRepositoryCombobox();
  renderLabelFilter();
  renderReasonCombobox();
  renderChipFilter(refs.checksFilterChips, "checks", mergeFilterOptions(CHECK_FILTER_OPTIONS, state.facets.checksStates), state.filters.checks, prettifyToken);
  renderChipFilter(refs.reviewFilterChips, "review", mergeFilterOptions(REVIEW_FILTER_OPTIONS, state.facets.reviewStates), state.filters.review, prettifyToken);

  refs.countInbox.textContent = state.counts.inbox ?? 0;
  refs.countDone.textContent = state.counts.done ?? 0;
  refs.countAll.textContent = state.counts.all ?? 0;
}

function renderResultsSummary(count) {
  const keywordSuffix = state.filters.keyword ? ` matching "${state.filters.keyword}"` : "";
  refs.resultsSummary.textContent = `Showing ${count} notifications in the ${state.filters.view} view${keywordSuffix}.`;
}

function buildQueryParams(filters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) {
      params.set(key, value.join(","));
    } else {
      params.set(key, value ?? "");
    }
  }
  return params.toString();
}

function renderSelect(element, values, activeValue, labelFn) {
  const uniqueValues = [...new Set(values.filter(Boolean))];
  element.innerHTML = uniqueValues
    .map((value) => {
      const selected = value === activeValue ? " selected" : "";
      return `<option value="${escapeAttr(value)}"${selected}>${escapeHtml(labelFn(value))}</option>`;
    })
    .join("");
}

function renderChipFilter(container, filterKey, values, activeValue, labelFn) {
  const uniqueValues = [...new Set(values.filter(Boolean))];
  container.innerHTML = uniqueValues
    .map((value) => {
      const activeClass = value === activeValue ? " is-active" : "";
      return `
        <button
          class="chip-filter__button${activeClass}"
          type="button"
          data-chip-filter="${escapeAttr(filterKey)}"
          data-value="${escapeAttr(value)}"
        >
          ${escapeHtml(labelFn(value))}
        </button>
      `;
    })
    .join("");
}

function mergeFilterOptions(baseValues, dynamicValues) {
  return [...new Set([...(baseValues || []), ...(dynamicValues || [])])];
}

function renderCombobox({ key, container, options, selectedValue, searchValue, renderOption, renderSelected, multiple = false }) {
  const uniqueOptions = dedupeOptions(options);
  const selectedValues = multiple ? normalizeMultiValue(selectedValue) : [selectedValue];
  const selectedOption = multiple
    ? uniqueOptions.filter((option) => selectedValues.includes(option.value))
    : uniqueOptions.find((option) => option.value === selectedValue) || uniqueOptions[0];
  const searchable = uniqueOptions.length > COMBOBOX_SEARCH_THRESHOLD || multiple;
  const filteredOptions = searchable ? getFilteredComboboxOptions(key, uniqueOptions) : uniqueOptions;
  const selectedValuesSet = new Set(selectedValues);
  const showBulkSelect = multiple && searchValue.trim() && filteredOptions.length > 0;
  const showBulkClear = multiple && selectedValues.length > 0;

  let menuOptions = filteredOptions;
  if (!multiple && selectedOption && !filteredOptions.some((option) => option.value === selectedOption.value)) {
    menuOptions = [selectedOption, ...filteredOptions];
  }

  const optionRenderer = renderOption || defaultComboboxOptionRenderer;
  const selectedRenderer = renderSelected || defaultComboboxSelectedRenderer;

  container.innerHTML = `
    <div class="combobox ${state.openCombobox === key ? "is-open" : ""}">
      <button
        class="combobox__trigger"
        type="button"
        data-combobox-toggle="${escapeAttr(key)}"
        aria-expanded="${state.openCombobox === key ? "true" : "false"}"
      >
        <span class="combobox__trigger-value">${selectedRenderer(selectedOption)}</span>
        <span class="combobox__chevron" aria-hidden="true">▾</span>
      </button>
      ${
        state.openCombobox === key
          ? `
            <div class="combobox__menu">
              ${
                searchable
                  ? `<input
                      class="combobox__search"
                      type="search"
                      data-combobox-search="${escapeAttr(key)}"
                      value="${escapeAttr(searchValue)}"
                      placeholder="Search"
                    >`
                  : ""
              }
              ${
                showBulkSelect || showBulkClear
                  ? `
                    <div class="combobox__actions">
                      ${showBulkSelect ? `<button class="combobox__action" type="button" data-combobox-bulk="${escapeAttr(key)}" data-action="select-matching">Select all matching</button>` : ""}
                      ${showBulkClear ? `<button class="combobox__action" type="button" data-combobox-bulk="${escapeAttr(key)}" data-action="clear">Clear</button>` : ""}
                    </div>
                  `
                  : ""
              }
              <div class="combobox__options">
                ${
                  menuOptions.length
                    ? menuOptions
                        .map(
                          (option) => `
                            <button
                              class="combobox__option ${
                                multiple
                                  ? selectedValuesSet.has(option.value) ? "is-selected" : ""
                                  : option.value === selectedValue ? "is-selected" : ""
                              }"
                              type="button"
                              data-combobox-option="${escapeAttr(key)}"
                              data-value="${escapeAttr(option.value)}"
                            >
                              ${optionRenderer(option, multiple ? selectedValuesSet.has(option.value) : option.value === selectedValue)}
                            </button>
                          `
                        )
                        .join("")
                    : `<div class="combobox__empty">No matches</div>`
                }
              </div>
            </div>
          `
          : ""
      }
    </div>
  `;
}

function dedupeOptions(options) {
  const seen = new Set();
  const deduped = [];
  for (const option of options) {
    if (!option || !option.value) continue;
    if (seen.has(option.value)) continue;
    seen.add(option.value);
    deduped.push(option);
  }
  return deduped;
}

function defaultComboboxOptionRenderer(option) {
  return `<span class="combobox__option-text">${escapeHtml(option.label)}</span>`;
}

function defaultComboboxSelectedRenderer(option) {
  return `<span class="combobox__selected-text">${escapeHtml(option?.label || "Select")}</span>`;
}

function renderLabelOption(option, selected = false) {
  if (option.value === "all") {
    return defaultComboboxOptionRenderer(option);
  }
  const checkmark = selected ? `<span class="combobox__check">✓</span>` : "";
  return `<span class="combobox__option-row">${checkmark}${renderLabelChip(option.label, option.color, "label-chip--compact")}</span>`;
}

function renderLabelSelected(option) {
  if (!option || option.value === "all") {
    return `<span class="combobox__selected-text">${escapeHtml(option?.label || "All labels")}</span>`;
  }
  return renderLabelChip(option.label, option.color, "label-chip--compact");
}

function renderRepositoryCombobox() {
  renderCombobox({
    key: "repository",
    container: refs.repositoryCombobox,
    options: ["all", ...state.facets.repositories].map((value) => ({
      value,
      label: value === "all" ? "All" : value,
    })),
    selectedValue: state.filters.repository,
    searchValue: state.filterSearch.repository,
  });
}

function renderReasonCombobox() {
  renderCombobox({
    key: "reason",
    container: refs.reasonCombobox,
    options: mergeFilterOptions(REASON_FILTER_OPTIONS, state.facets.reasons).map((value) => ({
      value,
      label: prettifyToken(value),
    })),
    selectedValue: state.filters.reason,
    searchValue: state.filterSearch.reason,
  });
}

function renderLabelFilter() {
  const repoSelected = state.filters.repository && state.filters.repository !== "all";
  refs.labelIncludeFilterField.classList.toggle("is-hidden", !repoSelected);
  refs.labelExcludeFilterField.classList.toggle("is-hidden", !repoSelected);
  if (!repoSelected) {
    refs.labelIncludeCombobox.innerHTML = "";
    refs.labelExcludeCombobox.innerHTML = "";
    return;
  }
  const labelOptions = [
    { value: "all", label: "All labels", color: "" },
    ...state.facets.labels.map((label) => ({
      value: label.name,
      label: label.name,
      color: label.color || "",
    })),
  ];
  renderCombobox({
    key: "label_include",
    container: refs.labelIncludeCombobox,
    options: labelOptions,
    selectedValue: state.filters.label_include,
    searchValue: state.filterSearch.label_include,
    renderOption: renderLabelOption,
    renderSelected: (option) => renderLabelSelectedText(option, "All labels"),
    multiple: true,
  });
  renderCombobox({
    key: "label_exclude",
    container: refs.labelExcludeCombobox,
    options: labelOptions,
    selectedValue: state.filters.label_exclude,
    searchValue: state.filterSearch.label_exclude,
    renderOption: renderLabelOption,
    renderSelected: (option) => renderLabelSelectedText(option, "No excluded label"),
    multiple: true,
  });
}

function renderLabelSelectedText(option, emptyLabel) {
  if (!option || (Array.isArray(option) && option.length === 0) || option.value === "all") {
    return `<span class="combobox__selected-text">${escapeHtml(emptyLabel)}</span>`;
  }
  if (Array.isArray(option)) {
    const chips = option.slice(0, 2).map((item) => renderLabelChip(item.label, item.color, "label-chip--compact")).join("");
    const rest = option.length > 2 ? `<span class="combobox__selected-more">+${option.length - 2}</span>` : "";
    return `<span class="combobox__selected-multi">${chips}${rest}</span>`;
  }
  return renderLabelChip(option.label, option.color, "label-chip--compact");
}

function focusComboboxSearch(key, cursorPosition = null) {
  window.requestAnimationFrame(() => {
    const input = document.querySelector(`[data-combobox-search="${key}"]`);
    if (!(input instanceof HTMLInputElement)) return;
    input.focus();
    const position = cursorPosition ?? input.value.length;
    input.setSelectionRange(position, position);
  });
}

function syncViewButtons() {
  refs.viewSwitch.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.filters.view);
  });
}

function renderMeta() {
  refs.metaLastSync.textContent = formatDateTime(state.meta.last_sync_at) || "Never";
  refs.metaLastSource.textContent = state.meta.last_sync_source || "n/a";
  refs.metaPolling.textContent = state.meta.polling_enabled
    ? `Every ${state.meta.polling_interval_seconds || "?"}s`
    : "Off";
  refs.metaAI.textContent = state.meta.ai_enabled ? "Configured with fallback" : "Missing API key";
}

function renderList() {
  if (!state.meta.github_configured) {
    refs.list.innerHTML = renderMessageCard(
      "GitHub token required",
      "Add `github.token` to `config.toml` or provide `GITHUB_TOKEN`, then hit refresh."
    );
    return;
  }

  if (!state.items.length) {
    refs.list.innerHTML = renderMessageCard(
      "Nothing queued right now",
      "Try another filter, or refresh if you expect new review work."
    );
    return;
  }

  refs.list.innerHTML = state.items.map(renderNotificationCard).join("");
}

function renderNotificationCard(item) {
  const summary = state.summaries.get(item.thread_id);
  const summaryExpanded = state.expandedSummaries.has(item.thread_id);
  const typeBadgeClass = item.type === "Issue" ? "badge badge--type badge--issue" : "badge badge--type";
  const statusBadgeClass = item.status === "merged" ? "badge badge--merged" : "badge";
  const checksBadge = item.type === "PullRequest" && item.checks_state !== "none"
    ? `<span class="mini-pill">Checks ${escapeHtml(prettifyToken(item.checks_state))}</span>`
    : "";
  const reviewBadge = item.type === "PullRequest" && item.review_decision !== "none"
    ? `<span class="mini-pill">Review ${escapeHtml(prettifyToken(item.review_decision))}</span>`
    : "";
  const labelBadges = (item.labels || [])
    .slice(0, 4)
    .map((label) => renderLabelChip(label.name, label.color, "label-chip--compact"))
    .join("");
  const summaryButtonLabel = (() => {
    if (!state.meta.ai_enabled) return "AI unavailable";
    if (summary?.loading) return "Loading summary";
    if (summaryExpanded && summary) return "Hide summary";
    if (summary && !summary.error) return "Show summary";
    return "AI summary";
  })();
  const doneButton = item.is_done
    ? `<button class="row-button row-button--strong" type="button" disabled title="GitHub Notifications API cannot restore a done thread to Inbox">Done on GitHub</button>`
    : `
      <button
        class="row-button row-button--strong"
        type="button"
        data-action="toggle-done"
        data-thread-id="${item.thread_id}"
        data-done="true"
      >
        Mark done
      </button>
    `;

  return `
    <article class="notification-card ${item.unread ? "notification-card--unread" : ""}" data-type="${escapeAttr(item.type)}" data-status="${escapeAttr(item.status)}">
      <div class="notification-card__row">
        <div class="notification-card__main">
          <div class="notification-card__header">
            <span class="${typeBadgeClass}">${escapeHtml(item.type === "PullRequest" ? "PR" : "Issue")}</span>
            <span class="${statusBadgeClass}">${escapeHtml(prettifyToken(item.status))}</span>
            ${checksBadge}
            ${reviewBadge}
            ${labelBadges}
            ${item.is_done ? `<span class="mini-pill">Done</span>` : `<span class="mini-pill">Inbox</span>`}
            ${item.unread ? `<span class="mini-pill">Unread</span>` : ""}
          </div>
          <h3 class="notification-card__title">
            ${item.web_url ? `<a class="notification-card__link" href="${escapeAttr(item.web_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>` : escapeHtml(item.title)}
          </h3>
          <div class="notification-card__meta">
            <span>${escapeHtml(item.repository)}</span>
            <span>${escapeHtml(prettifyToken(item.reason))}</span>
            <span>${escapeHtml(formatRelative(item.updated_at))}</span>
          </div>
          ${summaryExpanded ? renderSummary(summary) : ""}
        </div>
        <div class="notification-card__actions">
          <button
            class="row-button row-button--strong"
            type="button"
            data-action="summary"
            data-thread-id="${item.thread_id}"
            ${state.meta.ai_enabled ? "" : "disabled"}
          >
            ${summaryButtonLabel}
          </button>
          ${doneButton}
        </div>
      </div>
    </article>
  `;
}

function renderSummary(summary) {
  if (!summary) return "";
  if (summary.loading) {
    return `
      <section class="summary-panel">
        <div class="summary-panel__meta">AI summary is being generated<span class="loading-dot"></span></div>
      </section>
    `;
  }
  if (summary.error) {
    return `
      <section class="summary-panel">
        <div class="summary-panel__meta">AI summary failed</div>
        <p>${escapeHtml(summary.error)}</p>
      </section>
    `;
  }

  return `
    <section class="summary-panel">
      <div class="summary-panel__meta">
        <span>${summary.source === "fallback" ? "Local summary" : summary.cache_hit ? "Cached summary" : "Fresh summary"}</span>
        <span>${escapeHtml(formatDateTime(summary.generated_at))}</span>
      </div>
      ${renderSummaryBody(summary)}
      ${
        summary.action_items?.length
          ? `
        <h4 class="summary-panel__title">Action items</h4>
        <ul class="summary-list">
          ${summary.action_items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
        </ul>
      `
          : ""
      }
    </section>
  `;
}

function renderSummaryBody(summary) {
  if (summary.sections) {
    const sections = [
      ["What changed", summary.sections.what_changed],
      ["Current discussion", summary.sections.current_discussion],
      ["Latest status", summary.sections.latest_status],
    ].filter(([, value]) => value);

    return sections
      .map(
        ([title, value]) => `
          <h4 class="summary-panel__title">${escapeHtml(title)}</h4>
          <p>${escapeHtml(value)}</p>
        `
      )
      .join("");
  }

  return `
    <h4 class="summary-panel__title">Summary</h4>
    <p>${escapeHtml(summary.summary)}</p>
  `;
}

function renderMessageCard(title, body) {
  return `
    <section class="empty-state">
      <h3>${title}</h3>
      <p>${body}</p>
    </section>
  `;
}

function scheduleRefresh() {
  if (state.refreshTimer) {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  const seconds = Number(state.meta.polling_interval_seconds || 0);
  const intervalMs = seconds ? Math.min(seconds * 1000, 60000) : 60000;
  state.refreshTimer = window.setInterval(() => {
    loadNotifications({ silent: true });
  }, intervalMs);
}

function prettifyToken(value) {
  if (value === "all") return "All";
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatRelative(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    ...relativeParts(date)
  );
}

function relativeParts(date) {
  const diffMs = date.getTime() - Date.now();
  const absMinutes = Math.round(diffMs / 60000);
  if (Math.abs(absMinutes) < 60) return [absMinutes, "minute"];
  const absHours = Math.round(diffMs / 3600000);
  if (Math.abs(absHours) < 24) return [absHours, "hour"];
  const absDays = Math.round(diffMs / 86400000);
  return [absDays, "day"];
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function renderLabelChip(name, color, extraClass = "") {
  const hex = normalizeLabelColor(color);
  const textColor = pickLabelTextColor(hex);
  const background = withAlpha(hex, 0.18);
  const border = withAlpha(hex, 0.36);
  const className = ["label-chip", extraClass].filter(Boolean).join(" ");
  return `
    <span
      class="${className}"
      style="--label-bg:${background};--label-border:${border};--label-text:${textColor};"
      title="${escapeAttr(name)}"
    >
      ${escapeHtml(name)}
    </span>
  `;
}

function normalizeLabelColor(color) {
  const normalized = String(color || "").replace("#", "").trim();
  if (/^[0-9a-fA-F]{6}$/.test(normalized)) {
    return `#${normalized}`;
  }
  return "#6e7781";
}

function withAlpha(hex, alpha) {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function hexToRgb(hex) {
  const normalized = normalizeLabelColor(hex).slice(1);
  return {
    r: Number.parseInt(normalized.slice(0, 2), 16),
    g: Number.parseInt(normalized.slice(2, 4), 16),
    b: Number.parseInt(normalized.slice(4, 6), 16),
  };
}

function pickLabelTextColor(hex) {
  const { r, g, b } = hexToRgb(hex);
  const yiq = (r * 299 + g * 587 + b * 114) / 1000;
  return yiq >= 148 ? "#111827" : "#f8fafc";
}

function setBanner(message) {
  return;
}

function resetAllFilters() {
  state.filters = {
    ...defaults,
    label_include: [],
    label_exclude: [],
    keyword: "",
  };
  state.filterSearch.repository = "";
  state.filterSearch.label_include = "";
  state.filterSearch.label_exclude = "";
  state.filterSearch.reason = "";
  state.openCombobox = null;
  renderFilters();
  loadNotifications();
}

function normalizeMultiValue(value) {
  if (Array.isArray(value)) {
    return value.filter(Boolean);
  }
  if (typeof value === "string") {
    if (!value.trim() || value === "all") return [];
    return value.split(",").map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

function toggleMultiSelection(currentValue, nextValue) {
  if (nextValue === "all") return [];
  const values = new Set(normalizeMultiValue(currentValue));
  if (values.has(nextValue)) {
    values.delete(nextValue);
  } else {
    values.add(nextValue);
  }
  return Array.from(values);
}

function getFilteredComboboxOptions(key, optionsOverride = null) {
  const options = optionsOverride || getComboboxOptions(key);
  const searchValue = state.filterSearch[key] || "";
  const loweredSearch = searchValue.trim().toLowerCase();
  const filtered = loweredSearch
    ? options.filter((option) => option.label.toLowerCase().includes(loweredSearch))
    : options;
  return filtered.filter((option) => option.value !== "all");
}

function getComboboxOptions(key) {
  if (key === "repository") {
    return ["all", ...state.facets.repositories].map((value) => ({
      value,
      label: value === "all" ? "All" : value,
    }));
  }
  if (key === "reason") {
    return mergeFilterOptions(REASON_FILTER_OPTIONS, state.facets.reasons).map((value) => ({
      value,
      label: prettifyToken(value),
    }));
  }
  if (key === "label_include" || key === "label_exclude") {
    return [
      { value: "all", label: "All labels", color: "" },
      ...state.facets.labels.map((label) => ({
        value: label.name,
        label: label.name,
        color: label.color || "",
      })),
    ];
  }
  return [];
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

init();
