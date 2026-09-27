/* Service worker: receives Web Push and relays taps to the app.
 *
 * Deliberately minimal. It does not cache API responses - a stale internship
 * feed is worse than a brief loading state - and it does not decide what a
 * notification means; it hands the payload to the page, which owns routing
 * (see src/routes.ts routeForNotification).
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: "New internship", body: "Open the app to see it." };
  }
  const data = payload.data || {};
  event.waitUntil(
    self.registration.showNotification(payload.title || "New internship", {
      body: payload.body || "",
      data,
      tag: data.job_id || "jobmonitor",
      // Same job pushed twice must update the existing notification, not stack.
      renotify: false,
      icon: "/icon.svg",
      badge: "/icon.svg",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = data.deep_link || "/";

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        // Prefer an open tab: post the payload and let the app route.
        if ("focus" in client) {
          await client.focus();
          client.postMessage({ type: "jobmonitor:notification", payload: data });
          return;
        }
      }
      await self.clients.openWindow(target);
    })(),
  );
});
