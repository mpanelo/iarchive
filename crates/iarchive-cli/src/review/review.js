'use strict';
const token = location.hash.slice(1);
// Adding the session fragment to an already-open page must start a fresh session.
window.addEventListener('hashchange', () => location.reload());
const message = document.getElementById('message');
let audit, category = 'all', page = 0, busy = false;
let previews = new AbortController();
let blobs = [];
const selections = new Map();
const feedback = new Map();
const perPage = 12;
const labels = { all: 'All candidates', exact: 'Exact duplicates', similar: 'Similar images', dark: 'Dark candidates', findings: 'Failures & unprocessed', reviewed: 'Reviewed' };
const decisions = { keep_all: 'Group kept', flag_selected: 'Flags saved', defer: 'Group deferred' };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(text, error = false) {
  message.textContent = text;
  message.classList.toggle('error', error);
}
async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { Authorization: `Bearer ${token}`, ...options.headers }, cache: 'no-store' });
  if (response.status === 413) throw new Error('That selection is too large to save at once. Select fewer files.');
  if (!response.ok) throw new Error(response.status === 409 ? 'The decision could not be saved. Reload the gallery and check your selection.' : `Request failed (${response.status}). Check that this review session is still running.`);
  return response;
}
async function load(renderGallery = true) {
  audit = await (await request('/api/review')).json();
  const current = new Set(audit.groups.map(group => group.fingerprint));
  for (const cache of [selections, feedback]) {
    for (const fingerprint of cache.keys()) if (!current.has(fingerprint)) cache.delete(fingerprint);
  }
  document.getElementById('cycle').textContent = audit.status.cycle;
  document.getElementById('audit-status').textContent = `${audit.status.file_count} recorded files · ${audit.groups.length} candidate groups · ${audit.status.stage.replaceAll('_', ' ').toLowerCase()}`;
  document.getElementById('history').textContent = `${audit.decision_history_count} saved decision ${audit.decision_history_count === 1 ? 'entry' : 'entries'}. Earlier scans remain history.`;
  document.getElementById('workspace').hidden = false;
  if (renderGallery) render();
}
function visibleGroups() {
  if (category === 'findings') return audit.findings.map(file => ({ kind: 'finding', explanation: 'An explicit finding; never a removal candidate.', members: [file] })).concat(audit.scan_issues.map(issue => ({ kind: 'scan_issue', explanation: `${issue.path}: ${issue.error}`, members: [] })));
  return audit.groups.filter(group => category === 'all' || (category === 'reviewed' ? !!group.decision : group.kind === category));
}
function render() {
  previews.abort();
  previews = new AbortController();
  for (const blob of blobs) URL.revokeObjectURL(blob);
  blobs = [];
  const filters = document.getElementById('filters');
  filters.replaceChildren();
  for (const [key, label] of Object.entries(labels)) {
    const count = key === 'all' ? audit.groups.length : key === 'findings' ? audit.findings.length + audit.scan_issues.length : audit.groups.filter(g => key === 'reviewed' ? !!g.decision : g.kind === key).length;
    const button = element('button', '', label);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(category === key));
    button.append(element('span', 'count', String(count)));
    button.addEventListener('click', () => { if (busy) return; category = key; page = 0; render(); });
    filters.append(button);
  }
  const groups = visibleGroups();
  const pages = Math.max(1, Math.ceil(groups.length / perPage));
  page = Math.min(page, pages - 1);
  document.getElementById('list-title').textContent = labels[category];
  const unit = category === 'findings' ? 'finding' : 'group';
  document.getElementById('list-count').textContent = `${groups.length} ${unit}${groups.length === 1 ? '' : 's'}`;
  const list = document.getElementById('groups');
  list.replaceChildren();
  if (!groups.length) list.append(element('p', 'empty', category === 'reviewed' ? 'Your saved decisions will appear here.' : 'No saved results in this category. Run an audit to generate candidates and previews.'));
  for (const group of groups.slice(page * perPage, (page + 1) * perPage)) list.append(groupCard(group));
  document.getElementById('page').textContent = `Page ${page + 1} of ${pages}`;
  document.getElementById('previous').disabled = page === 0 || busy;
  document.getElementById('next').disabled = page === pages - 1 || busy;
}
function groupCard(group) {
  const card = element('article', 'group');
  if (group.fingerprint) card.dataset.fingerprint = group.fingerprint;
  const heading = element('div', 'group-heading');
  const description = element('div');
  description.append(element('h3', '', labels[group.kind] || 'Saved finding'), element('p', '', group.explanation));
  heading.append(description);
  if (group.decision) heading.append(element('span', 'decision', decisions[group.decision.action]));
  card.append(heading);
  const members = element('div', 'members');
  if (group.fingerprint && !selections.has(group.fingerprint)) selections.set(group.fingerprint, new Set(group.decision?.selected || []));
  const selected = selections.get(group.fingerprint);
  const status = element('p', 'group-status');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.tabIndex = -1;
  let flagButton;
  function updateSelection() {
    const saved = feedback.get(group.fingerprint);
    status.classList.toggle('error', !!saved?.error);
    const count = selected.size;
    const unchanged = group.decision?.action === 'flag_selected' && count === group.decision.selected.length && group.decision.selected.every(id => selected.has(id));
    if (saved) status.textContent = saved.text;
    else if (unchanged) status.textContent = `${count} image${count === 1 ? '' : 's'} flagged in this group. Flags are saved.`;
    else if (count) status.textContent = `${count} image${count === 1 ? '' : 's'} selected — not saved. Click “Save selected flags”.`;
    else if (group.decision?.action === 'flag_selected') status.textContent = 'No images selected. Use “Clear this group’s decision” to remove its saved flags.';
    else status.textContent = 'Select images above, then click “Save selected flags”.';
    if (flagButton) flagButton.disabled = busy || count === 0 || unchanged;
  }
  for (const file of group.members) members.append(fileCard(file, selected, () => {
    feedback.delete(group.fingerprint);
    updateSelection();
  }));
  if (group.members.length) card.append(members);
  if (group.fingerprint) {
    const actions = element('div', 'actions');
    for (const [action, label] of [['keep_all', 'Keep this group'], ['flag_selected', 'Save selected flags'], ['defer', 'Decide on this group later'], ['reset', 'Clear this group’s decision']]) {
      const button = element('button', '', label);
      button.type = 'button';
      button.addEventListener('click', () => save(group, action, selected, card));
      if (action === 'flag_selected') flagButton = button;
      actions.append(button);
    }
    actions.append(element('span', 'hint', 'Applies only to this group'));
    card.append(actions);
    card.append(status);
    updateSelection();
  }
  return card;
}
function fileCard(file, selected, selectionChanged) {
  const card = element('div', 'file');
  const preview = element('div', 'preview', file.decode_error ? 'Image could not be decoded' : 'No saved preview');
  card.append(preview);
  if (file.preview_available) showPreview(file, preview, previews.signal);
  const info = element('div', 'file-info');
  info.append(element('p', 'file-path', file.relative_path));
  info.append(element('p', '', `${file.cycle} · ${file.kind} · ${new Intl.NumberFormat().format(file.byte_count)} bytes`));
  if (file.features) {
    const f = file.features;
    info.append(element('p', '', `${f.width} × ${f.height} · brightness median ${f.p50.toFixed(1)} · dark score ${f.dark_candidate_score.toFixed(2)}`));
    info.append(element('p', '', f.dark_reason));
  }
  for (const problem of [file.file_error, file.decode_error, ...file.warnings].filter(Boolean)) info.append(element('p', 'problem', problem));
  if (!file.features && !file.file_error && !file.decode_error) info.append(element('p', 'problem', 'Not visually analyzed in this saved audit.'));
  if (selected) {
    const label = element('label');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.checked = selected.has(file.id);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selected.add(file.id);
      else selected.delete(file.id);
      selectionChanged();
    });
    label.append(checkbox, document.createTextNode('Flag this image for later'));
    info.append(label);
  }
  card.append(info);
  return card;
}
async function showPreview(file, container, signal) {
  try {
    const response = await request(`/api/preview/${file.id}`, { signal });
    const blob = await response.blob();
    if (signal.aborted) return;
    const url = URL.createObjectURL(blob);
    blobs.push(url);
    const image = element('img');
    image.src = url;
    image.alt = `Saved preview of ${file.relative_path}`;
    image.addEventListener('error', () => container.replaceChildren(document.createTextNode('Saved preview cannot be displayed')));
    container.replaceChildren(image);
  } catch (error) {
    if (!signal.aborted) container.textContent = 'Preview unavailable. Rerun audit to regenerate it.';
  }
}
async function save(group, action, selected, card) {
  if (busy) return;
  if (action === 'flag_selected' && !selected.size) return;
  busy = true;
  for (const button of document.querySelectorAll('button')) button.disabled = true;
  for (const input of card.querySelectorAll('input')) input.disabled = true;
  const status = card.querySelector('.group-status');
  status.classList.remove('error');
  status.textContent = 'Saving this group’s decision…';
  let saved = false;
  let result;
  try {
    await request('/api/decision', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fingerprint: group.fingerprint, action, selected: action === 'flag_selected' ? [...selected] : [] }) });
    saved = true;
    await load(false);
    // Reset only this group's draft after its saved state has been refreshed.
    selections.delete(group.fingerprint);
    const count = selected.size;
    result = { text: {
      keep_all: 'Saved: every image in this group is marked to keep.',
      flag_selected: `Saved: ${count} image${count === 1 ? '' : 's'} flagged in this group.`,
      defer: 'Saved: this group is deferred.',
      reset: 'This group’s decision and selection were cleared.',
    }[action] };
  } catch (error) {
    result = { text: saved ? 'Decision saved, but the gallery could not refresh. Reload to see the saved state.' : error.message, error: true };
  }
  feedback.set(group.fingerprint, result);
  busy = false;
  render();
  const updated = document.querySelector(`[data-fingerprint="${group.fingerprint}"]`);
  updated?.querySelector('.group-status').focus({ preventScroll: true });
  // Clearing a group in the Reviewed filter removes its card from this view.
  if (!updated) notice(result.text, !!result.error);
}
document.getElementById('previous').addEventListener('click', () => { if (page > 0 && !busy) { page--; render(); } });
document.getElementById('next').addEventListener('click', () => { if (!busy) { page++; render(); } });
if (!/^[a-f0-9]{64}$/.test(token)) notice('Open the complete private review URL printed by iarchive, including the part after #.', true);
else load().then(() => notice('Saved observations ready for review.')).catch(error => notice(error.message, true));
