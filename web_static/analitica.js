/* =====================================================================
   BABY MATHEW IA — ANALÍTICA (GA4) · archivo independiente
   Se carga desde index.html con: <script src="/analitica.js"></script>
   NO eliminar esa línea del <head> de index.html.
   Mide cada llamada al bot (/api/chat) y el tema clasificado por el backend.
   No envía el texto de preguntas ni respuestas (dato de salud, Ley 1581/2012).
   ===================================================================== */
(function () {
  var s = document.createElement('script');
  s.async = true;
  s.src = 'https://www.googletagmanager.com/gtag/js?id=G-8M6P16771E';
  document.head.appendChild(s);
})();
window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-8M6P16771E', { anonymize_ip: true });

  (function () {
    var nPregunta = 0;   // número de pregunta dentro de la conversación

    function tieneAdjunto(body) {
      try {
        if (body instanceof FormData) {
          var hay = false;
          body.forEach(function (v) { if (v instanceof File) hay = true; });
          return hay;
        }
        if (typeof body === 'string') {
          // Formato actual: "documents": [ {...} ] (o "document": {...} en clientes viejos)
          return /"(documents?|files?|attachments?|archivos?)"\s*:\s*(\{|\[\s*\{)/i.test(body);
        }
      } catch (e) {}
      return false;
    }

    // 1) Pregunta enviada, respuesta recibida y errores: se mide en la llamada al bot
    var fetchOriginal = window.fetch;
    window.fetch = function (url, opts) {
      var esChat = String(url && url.url || url).indexOf('/api/chat') !== -1;
      if (!esChat) return fetchOriginal.apply(this, arguments);

      nPregunta++;
      var inicio = performance.now();
      gtag('event', 'mensaje_enviado', {
        numero_pregunta: nPregunta,
        con_adjunto: tieneAdjunto(opts && opts.body) ? 'si' : 'no'
      });
      if (nPregunta === 1) gtag('event', 'conversacion_iniciada');

      return fetchOriginal.apply(this, arguments).then(function (resp) {
        var ms = Math.round(performance.now() - inicio);
        var n = nPregunta;
        if (!resp.ok) {
          gtag('event', 'respuesta_error', { latencia_ms: ms, codigo_http: resp.status, tipo_error: 'http' });
          return resp;
        }
        // Se lee una copia de la respuesta; la página sigue usando la original.
        resp.clone().json().then(function (d) {
          if (d && d.error) {
            gtag('event', 'respuesta_error', { latencia_ms: ms, codigo_http: resp.status, tipo_error: String(d.error) });
          } else {
            var tema = (d && d.tema) || 'Sin clasificar';
            gtag('event', 'respuesta_recibida', { latencia_ms: ms, numero_pregunta: n, tema: tema });
            gtag('event', 'tema_consultado', { tema: tema });
          }
        }).catch(function () {
          gtag('event', 'respuesta_recibida', { latencia_ms: ms, numero_pregunta: n, tema: 'Sin clasificar' });
        });
        return resp;
      }, function (err) {
        gtag('event', 'respuesta_error', {
          latencia_ms: Math.round(performance.now() - inicio), codigo_http: 0
        });
        throw err;
      });
    };

    // 2) Nueva conversación, uso del adjunto y clic en preguntas de ejemplo
    document.addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest('#newChatBtn')) {
        gtag('event', 'nueva_conversacion', { preguntas_previas: nPregunta });
        nPregunta = 0;
      } else if (t.closest('#attachBtn')) {
        gtag('event', 'adjunto_click');
      } else if (t.closest('.chip')) {
        gtag('event', 'pregunta_ejemplo_click');
      }
    }, true);
  })();
