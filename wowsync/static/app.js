'use strict';
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('session')) {
  sessionStorage.setItem('wowsync-session', fragment.get('session'));
  history.replaceState(null, '', location.pathname);
}
let state, plan, selected = new Set();
let busy = false;
function notice(message, error=false) {
  $('notice').textContent = message;
  $('notice').className = error ? 'error' : '';
  $('notice').hidden = false;
}
async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'X-WowSync-Session': sessionStorage.getItem('wowsync-session') || '', 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
async function task(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try { await action(); } catch (error) { notice(error.message, true); }
  finally { busy = false; document.querySelectorAll('button').forEach(b => b.disabled = false); }
}
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function option(value, text) { const o = node('option', text); o.value = value; return o; }
async function load() {
  const oldPeer = $('peer').value, oldProfile = $('profile').value;
  state = await api('status');
  $('device').textContent = state.device + (state.running ? ' · WoW running' : ' · Ready');
  $('install').textContent = state.install;
  $('statePath').textContent = 'Private data: ' + state.state;
  const versions = {'_retail_':'Retail', '_forever_':'Forever', '_classic_':'Classic', '_classic_beta_':'Classic Beta'};
  $('profile').replaceChildren(...state.profiles.map(p => option(p, versions[p] || p.replaceAll('_',' ').trim())));
  if (state.profiles.includes(oldProfile)) $('profile').value = oldProfile;
  $('peer').replaceChildren(option('', state.peers.length ? 'Choose a machine' : 'Pair a machine below'), ...state.peers.map(p => option(p.id,p.name)));
  if (state.peers.some(p => p.id === oldPeer)) $('peer').value = oldPeer;
  $('peerList').replaceChildren();
  for (const p of state.peers) {
    const row = node('div', undefined, 'peer-row'), info = node('div', p.name);
    info.append(node('small', p.url + ' · ' + p.profiles.join(', ')));
    const remove = node('button', 'Remove', 'secondary');
    remove.onclick = () => task(async () => { await api('remove-peer', {peer:p.id}); invalidate(); await load(); });
    row.append(info, remove); $('peerList').append(row);
  }
}
function invalidate() { plan = null; selected.clear(); $('review').hidden = true; }
function render() {
  if (!plan) return;
  const search = $('search').value.toLowerCase(), scope = $('scope').value, status = $('statusFilter').value;
  const filtered = plan.entries.filter(e => e.id.toLowerCase().includes(search)
    && (!scope || (scope === 'config' ? !!e.key : e.path.startsWith(scope + '/')))
    && (status === 'all' || (status ? e.status === status : e.status !== 'same')));
  $('count').textContent = plan.entries.length;
  $('changes').replaceChildren();
  for (const e of filtered.slice(0, 500)) {
    const row = node('tr'), pick = node('td'), box = document.createElement('input');
    box.type = 'checkbox'; box.checked = selected.has(e.id); box.disabled = !e.selectable;
    box.setAttribute('aria-label','Select ' + e.id);
    box.onchange = () => { if (box.checked) selected.add(e.id); else selected.delete(e.id); updateCount(); };
    pick.append(box);
    const path = node('td', e.path + (e.key ? ' → ' + e.key : ''), 'path');
    if (!e.key) path.append(node('div', `${(e.size/1024).toFixed(1)} KB · SHA ${e.local?.slice(0,8) || 'absent'} → ${e.remote?.slice(0,8) || 'absent'}`, 'muted'));
    const badge = node('td'); badge.append(node('span', e.status.replaceAll('-', ' '), 'badge ' + e.status));
    const delta = node('td'), view = node('button', 'View', 'link');
    view.onclick = () => task(async () => {
      const result = await api('diff', {plan:plan.id, entry:e.id});
      $('diffPath').textContent = e.id; $('diff').textContent = result.diff || 'Contents match.'; $('diffDialog').showModal();
    });
    delta.append(view); row.append(pick, path, badge, delta); $('changes').append(row);
  }
  $('pagination').textContent = filtered.length > 500 ? `Showing the first 500 of ${filtered.length} items. Narrow the filter to see more.` : `${filtered.length} matching items.`;
  updateCount();
}
function updateCount() { $('selectedCount').textContent = selected.size; }
async function historyList() {
  const rows = await api('history');
  $('backups').replaceChildren();
  if (!rows.length) $('backups').textContent = 'No syncs yet. Your first backup will appear here.';
  for (const entry of rows) {
    const row = node('div', undefined, 'backup-row');
    row.append(node('span', `${new Date(entry.created * 1000).toLocaleString()} · ${entry.profile} · ${Object.keys(entry.files).length} files · ${entry.status}`));
    if (['complete', 'prepared', 'failed'].includes(entry.status) && Object.keys(entry.files).length) {
      const restore = node('button', 'Restore', 'secondary');
      restore.onclick = () => { if (confirm('Restore these files to their pre-sync state? Newer edits will block restore.')) task(async () => {
        const result = await api('restore', {backup:entry.id}); invalidate(); notice(`Restored ${result.files} files.`); await historyList();
      }); };
      row.append(restore);
    }
    $('backups').append(row);
  }
}
$('compare').onclick = () => task(async () => {
  if (!$('peer').value || !$('profile').value) throw new Error('Choose a source machine and game version.');
  invalidate(); notice('Hashing folders and comparing changes…');
  plan = await api('compare', {peer:$('peer').value, profile:$('profile').value}); selected = new Set();
  $('review').hidden = false;
  $('summary').textContent = `Source: ${plan.remoteDevice}. Nothing is selected automatically. ${plan.skipped} excluded files on this machine.`;
  render(); notice('Preview ready. Select the changes you want to pull. Conflicts and machine settings require your explicit selection.');
});
$('selectIncoming').onclick = () => { if (!plan) return;
  // Bulk selection respects the active scope and search, and never selects conflicts.
  const scope = $('scope').value, query = $('search').value.toLowerCase();
  for (const e of plan.entries) if (e.status === 'incoming' && e.selectable && e.id.toLowerCase().includes(query)
    && (!scope || (scope === 'config' ? !!e.key : e.path.startsWith(scope+'/')))) selected.add(e.id);
  render();
};
$('apply').onclick = () => {
  if (!plan || !selected.size) { notice('Select at least one item.', true); return; }
  const manual = plan.entries.filter(e => selected.has(e.id) && ['conflict','machine-local','local-only'].includes(e.status));
  if (manual.length && !confirm(`Your selection overrides ${manual.length} conflicts, machine-specific settings, or local edits. Use the peer’s selected values?`)) return;
  task(async () => {
    notice('Downloading, verifying, backing up, and syncing selected files…');
    const result = await api('apply', {plan:plan.id, selected:[...selected]});
    invalidate(); notice(`Synced ${result.files} files. Backup: ${result.backup}`); await historyList(); await load();
  });
};
$('pairForm').onsubmit = event => { event.preventDefault(); task(async () => {
  const data = Object.fromEntries(new FormData(event.target)); await api('peers', data);
  event.target.reset(); await load(); notice('Machine paired. Choose it as a source above.');
}); };
$('showToken').onclick = () => task(async () => {
  if (!$('tokenPanel').hidden) { $('tokenPanel').hidden = true; $('token').textContent = ''; return; }
  $('token').textContent = (await api('token', {})).token; $('tokenPanel').hidden = false;
});
$('rotateToken').onclick = () => { if (confirm('Invalidate this token? Other machines must be paired again to read from this machine.')) task(async () => {
  await api('rotate-token', {}); $('token').textContent = (await api('token', {})).token; notice('Pairing token rotated. Update it on other machines.');
}); };
$('closeDiff').onclick = () => $('diffDialog').close();
$('refreshHistory').onclick = () => task(historyList);
for (const id of ['scope','statusFilter','search']) $(id).addEventListener('input', render);
for (const id of ['profile','peer']) $(id).addEventListener('change', invalidate);
task(async () => { await load(); await historyList(); });
