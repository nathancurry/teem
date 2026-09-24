self.addEventListener('push', event => {
  const runId = event.data ? event.data.text() : '';
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(runId)) return;
  event.waitUntil(self.registration.showNotification('Teem', {
    body: 'Teem has an update. Open Teem to view it.',
    tag: `teem-${runId}`, data: { url: `/runs/${runId}` }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/', self.location.origin);
  if (url.origin !== self.location.origin || !/^\/runs\/[0-9a-f-]{36}$/.test(url.pathname)) return;
  event.waitUntil((async () => {
    const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (clientsList.length) {
      await clientsList[0].navigate(url.href);
      await clientsList[0].focus();
    } else await self.clients.openWindow(url.href);
  })());
});
