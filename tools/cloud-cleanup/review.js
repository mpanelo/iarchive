'use strict';
const byId = id => document.getElementById(id);
const token = location.hash.slice(1);
let state;
let busy = false;

async function request(path, body) {
  const options = {headers: {Authorization: `Bearer ${token}`}, cache: 'no-store'};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Could not save the selection.');
  return result;
}

function paint(next) {
  state = next;
  if (!byId('cards').children.length) byId('cards').innerHTML = state.cards;
  const selected = new Set(state.selected);
  const closed = state.phase !== 'reviewing';
  document.querySelectorAll('.selection input').forEach(input => {
    input.checked = selected.has(input.value);
    input.disabled = busy || closed;
    const card = input.closest('article');
    card.classList.toggle('selected', input.checked);
    card.classList.toggle('kept', !input.checked);
    card.querySelector('.selection-label').textContent = input.checked ? 'Remove from cloud' : 'Keep in cloud';
  });
  byId('selection-summary').textContent = `${state.count} selected · ${state.total - state.count} kept`;
  byId('save-status').textContent = busy ? 'Updating selection…' : closed ? 'Review closed. Check Terminal for the result.' :
    state.count === 0 ? 'All items will be kept. Cancel in Terminal, or select an item.' :
    state.saved ? 'Copy the confirmation below, then paste it into Terminal.' : 'Unsaved changes. Previous confirmation is no longer valid.';
  byId('save').textContent = state.saved ? 'Continue to confirmation' : 'Save selection';
  byId('save').disabled = busy || closed || state.count === 0;
  byId('select-all').disabled = busy || closed || state.count === state.total;
  byId('keep-all').disabled = busy || closed || state.count === 0;
  byId('next-step').hidden = busy || !state.confirmation || closed;
  byId('confirmation').value = state.confirmation || '';
  byId('copy').disabled = busy || !state.confirmation || closed;
  const backups = byId('backups');
  if (!backups.children.length) state.roots.forEach((root, index) => {
    const row = document.createElement('li');
    const label = document.createElement('strong');
    label.textContent = `Copy ${index + 1}`;
    const path = document.createElement('code');
    path.textContent = root;
    row.append(label, path);
    backups.append(row);
  });
}

async function change(path, body) {
  if (busy || !state) return;
  busy = true;
  byId('message').textContent = '';
  byId('copy-status').textContent = '';
  // Update only card states, never replace the page or its illustrations.
  paint(body.selected ? {...state, selected: body.selected, count: body.selected.length,
    saved: false, confirmation: null} : state);
  let next = state;
  try {
    next = await request(path, body);
  } catch (error) {
    byId('message').textContent = error.message;
    try { next = await request('/api/state'); }
    catch (_) {
      next = {...state, saved: false, confirmation: null, phase: 'stopped'};
      byId('message').textContent = 'Review connection lost. Do not use an earlier confirmation; check Terminal.';
    }
  } finally {
    busy = false;
    paint(next);
    if (path === '/api/save' && state.confirmation) showConfirmation();
  }
}

function showConfirmation() {
  byId('next-step').scrollIntoView({block: 'center'});
  byId('copy').focus({preventScroll: true});
}

byId('cards').addEventListener('change', event => {
  if (!event.target.matches('.selection input')) return;
  const selected = [...document.querySelectorAll('.selection input:checked')].map(input => input.value);
  change('/api/draft', {revision: state.revision, selected});
});
byId('select-all').addEventListener('click', () => change('/api/draft', {
  revision: state.revision, selected: [...document.querySelectorAll('.selection input')].map(input => input.value)
}));
byId('keep-all').addEventListener('click', () => change('/api/draft', {revision: state.revision, selected: []}));
byId('save').addEventListener('click', () => {
  if (state.saved) showConfirmation();
  else change('/api/save', {revision: state.revision});
});
byId('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(state.confirmation);
    byId('copy-status').textContent = 'Copied. Paste into the waiting Terminal to run the simulation.';
  } catch (_) {
    byId('confirmation').focus();
    byId('confirmation').select();
    byId('copy-status').textContent = 'Copy the selected text, then paste it into Terminal.';
  }
});
if (!token) byId('message').textContent = 'Open the complete review link printed in Terminal.';
else request('/api/state').then(paint).catch(error => { byId('message').textContent = error.message; });
