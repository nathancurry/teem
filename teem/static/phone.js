(() => {
  'use strict';

  const refreshLabel = document.getElementById('refresh-status');
  const runList = document.getElementById('run-list');
  const runStatus = document.getElementById('run-status');
  const runMatch = location.pathname.match(/^\/runs\/([0-9a-f-]{36})$/);
  let lastSeen = '';
  let refreshing = false;

  function setActionsEnabled(enabled) {
    document.querySelectorAll('form button').forEach(button => { button.disabled = !enabled; });
  }

  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(runMatch ? `${location.pathname}/state` : '/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('refresh failed');
      const state = await response.json();
      lastSeen = new Date(state.refreshed_at).toLocaleTimeString();
      if (refreshLabel) refreshLabel.textContent = `Current as of ${lastSeen}`;
      setActionsEnabled(true);
      if (runStatus && state.runs[0] && runStatus.dataset.status !== state.runs[0].status) {
        location.reload();
        return;
      }
      if (runList) {
        runList.replaceChildren();
        for (const run of state.runs) {
          const item = document.createElement('li');
          const link = document.createElement('a');
          link.href = `/runs/${run.id}`;
          link.textContent = `${run.project}: ${run.objective}`;
          item.append(link, document.createTextNode(` — ${run.label}, round ${run.round}. ${run.summary}`));
          if (run.stop_reason) item.append(document.createTextNode(`. Stop reason: ${run.stop_reason}`));
          runList.append(item);
        }
      }
    } catch (_) {
      if (refreshLabel) refreshLabel.textContent = `Last seen ${lastSeen || 'unknown'} — refresh required before acting`;
      setActionsEnabled(false);
    } finally {
      refreshing = false;
    }
  }
  refresh();
  setInterval(refresh, 5000);
  addEventListener('focus', refresh);
  addEventListener('online', refresh);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const values = new URLSearchParams(new FormData(form));
    if (event.submitter?.name) values.set(event.submitter.name, event.submitter.value);
    const button = event.submitter;
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, { method: 'POST', body: values,
        credentials: 'same-origin', cache: 'no-store' });
      if (response.redirected && new URL(response.url).origin === location.origin) {
        location.assign(response.url);
        return;
      }
      await refresh();
      if (refreshLabel) refreshLabel.textContent = response.status === 409 ?
        'Decision changed. Review the current Run before acting.' :
        `Submission was not accepted (${response.status}). Review the form and current state.`;
    } catch (_) {
      await refresh();
      if (refreshLabel) refreshLabel.textContent = 'Submission not confirmed. Check recent Runs before retrying this form.';
    } finally {
      if (button && (!refreshLabel || !refreshLabel.textContent.includes('refresh required'))) button.disabled = false;
    }
  }));
})();
