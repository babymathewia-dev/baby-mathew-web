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
      var ruta = String(url && url.url || url);
      var metodo = String((opts && opts.method) || (url && url.method) || 'GET').toUpperCase();

      // [ANALÍTICA] Registro de crecimiento: solo QUÉ campos se llenaron (si/no).
      // Nunca se envían valores de peso, talla, perímetro ni fechas (dato de
      // salud de un menor — Ley 1581/2012, arts. 5 y 7).
      if (metodo === 'POST' && /\/api\/bebe\/crecimiento\/?$/.test(ruta.split('?')[0])) {
        var campos = {};
        try { campos = JSON.parse((opts && opts.body) || '{}') || {}; } catch (e) {}
        return fetchOriginal.apply(this, arguments).then(function (resp) {
          resp.clone().json().then(function (data) {
            if (resp.ok && data && data.ok) {
              gtag('event', 'crecimiento_registrado', {
                con_peso: campos.peso_g ? 'si' : 'no',
                con_talla: campos.talla_mm ? 'si' : 'no',
                con_pc: campos.perimetro_cefalico_mm ? 'si' : 'no'
              });
            }
          }).catch(function () {});
          return resp;
        });
      }

      var esChat = ruta.indexOf('/api/chat') !== -1;
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
          var conPerfil = null;    // [ANALÍTICA] true/false según el evento final del backend
          var tipoError = null;
          var terminado = false;   // llegó el evento final ("done" o "error")
          function procesarEvento(bloque) {
            // Un evento SSE puede traer varias líneas (p. ej. "event: x" + "data: {...}").
            var lineas = bloque.split(/\r?\n/);
            for (var j = 0; j < lineas.length; j++) {
              var l = lineas[j];
              if (l.indexOf('data:') !== 0) continue;
              var evt;
              try { evt = JSON.parse(l.slice(5).trim()); } catch (e) { continue; }
              if (evt && evt.error) { tipoError = String(evt.error); terminado = true; }
              if (evt && evt.done) {
                tema = evt.tema || null;
                if (typeof evt.con_perfil === 'boolean') conPerfil = evt.con_perfil;
                terminado = true;
              }
            }
          }
          try {
            var tipo = (resp.headers.get('content-type') || '').toLowerCase();
            if (tipo.indexOf('text/event-stream') === -1) {
              // [ANALÍTICA] Respuestas que NO son streaming (JSON): validaciones
              // tempranas del backend (archivo muy grande, tipo no soportado,
              // límite de mensajes 429) o un backend aún sin streaming.
              var data = await resp.clone().json();
              if (data && data.error) tipoError = String(data.error);
              else {
                tema = (data && data.tema) || null;
                if (data && typeof data.con_perfil === 'boolean') conPerfil = data.con_perfil;
              }
              terminado = true;
            } else {
              var reader = resp.clone().body.getReader();
              var decoder = new TextDecoder();
              var buffer = '';
              while (true) {
                var chunk = await reader.read();
                if (chunk.done) break;
                buffer += decoder.decode(chunk.value, { stream: true });
                var partes = buffer.split(/\r?\n\r?\n/);
                buffer = partes.pop();
                for (var i = 0; i < partes.length; i++) procesarEvento(partes[i]);
              }
              buffer += decoder.decode();
              if (buffer.trim()) procesarEvento(buffer);   // último evento sin línea en blanco final
            }
          } catch (e) {}
          var ms = Math.round(performance.now() - inicio);
          if (!tipoError && !terminado) tipoError = 'stream_incompleto';   // se cortó antes del final
          if (tipoError) {
            gtag('event', 'respuesta_error', { latencia_ms: ms, codigo_http: resp.status, tipo_error: tipoError });
          } else {
            var perfil = conPerfil === null ? 'desconocido' : (conPerfil ? 'si' : 'no');
            gtag('event', 'respuesta_recibida', { latencia_ms: ms, numero_pregunta: n, tema: tema || 'Sin clasificar', con_perfil: perfil });
            // Propiedad de usuario para comparar regreso de usuarios con y sin perfil del bebé.
            if (conPerfil !== null) gtag('set', 'user_properties', { tiene_perfil: conPerfil ? 'si' : 'no' });
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
      // [ANALÍTICA] #newChatBtn ahora abre el menú "Más opciones"; la nueva
      // conversación real es el ítem #menuNewChat (antes se contaba cada
      // apertura del menú como nueva conversación y se reiniciaba el conteo).
      if (t.closest('#menuNewChat')) {
        gtag('event', 'nueva_conversacion', { preguntas_previas: nPregunta });
        nPregunta = 0;
      } else if (t.closest('#menuBebe')) {
        gtag('event', 'perfil_bebe_abierto');
      } else if (t.closest('#menuAgenda')) {
        gtag('event', 'agenda_abierta');
      } else if (t.closest('#menuSonidos')) {
        gtag('event', 'sonidos_abiertos');
      } else if (t.closest('#menuTikTok')) {
        gtag('event', 'tiktok_click');
      } else if (t.closest('.gCard, [id^="guia"]')) {
        // [ANALÍTICA] Clic en una guía de pago (Hotmart). Se envía el nombre
        // corto de la guía, sacado del id (guiaEmbarazo -> embarazo), y el
        // título visible. Ningún dato del usuario.
        var card = t.closest('.gCard, [id^="guia"]');
        var slug = (card.id || '').replace(/^guia/, '').toLowerCase() || 'desconocida';
        var h3 = card.querySelector('h3');
        gtag('event', 'guia_click', {
          guia: slug,
          guia_titulo: h3 ? h3.textContent.trim().slice(0, 100) : slug
        });
      } else if (t.closest('#attachBtn')) {
        gtag('event', 'adjunto_click');
      } else if (t.closest('.chip')) {
        gtag('event', 'pregunta_ejemplo_click');
      }
    }, true);

    // 3) [ANALÍTICA] Gráfica de crecimiento vista: se detecta cuando la página
    // dibuja #crecimientoGrafica (un evento por apertura; los redibujos al
    // guardar o borrar una medición dentro de 30 s no se cuentan de nuevo).
    var ultimaGrafica = 0, nodoGrafica = null;
    function revisarGrafica() {
      var el = document.getElementById('crecimientoGrafica');
      if (el && el !== nodoGrafica) {          // solo cuando se dibuja de nuevo
        nodoGrafica = el;
        if (Date.now() - ultimaGrafica > 30000) {
          ultimaGrafica = Date.now();
          gtag('event', 'crecimiento_grafica_vista');
        }
      }
    }
    try {
      new MutationObserver(revisarGrafica).observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
  })();
