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
        var n = nPregunta;
        if (!resp.ok || !resp.body) {
          var msErr = Math.round(performance.now() - inicio);
          gtag('event', 'respuesta_error', { latencia_ms: msErr, codigo_http: resp.status, tipo_error: 'http' });
          return resp;
        }
        // /api/chat ahora responde en streaming (Server-Sent Events) en vez de
        // un solo JSON, para que el texto aparezca progresivamente. Se lee una
        // COPIA del cuerpo (resp.clone()) trozo a trozo solo para sacar el tema
        // y detectar errores — la página sigue leyendo el body original para
        // mostrar el texto. "latencia_ms" se mide cuando el stream completo
        // termina (evento "done"/"error"), igual que antes medía el JSON
        // completo — no cuando llegan los primeros bytes.
        (async function () {
          var tema = null;
          var tipoError = null;
          try {
            var reader = resp.clone().body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';
            while (true) {
              var chunk = await reader.read();
              if (chunk.done) break;
              buffer += decoder.decode(chunk.value, { stream: true });
              var partes = buffer.split('\n\n');
              buffer = partes.pop();
              for (var i = 0; i < partes.length; i++) {
                var linea = partes[i];
                if (linea.indexOf('data: ') !== 0) continue;
                var evt;
                try { evt = JSON.parse(linea.slice(6)); } catch (e) { continue; }
                if (evt.error) tipoError = String(evt.error);
                if (evt.done) tema = evt.tema || 'Sin clasificar';
              }
            }
          } catch (e) {
            // conexión cortada a mitad del stream: se reporta como respuesta
            // recibida sin tema (mejor eso que inventar un error que no ocurrió
            // del lado del backend).
          }
          var ms = Math.round(performance.now() - inicio);
          if (tipoError) {
            gtag('event', 'respuesta_error', { latencia_ms: ms, codigo_http: resp.status, tipo_error: tipoError });
          } else {
            gtag('event', 'respuesta_recibida', { latencia_ms: ms, numero_pregunta: n, tema: tema || 'Sin clasificar' });
            gtag('event', 'tema_consultado', { tema: tema || 'Sin clasificar' });
          }
        })();
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
