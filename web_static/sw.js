// Service worker mínimo: solo existe para que el navegador (sobre todo Android/Chrome)
// considere el sitio "instalable" como app. No cachea nada a propósito: el chat
// siempre necesita conexión en vivo con el servidor, así que no tiene sentido
// servir contenido offline ni arriesgarse a mostrar una versión vieja de la página.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Passthrough: deja que cada solicitud vaya normalmente a la red.
  event.respondWith(fetch(event.request));
});
