"""
Servidor web — Agente de orientación para padres primerizos
Versión web del piloto (alternativa a Telegram para quien no quiera instalarlo)

Uso:
  1. pip install -r requirements.txt
  2. Configurar variables de entorno TELEGRAM_TOKEN y ANTHROPIC_API_KEY
     (usa el mismo archivo .env que ya tienes para el bot de Telegram)
  3. python web_app.py
  4. Abre http://localhost:5000 en tu navegador para probarlo localmente
  5. Para compartir el link con amigos, expón el puerto 5000 con un túnel
     (ver instrucciones en README_WEB.md)

Reutiliza el mismo system_prompt.md, kb_gestacion.md y kb_lactante.md que
el bot de Telegram — cualquier ajuste de contenido que hagas ahí aplica
a ambos canales sin tocar código.
"""

import os
import json
import base64
import logging
import secrets
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, Response, stream_with_context
import requests
import anthropic
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from authlib.integrations.flask_client import OAuth

# ---------------------------------------------------------------------------
# Configuración (misma lógica que bot.py)
# ---------------------------------------------------------------------------

def load_dotenv_simple(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

BASE_DIR = Path(__file__).parent
load_dotenv_simple(BASE_DIR / ".env")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
# [ANALÍTICA] Modelo pequeño y barato solo para clasificar el tema de cada pregunta.
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5")

if not ANTHROPIC_API_KEY:
    raise SystemExit("Falta la variable de entorno ANTHROPIC_API_KEY (revisa tu archivo .env).")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("baby-web")

def load_text(filename: str) -> str:
    path = BASE_DIR / filename
    if not path.exists():
        logger.warning("No se encontró %s — el servidor arrancará sin ese contenido.", filename)
        return ""
    return path.read_text(encoding="utf-8")

SYSTEM_PROMPT = load_text("system_prompt.md")
KB_GESTACION = load_text("kb_gestacion.md")
KB_LACTANTE = load_text("kb_lactante.md")
KB_INFANTE_MAYOR = load_text("kb_infante_mayor.md")

FULL_SYSTEM_PROMPT = f"""{SYSTEM_PROMPT}

---

# BASE DE CONOCIMIENTO — GESTACIÓN

{KB_GESTACION}

---

# BASE DE CONOCIMIENTO — LACTANTE MENOR (0-6 MESES)

{KB_LACTANTE}

---

# BASE DE CONOCIMIENTO — INFANTE MAYOR (6-24 MESES)

{KB_INFANTE_MAYOR}

---

Nota adicional: esta es la versión web del piloto (no Telegram). Mantén el mismo protocolo de seguridad y límites.
"""

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Herramienta de búsqueda web en vivo (Anthropic la ejecuta del lado del servidor).
# max_uses limita cuántas búsquedas puede hacer el modelo por mensaje (control de costo).
# Nota: "CO" (Colombia) no está soportado todavía como país en user_location, por eso
# no se restringe la ubicación aquí -- el modelo igual puede buscar información de Colombia,
# solo sin el sesgo geográfico automático.
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
}

# Antes, web_search viajaba en TODAS las consultas (incluidas las de sueño,
# lactancia, vacunas, etc. que el modelo ya sabe responder solo), y cada vez
# que el modelo decidía buscar, la respuesta tardaba varios segundos más —
# a veces lo suficiente para disparar el timeout de 55s. Con esta lista de
# disparadores solo se activa cuando la pregunta realmente necesita algo
# actual/local (direcciones, horarios, precios, noticias), que es la
# minoría de los casos reales de uso. No es perfecto (puede dejar pasar
# algún caso que sí lo necesitaba), pero el costo de un falso negativo es
# bajo: el modelo responde con lo que sabe en vez de buscar, no un error.
_WEB_SEARCH_TRIGGERS = (
    "donde queda", "donde esta", "direccion de", "cerca de mi", "cerca de aqui",
    "telefono de", "numero de", "horario de", "a que hora abre", "a que hora cierra",
    "cuanto cuesta", "precio de", "disponible en", "disponibilidad de",
    "noticia", "ultima hora", "hoy en dia", "actualmente", "este año", "2026",
    "ips cercana", "pediatra cerca", "farmacia cerca", "clinica cerca",
    "hospital cerca", "eps en", "agendar cita", "pagina web", "link de", "enlace de",
)


def necesita_busqueda_web(texto):
    """Heurística barata (sin llamada a la IA) para decidir si esta pregunta
    puede necesitar información actual/local y amerita activar web_search."""
    t = _normalizar(texto or "")
    return any(_normalizar(disparador) in t for disparador in _WEB_SEARCH_TRIGGERS)

MAX_HISTORY_TURNS = 12  # mensajes (usuario+bot) que se envían como contexto por request

# Tipos de archivo que el usuario puede adjuntar para que el agente los lea.
# "document" = se envía tal cual a Claude (lee el PDF de forma nativa, incluidas imágenes escaneadas).
# "image"    = se envía como imagen.
# "text"     = se decodifica a texto plano y se envía como documento de texto.
ALLOWED_DOC_TYPES = {
    "application/pdf": "document",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/png": "image",
    "image/webp": "image",
    "text/plain": "text",
}
MAX_DOC_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DOCS_PER_MESSAGE = 5  # tope de archivos/fotos adjuntos por mensaje
MAX_MESSAGE_CHARS = 4000  # tope por mensaje de usuario (control de costo de tokens)

# ---------------------------------------------------------------------------
# [ANALÍTICA] Clasificación de temas — NO ELIMINAR
# Asigna cada pregunta a UNA categoría fija. Solo la categoría viaja a la
# página (campo "tema" de /api/chat) y de ahí a Google Analytics. El texto de la
# pregunta nunca se registra en logs ni se envía a GA4.
# ---------------------------------------------------------------------------

TEMAS = [
    "Síntomas del embarazo",
    "Nutrición en el embarazo",
    "Controles y exámenes",
    "Parto y preparación",
    "Señales de alarma embarazo",
    "Sueño del bebé",
    "Lactancia",
    "Alimentación complementaria",
    "Desarrollo y hitos",
    "Vacunas",
    "Síntomas y enfermedades del bebé",
    "Cuidados del recién nacido",
    "Señales de alarma bebé",
    "Bienestar emocional de los padres",
    "Saludo o uso del bot",
    "Otro",
]

CLASSIFIER_PROMPT = (
    "Clasifica la PREGUNTA ACTUAL de un padre o madre en EXACTAMENTE una de estas categorías:\n"
    + "\n".join(f"- {t}" for t in TEMAS)
    + "\n\nLa PREGUNTA ANTERIOR (si viene) es solo contexto: úsala únicamente si la actual es un "
    "seguimiento corto que no se entiende sola (ej. '¿y si tiene fiebre?'). Si la actual cambia de "
    "tema, clasifica el tema NUEVO.\n"
    "Responde solo con el nombre exacto de la categoría, sin explicaciones ni comillas."
)

_classifier_pool = ThreadPoolExecutor(max_workers=4)


def _normalizar(texto):
    import unicodedata
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in texto).split())


_TEMAS_NORM = sorted(((_normalizar(t), t) for t in TEMAS), key=lambda x: -len(x[0]))


def classify_topic(pregunta_actual, pregunta_anterior=None):
    """Devuelve una categoría de TEMAS. Nunca lanza error: ante cualquier fallo, 'Otro'."""
    try:
        if not (pregunta_actual or "").strip():
            return "Otro"
        consulta = f"PREGUNTA ACTUAL:\n{pregunta_actual[:1500]}"
        if pregunta_anterior:
            consulta = f"PREGUNTA ANTERIOR (solo contexto):\n{pregunta_anterior[:800]}\n\n" + consulta
        resp = anthropic_client.with_options(timeout=15).messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=30,
            system=CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": consulta}],
        )
        etiqueta = _normalizar("".join(b.text for b in resp.content if b.type == "text"))
        # Coincidencia exacta y, si no, la categoría contenida en la respuesta
        # (tolera tildes, comillas, puntos o prefijos como "Categoría:").
        for norm, t in _TEMAS_NORM:
            if etiqueta == norm:
                return t
        for norm, t in _TEMAS_NORM:
            if norm in etiqueta:
                return t
        logger.info("clasificador_etiqueta_no_reconocida")  # nunca se registra el texto
        return "Otro"
    except Exception as e:
        logger.warning("No se pudo clasificar el tema (se usa 'Otro'): %s", type(e).__name__)
        return "Otro"

# ---------------------------------------------------------------------------
# App Flask
#
# IMPORTANTE — comando de arranque en Render: el worker de gunicorn tiene un
# timeout propio (30s por defecto) que no se configura aquí sino en el Start
# Command de Render (p.ej. "gunicorn web_app:app --timeout 75 --workers 2").
# Si ese valor es menor al timeout de 55s que se le pone a la llamada
# principal a la IA (ver /api/chat), gunicorn puede matar el proceso antes de
# que nuestro propio manejo de errores alcance a responder, y el usuario
# vuelve a ver el mensaje genérico de "no pude conectarme" en vez del mensaje
# claro que sí devolvemos nosotros. Verifica ese Start Command en el panel de
# Render — debe ser de al menos 75s.
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="web_static", static_url_path="")

# Tope de tamaño de solicitud a nivel servidor (independiente del límite de 10MB
# del documento, que se valida más adelante ya decodificado). ~14 MB cubre un
# adjunto de 10MB codificado en base64 (+33%) más el resto del payload JSON.
app.config["MAX_CONTENT_LENGTH"] = 14 * 1024 * 1024

# SECRET_KEY firma la cookie de sesión (login). Es obligatoria para que el
# registro con Google funcione. Genera una con:
#   python -c "import secrets; print(secrets.token_hex(32))"
# y agrégala como variable de entorno SECRET_KEY (en Render: Environment).
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    logger.warning(
        "Falta la variable de entorno SECRET_KEY: el login con Google y el "
        "historial sincronizado quedarán deshabilitados hasta que la configures."
    )
    SECRET_KEY = os.urandom(32).hex()  # solo para no tumbar el servidor; las sesiones no persistirán entre reinicios
app.secret_key = SECRET_KEY
# Cookie de sesión: persiste 30 días, solo por HTTPS, no accesible por JS.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

# Límite de solicitudes por IP a /api/chat: cada llamada tiene costo real
# (tokens de la API de Anthropic + búsqueda web). Dos niveles: uno generoso
# para no afectar el uso normal (incluso de varias personas compartiendo la
# misma IP), y uno diario como freno real contra abuso automatizado sostenido.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

# ---------------------------------------------------------------------------
# Cabeceras de seguridad HTTP (se aplican a toda respuesta, incluido el HTML,
# el JS y las respuestas de la API). Sitio detrás de HTTPS siempre (Cloudflare
# + Render), por eso se puede fijar HSTS sin condicionar por request.scheme.
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP: refleja lo que index.html realmente carga hoy (ver comentarios por
    # directiva). Si en el futuro se agrega un recurso externo nuevo (otra
    # fuente, otro CDN, otro dominio de imágenes), hay que sumarlo aquí o el
    # navegador lo bloqueará.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # [ANALÍTICA] googletagmanager.com: script de Google Analytics que carga /analitica.js. NO QUITAR.
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "  # CSS propio + Google Fonts
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://lh3.googleusercontent.com https://*.googleusercontent.com "  # fotos de perfil de Google
        "https://*.google-analytics.com https://www.googletagmanager.com; "  # [ANALÍTICA] GA4. NO QUITAR.
        # [ANALÍTICA] Envío de datos a Google Analytics 4 (dominios oficiales de GA4). NO QUITAR.
        "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com; "
        "frame-ancestors 'none'; "                            # nadie puede embeber el sitio en un iframe (clickjacking)
        "base-uri 'self'; "
        "form-action 'self' https://accounts.google.com; "
        "object-src 'none'"
    )
    return response

# ---------------------------------------------------------------------------
# Registro con Google + historial sincronizado
# ---------------------------------------------------------------------------
#
# Nota de infraestructura: esta base de datos es un archivo SQLite. Su
# ubicación se controla con la variable de entorno DB_DIR:
#   - En local, si DB_DIR no está definida, se usa la carpeta del proyecto
#     (comportamiento de siempre, sin configuración adicional).
#   - En Render, DB_DIR debe apuntar a la ruta de montaje de un "Persistent
#     Disk" (ej. /var/data), para que el archivo sobreviva a reinicios y
#     redeploys. Sin un disco persistente montado ahí, cada redeploy borra
#     el archivo y se pierden los usuarios/historiales registrados.
DB_DIR = Path(os.environ.get("DB_DIR", str(BASE_DIR)))
DB_PATH = DB_DIR / "babymathew.db"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# --- Recordatorios por correo (citas y medicamentos) ------------------------
# Servicio: Resend (resend.com). RESEND_API_KEY y RESEND_FROM se configuran
# como variables de entorno en Render. RESEND_FROM debe ser una dirección del
# dominio verificado en Resend (ej. recordatorios@babymathewia.com).
# TAREAS_SECRET_TOKEN protege /api/tareas/enviar-alertas: solo el Cron Job de
# Render (que lo manda como header) puede disparar el envío. Genera uno con:
#   python -c "import secrets; print(secrets.token_hex(32))"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "Baby Mathew IA <recordatorios@babymathewia.com>")
TAREAS_SECRET_TOKEN = os.environ.get("TAREAS_SECRET_TOKEN")
if not RESEND_API_KEY:
    logger.warning(
        "Falta la variable de entorno RESEND_API_KEY: los recordatorios por "
        "correo de citas y medicamentos quedarán deshabilitados hasta que la configures."
    )
if not TAREAS_SECRET_TOKEN:
    logger.warning(
        "Falta la variable de entorno TAREAS_SECRET_TOKEN: /api/tareas/enviar-alertas "
        "rechazará todas las solicitudes hasta que la configures."
    )

oauth = OAuth(app)
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
if GOOGLE_LOGIN_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    logger.warning(
        "Faltan GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET: el botón de 'Iniciar sesión con Google' "
        "quedará deshabilitado hasta que configures esas variables de entorno."
    )


def get_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


TERMS_VERSION = "2026-10-01"  # sube esta fecha cada vez que cambie el texto legal publicado en /terminos.html


def init_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT,
            picture TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            terms_accepted_at TEXT,
            terms_version TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # Feedback (👍/👎) por respuesta del bot. user_id es NULL para usuarios
    # anónimos (sin login) -- igual queremos su feedback, solo no podemos
    # atribuírselo a una cuenta. question/answer van completos para poder
    # revisar el caso real más adelante, no solo el conteo de votos.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # Migración para bases ya desplegadas antes de que existieran estas dos
    # columnas: CREATE TABLE IF NOT EXISTS no las agrega a una tabla que ya
    # existe, así que se intentan añadir por separado (falla en silencio si
    # ya están, que es el caso normal después de la primera vez).
    for ddl in (
        "ALTER TABLE users ADD COLUMN terms_accepted_at TEXT",
        "ALTER TABLE users ADD COLUMN terms_version TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # la columna ya existe

    # --- Perfil del bebé + Agenda de citas -------------------------------
    # Un "bebé" es una entidad independiente del usuario que lo crea: varios
    # cuidadores (mamá, papá, etc.) se vinculan a él vía bebe_cuidadores, así
    # que la agenda y las notas son compartidas entre todos los vinculados,
    # no privadas de quien las registró.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bebes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT,
            etapa TEXT NOT NULL CHECK (etapa IN ('gestacion', 'nacido')),
            semana_gestacion INTEGER,
            fecha_parto_probable TEXT,
            fecha_nacimiento TEXT,
            sexo TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bebe_cuidadores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bebe_id INTEGER NOT NULL REFERENCES bebes(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            rol TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (bebe_id, user_id)
        )"""
    )
    # Código de invitación de un solo uso para vincular a otro cuidador
    # (ej. el otro padre/madre) al mismo bebé, sin exponer el bebe_id directo.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bebe_invitaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bebe_id INTEGER NOT NULL REFERENCES bebes(id),
            codigo TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            used_by INTEGER REFERENCES users(id),
            used_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS citas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bebe_id INTEGER NOT NULL REFERENCES bebes(id),
            especialidad TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT,
            lugar TEXT,
            medico TEXT,
            valor TEXT,
            avisarme INTEGER NOT NULL DEFAULT 0,
            notas TEXT,
            estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'realizada', 'cancelada')),
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # Medicamentos: SOLO lo que el cuidador registra tal cual se lo indicó el
    # pediatra (nombre, cantidad, hora, frecuencia). El sistema nunca sugiere
    # ni calcula dosis — es un recordatorio de lo ya prescrito, mismo
    # principio que el disclaimer legal del chat.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS medicamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bebe_id INTEGER NOT NULL REFERENCES bebes(id),
            nombre TEXT NOT NULL,
            cantidad TEXT,
            hora TEXT,
            frecuencia TEXT,
            fecha_inicio TEXT,
            fecha_fin TEXT,
            notas TEXT,
            activo INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    conn.close()


init_db()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/auth/login")
def auth_login():
    if not GOOGLE_LOGIN_ENABLED:
        return jsonify({"error": "google_login_not_configured"}), 503
    redirect_uri = url_for("auth_callback", _external=True)
    # prompt="select_account": sin esto, Google omite la pantalla de elegir
    # cuenta si el navegador ya tiene una sesión activa, y loguea siempre con
    # esa misma cuenta. Con esto, el selector de cuentas de Google aparece
    # siempre, para poder elegir otra cuenta cuando se quiera.
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


@app.route("/auth/callback")
def auth_callback():
    if not GOOGLE_LOGIN_ENABLED:
        return redirect("/")
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.google.userinfo(token=token)
    except Exception:
        logger.exception("Error en el callback de Google OAuth")
        return redirect("/?login_error=1")

    google_sub = userinfo.get("sub")
    email = userinfo.get("email", "")
    name = userinfo.get("name", "") or email
    picture = userinfo.get("picture", "")
    if not google_sub:
        return redirect("/?login_error=1")

    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
    if row:
        user_id = row["id"]
        conn.execute(
            "UPDATE users SET email = ?, name = ?, picture = ? WHERE id = ?",
            (email, name, picture, user_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO users (google_sub, email, name, picture) VALUES (?, ?, ?, ?)",
            (google_sub, email, name, picture),
        )
        user_id = cur.lastrowid
    conn.commit()
    conn.close()

    session.permanent = True
    session["user_id"] = user_id
    session["name"] = name
    session["picture"] = picture
    return redirect("/")


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    if "user_id" not in session:
        return jsonify({"logged_in": False, "google_login_enabled": GOOGLE_LOGIN_ENABLED})
    conn = get_db()
    row = conn.execute(
        "SELECT terms_accepted_at, terms_version FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    conn.close()
    terms_accepted = bool(row and row["terms_accepted_at"] and row["terms_version"] == TERMS_VERSION)
    return jsonify({
        "logged_in": True,
        "name": session.get("name"),
        "picture": session.get("picture"),
        "google_login_enabled": GOOGLE_LOGIN_ENABLED,
        "terms_accepted": terms_accepted,
        "terms_version": TERMS_VERSION,
    })


@app.route("/api/accept-terms", methods=["POST"])
def api_accept_terms():
    # Evidencia de aceptación (Términos, num. 2): qué versión, cuándo, y
    # quién. Para usuarios anónimos la evidencia queda solo en su propio
    # navegador (localStorage) — no hay a quién atribuírsela en el servidor.
    if "user_id" not in session:
        return jsonify({"ok": True, "stored": "local_only"})
    conn = get_db()
    conn.execute(
        "UPDATE users SET terms_accepted_at = CURRENT_TIMESTAMP, terms_version = ? WHERE id = ?",
        (TERMS_VERSION, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "stored": "account", "terms_version": TERMS_VERSION})


@app.route("/api/history", methods=["GET"])
def api_history_get():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id ASC",
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return jsonify({"turns": [{"role": r["role"], "content": r["content"]} for r in rows]})


@app.route("/api/history", methods=["DELETE"])
def api_history_clear():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE user_id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["POST"])
@limiter.limit("30 per minute")
def api_feedback():
    data = request.get_json(silent=True) or {}
    rating = data.get("rating")
    question = data.get("question")
    answer = data.get("answer")
    if rating not in ("up", "down") or not isinstance(answer, str) or not answer.strip():
        return jsonify({"error": "invalid_request"}), 400
    if not isinstance(question, str):
        question = ""

    conn = get_db()
    conn.execute(
        "INSERT INTO feedback (user_id, question, answer, rating) VALUES (?, ?, ?, ?)",
        (session.get("user_id"), question[:MAX_MESSAGE_CHARS], answer[:4000], rating),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _mi_bebe_id(conn, user_id):
    """bebe_id vinculado al usuario, o None si todavía no tiene perfil creado
    ni se ha vinculado al de otro cuidador. Por ahora se asume 1 bebé por
    usuario (el modelo de datos soporta más, pero la UI actual solo maneja uno)."""
    row = conn.execute(
        "SELECT bebe_id FROM bebe_cuidadores WHERE user_id = ? ORDER BY id ASC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["bebe_id"] if row else None


def _generar_codigo_invitacion():
    import secrets
    import string
    alfabeto = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(6))


@app.route("/api/bebe", methods=["GET"])
def api_bebe_get():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"bebe": None})
    bebe = conn.execute("SELECT * FROM bebes WHERE id = ?", (bebe_id,)).fetchone()
    cuidadores = conn.execute(
        """SELECT u.id, u.name, u.picture, bc.rol
           FROM bebe_cuidadores bc JOIN users u ON u.id = bc.user_id
           WHERE bc.bebe_id = ? ORDER BY bc.id ASC""",
        (bebe_id,),
    ).fetchall()
    conn.close()
    return jsonify({
        "bebe": dict(bebe),
        "cuidadores": [dict(c) for c in cuidadores],
    })


@app.route("/api/bebe", methods=["POST"])
def api_bebe_save():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    data = request.get_json(silent=True) or {}
    etapa = data.get("etapa")
    if etapa not in ("gestacion", "nacido"):
        return jsonify({"error": "invalid_request"}), 400

    campos = {
        "nombre": (data.get("nombre") or "").strip()[:80] or None,
        "etapa": etapa,
        "semana_gestacion": data.get("semana_gestacion") if etapa == "gestacion" else None,
        "fecha_parto_probable": (data.get("fecha_parto_probable") or None) if etapa == "gestacion" else None,
        "fecha_nacimiento": (data.get("fecha_nacimiento") or None) if etapa == "nacido" else None,
        "sexo": (data.get("sexo") or "").strip()[:40] or None,
    }
    rol = (data.get("rol") or "").strip()[:40] or None

    conn = get_db()
    user_id = session["user_id"]
    bebe_id = _mi_bebe_id(conn, user_id)
    if bebe_id:
        conn.execute(
            """UPDATE bebes SET nombre=?, etapa=?, semana_gestacion=?, fecha_parto_probable=?,
               fecha_nacimiento=?, sexo=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (*campos.values(), bebe_id),
        )
        if rol:
            conn.execute(
                "UPDATE bebe_cuidadores SET rol=? WHERE bebe_id=? AND user_id=?",
                (rol, bebe_id, user_id),
            )
    else:
        cur = conn.execute(
            """INSERT INTO bebes (nombre, etapa, semana_gestacion, fecha_parto_probable,
               fecha_nacimiento, sexo, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (*campos.values(), user_id),
        )
        bebe_id = cur.lastrowid
        conn.execute(
            "INSERT INTO bebe_cuidadores (bebe_id, user_id, rol) VALUES (?, ?, ?)",
            (bebe_id, user_id, rol or "Cuidador"),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "bebe_id": bebe_id})


@app.route("/api/bebe/invitar", methods=["POST"])
@limiter.limit("10 per hour")
def api_bebe_invitar():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    codigo = _generar_codigo_invitacion()
    conn.execute(
        "INSERT INTO bebe_invitaciones (bebe_id, codigo, created_by) VALUES (?, ?, ?)",
        (bebe_id, codigo, session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "codigo": codigo})


@app.route("/api/bebe/vincular", methods=["POST"])
@limiter.limit("10 per hour")
def api_bebe_vincular():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    data = request.get_json(silent=True) or {}
    codigo = (data.get("codigo") or "").strip().upper()
    rol = (data.get("rol") or "").strip()[:40] or "Cuidador"
    if not codigo:
        return jsonify({"error": "invalid_request"}), 400

    conn = get_db()
    user_id = session["user_id"]
    if _mi_bebe_id(conn, user_id):
        conn.close()
        return jsonify({"error": "ya_tiene_perfil"}), 400

    inv = conn.execute(
        "SELECT * FROM bebe_invitaciones WHERE codigo = ? AND used_by IS NULL",
        (codigo,),
    ).fetchone()
    if not inv:
        conn.close()
        return jsonify({"error": "codigo_invalido"}), 404

    try:
        conn.execute(
            "INSERT INTO bebe_cuidadores (bebe_id, user_id, rol) VALUES (?, ?, ?)",
            (inv["bebe_id"], user_id, rol),
        )
        conn.execute(
            "UPDATE bebe_invitaciones SET used_by=?, used_at=CURRENT_TIMESTAMP WHERE id=?",
            (user_id, inv["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "ya_vinculado"}), 400
    conn.close()
    return jsonify({"ok": True, "bebe_id": inv["bebe_id"]})


@app.route("/api/citas", methods=["GET"])
def api_citas_list():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    rows = conn.execute(
        """SELECT c.*, u.name AS creado_por_nombre
           FROM citas c JOIN users u ON u.id = c.created_by
           WHERE c.bebe_id = ? ORDER BY c.fecha ASC, c.hora ASC""",
        (bebe_id,),
    ).fetchall()
    conn.close()
    return jsonify({"citas": [dict(r) for r in rows]})


@app.route("/api/citas", methods=["POST"])
@limiter.limit("30 per hour")
def api_citas_create():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    user_id = session["user_id"]
    bebe_id = _mi_bebe_id(conn, user_id)
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400

    data = request.get_json(silent=True) or {}
    especialidad = (data.get("especialidad") or "").strip()[:80]
    fecha = (data.get("fecha") or "").strip()[:10]
    if not especialidad or not fecha:
        conn.close()
        return jsonify({"error": "invalid_request"}), 400

    cur = conn.execute(
        """INSERT INTO citas (bebe_id, especialidad, fecha, hora, lugar, medico, valor,
           avisarme, notas, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bebe_id, especialidad, fecha,
            (data.get("hora") or "").strip()[:5] or None,
            (data.get("lugar") or "").strip()[:120] or None,
            (data.get("medico") or "").strip()[:120] or None,
            (data.get("valor") or "").strip()[:40] or None,
            1 if data.get("avisarme") else 0,
            (data.get("notas") or "").strip()[:2000] or None,
            user_id,
        ),
    )
    conn.commit()
    cita_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": cita_id})


@app.route("/api/citas/<int:cita_id>", methods=["PUT"])
def api_citas_update(cita_id):
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    cita = conn.execute("SELECT id FROM citas WHERE id = ? AND bebe_id = ?", (cita_id, bebe_id)).fetchone()
    if not cita:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    campos = []
    valores = []
    if "notas" in data:
        campos.append("notas = ?")
        valores.append((data.get("notas") or "").strip()[:2000] or None)
    if "estado" in data and data.get("estado") in ("pendiente", "realizada", "cancelada"):
        campos.append("estado = ?")
        valores.append(data.get("estado"))
    if "avisarme" in data:
        campos.append("avisarme = ?")
        valores.append(1 if data.get("avisarme") else 0)
    if not campos:
        conn.close()
        return jsonify({"error": "invalid_request"}), 400
    campos.append("updated_at = CURRENT_TIMESTAMP")
    valores.append(cita_id)
    conn.execute(f"UPDATE citas SET {', '.join(campos)} WHERE id = ?", valores)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/citas/<int:cita_id>", methods=["DELETE"])
def api_citas_delete(cita_id):
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    conn.execute("DELETE FROM citas WHERE id = ? AND bebe_id = ?", (cita_id, bebe_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/medicamentos", methods=["GET"])
def api_medicamentos_list():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    rows = conn.execute(
        """SELECT m.*, u.name AS creado_por_nombre
           FROM medicamentos m JOIN users u ON u.id = m.created_by
           WHERE m.bebe_id = ? ORDER BY m.activo DESC, m.hora ASC""",
        (bebe_id,),
    ).fetchall()
    conn.close()
    return jsonify({"medicamentos": [dict(r) for r in rows]})


@app.route("/api/medicamentos", methods=["POST"])
@limiter.limit("30 per hour")
def api_medicamentos_create():
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    user_id = session["user_id"]
    bebe_id = _mi_bebe_id(conn, user_id)
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400

    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()[:120]
    if not nombre:
        conn.close()
        return jsonify({"error": "invalid_request"}), 400

    cur = conn.execute(
        """INSERT INTO medicamentos (bebe_id, nombre, cantidad, hora, frecuencia,
           fecha_inicio, fecha_fin, notas, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bebe_id, nombre,
            (data.get("cantidad") or "").strip()[:60] or None,
            (data.get("hora") or "").strip()[:5] or None,
            (data.get("frecuencia") or "").strip()[:60] or None,
            (data.get("fecha_inicio") or "").strip()[:10] or None,
            (data.get("fecha_fin") or "").strip()[:10] or None,
            (data.get("notas") or "").strip()[:500] or None,
            user_id,
        ),
    )
    conn.commit()
    medicamento_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": medicamento_id})


@app.route("/api/medicamentos/<int:medicamento_id>", methods=["PUT"])
def api_medicamentos_update(medicamento_id):
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    med = conn.execute(
        "SELECT id FROM medicamentos WHERE id = ? AND bebe_id = ?", (medicamento_id, bebe_id)
    ).fetchone()
    if not med:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    data = request.get_json(silent=True) or {}
    campos = []
    valores = []
    if "activo" in data:
        campos.append("activo = ?")
        valores.append(1 if data.get("activo") else 0)
    if "notas" in data:
        campos.append("notas = ?")
        valores.append((data.get("notas") or "").strip()[:500] or None)
    if not campos:
        conn.close()
        return jsonify({"error": "invalid_request"}), 400
    campos.append("updated_at = CURRENT_TIMESTAMP")
    valores.append(medicamento_id)
    conn.execute(f"UPDATE medicamentos SET {', '.join(campos)} WHERE id = ?", valores)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/medicamentos/<int:medicamento_id>", methods=["DELETE"])
def api_medicamentos_delete(medicamento_id):
    if "user_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_db()
    bebe_id = _mi_bebe_id(conn, session["user_id"])
    if not bebe_id:
        conn.close()
        return jsonify({"error": "sin_perfil"}), 400
    conn.execute("DELETE FROM medicamentos WHERE id = ? AND bebe_id = ?", (medicamento_id, bebe_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Recordatorios por correo: citas (día anterior) y medicamentos activos (hoy)
# ---------------------------------------------------------------------------
def _enviar_correo(destinatario, asunto, html):
    """Envía un correo vía Resend. Devuelve True/False; nunca lanza excepción
    hacia el llamador (un fallo de envío no debe tumbar el resto del job)."""
    if not RESEND_API_KEY:
        return False
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM, "to": [destinatario], "subject": asunto, "html": html},
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.warning("Resend rechazó un correo a %s: %s %s", destinatario, resp.status_code, resp.text[:300])
            return False
        return True
    except requests.RequestException as e:
        logger.warning("Fallo de red enviando correo a %s: %s", destinatario, e)
        return False


def _cuidadores_de(conn, bebe_id):
    """Emails + nombre de todos los cuidadores vinculados a un bebé."""
    rows = conn.execute(
        """SELECT u.email, u.name FROM bebe_cuidadores bc
           JOIN users u ON u.id = bc.user_id WHERE bc.bebe_id = ?""",
        (bebe_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _nombre_bebe(conn, bebe_id):
    row = conn.execute("SELECT nombre, etapa FROM bebes WHERE id = ?", (bebe_id,)).fetchone()
    if not row:
        return "tu bebé"
    return row["nombre"] or ("tu bebé en camino" if row["etapa"] == "gestacion" else "tu bebé")


def _html_recordatorio(nombre_bebe, citas, medicamentos):
    partes = [
        "<div style=\"font-family:'Source Sans 3',Arial,sans-serif;color:#173330;max-width:480px;margin:0 auto;\">",
        "<h2 style=\"color:#2B6A67;\">Recordatorios de " + nombre_bebe + "</h2>",
    ]
    if citas:
        partes.append("<h3 style=\"color:#2B6A67;font-size:1rem;\">📅 Cita médica mañana</h3>")
        for c in citas:
            linea = f"{c['especialidad']} — {c['fecha']}"
            if c.get("hora"):
                linea += f" a las {c['hora']}"
            if c.get("lugar"):
                linea += f"<br>Lugar: {c['lugar']}"
            if c.get("medico"):
                linea += f"<br>Con: {c['medico']}"
            partes.append(f"<p style='background:#EAF6F4;border-radius:10px;padding:12px 14px;'>{linea}</p>")
    if medicamentos:
        partes.append("<h3 style=\"color:#2B6A67;font-size:1rem;\">💊 Medicamentos de hoy</h3>")
        partes.append(
            "<p style='font-size:.78rem;color:#5C7A76;'>Tal como lo registraste según indicación médica. "
            "Baby Mathew IA no sugiere ni calcula dosis.</p>"
        )
        for m in medicamentos:
            linea = f"{m['nombre']}"
            if m.get("cantidad"):
                linea += f" — {m['cantidad']}"
            if m.get("hora"):
                linea += f" a las {m['hora']}"
            if m.get("frecuencia"):
                linea += f"<br>Frecuencia: {m['frecuencia']}"
            partes.append(f"<p style='background:#EAF6F4;border-radius:10px;padding:12px 14px;'>{linea}</p>")
    partes.append(
        "<p style='font-size:.72rem;color:#93ACA8;margin-top:20px;'>Baby Mathew IA · "
        "Este correo es un recordatorio de lo que tú registraste, no un consejo médico.</p>"
    )
    partes.append("</div>")
    return "".join(partes)


@app.route("/api/tareas/enviar-alertas", methods=["POST"])
def api_tareas_enviar_alertas():
    """Dispara el envío diario de recordatorios. Protegido por un token
    secreto (header X-Tarea-Token) — pensado para ser llamado por un Cron Job
    de Render, no por el navegador del usuario."""
    token_recibido = request.headers.get("X-Tarea-Token", "")
    if not TAREAS_SECRET_TOKEN or not secrets.compare_digest(token_recibido, TAREAS_SECRET_TOKEN):
        return jsonify({"error": "no_autorizado"}), 401

    hoy = date.today()
    manana = (hoy + timedelta(days=1)).isoformat()
    hoy_str = hoy.isoformat()

    conn = get_db()

    citas_rows = conn.execute(
        """SELECT * FROM citas WHERE estado = 'pendiente' AND avisarme = 1 AND fecha = ?""",
        (manana,),
    ).fetchall()

    medicamentos_rows = conn.execute(
        """SELECT * FROM medicamentos WHERE activo = 1
           AND (fecha_inicio IS NULL OR fecha_inicio <= ?)
           AND (fecha_fin IS NULL OR fecha_fin >= ?)""",
        (hoy_str, hoy_str),
    ).fetchall()

    por_bebe = {}
    for r in citas_rows:
        por_bebe.setdefault(r["bebe_id"], {"citas": [], "medicamentos": []})["citas"].append(dict(r))
    for r in medicamentos_rows:
        por_bebe.setdefault(r["bebe_id"], {"citas": [], "medicamentos": []})["medicamentos"].append(dict(r))

    correos_enviados = 0
    correos_fallidos = 0
    bebes_procesados = 0

    for bebe_id, info in por_bebe.items():
        cuidadores = _cuidadores_de(conn, bebe_id)
        if not cuidadores:
            continue
        nombre_bebe = _nombre_bebe(conn, bebe_id)
        html = _html_recordatorio(nombre_bebe, info["citas"], info["medicamentos"])
        asunto = "Recordatorio de " + nombre_bebe
        bebes_procesados += 1
        for cuidador in cuidadores:
            if not cuidador.get("email"):
                continue
            if _enviar_correo(cuidador["email"], asunto, html):
                correos_enviados += 1
            else:
                correos_fallidos += 1

    conn.close()
    return jsonify({
        "ok": True,
        "bebes_procesados": bebes_procesados,
        "correos_enviados": correos_enviados,
        "correos_fallidos": correos_fallidos,
    })


@app.route("/api/chat", methods=["POST"])
@limiter.limit("20 per minute")
@limiter.limit("300 per day")
def chat():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    # "documents": lista de adjuntos {"name", "media_type", "data" (base64)}.
    # Se mantiene "document" (singular) por compatibilidad con clientes viejos.
    documents = data.get("documents")
    if not isinstance(documents, list):
        single = data.get("document")
        documents = [single] if isinstance(single, dict) else []
    documents = documents[:MAX_DOCS_PER_MESSAGE]

    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "invalid_request"}), 400

    # Validación básica de forma (solo role/content, alternando, termina en user,
    # y con un tope de longitud por mensaje para controlar el costo de tokens)
    cleaned = []
    for m in messages[-MAX_HISTORY_TURNS:]:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})

    if not cleaned or cleaned[-1]["role"] != "user":
        return jsonify({"error": "invalid_request"}), 400

    # Texto plano del último mensaje del usuario, para guardar en el historial
    # si está logueado (el documento adjunto NUNCA se guarda, solo se usa para
    # esta respuesta — coincide con lo que promete el aviso de privacidad).
    last_user_text = cleaned[-1]["content"]

    # [ANALÍTICA] Clasificación del tema en paralelo a la respuesta (no suma espera).
    # Clasifica la pregunta actual; la anterior solo sirve de contexto para seguimientos cortos.
    preguntas = [m["content"] for m in cleaned if m["role"] == "user" and isinstance(m["content"], str)]
    tema_futuro = _classifier_pool.submit(
        classify_topic, preguntas[-1] if preguntas else "", preguntas[-2] if len(preguntas) > 1 else None
    )

    # Si vienen documentos/fotos adjuntos, se agregan al último mensaje del usuario
    if documents:
        blocks = [{"type": "text", "text": last_user_text}]
        has_unsupported_type = False
        has_oversized = False

        for doc in documents:
            if not isinstance(doc, dict):
                continue
            media_type = doc.get("media_type", "")
            b64data = doc.get("data", "") or ""
            kind = ALLOWED_DOC_TYPES.get(media_type)

            if not kind:
                has_unsupported_type = True
                continue

            approx_bytes = len(b64data) * 3 / 4
            if approx_bytes > MAX_DOC_BYTES:
                has_oversized = True
                continue

            if kind == "document":
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64data}})
            elif kind == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}})
            elif kind == "text":
                try:
                    text_content = base64.b64decode(b64data).decode("utf-8", errors="replace")
                except Exception:
                    text_content = ""
                blocks.append({"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text_content}})

        if len(blocks) == 1:
            # Ningún adjunto se pudo procesar: se avisa con el motivo más relevante.
            if has_oversized:
                return jsonify({
                    "error": "file_too_large",
                    "reply": "Alguno de esos archivos pesa más de 10 MB, que es el máximo que puedo procesar por ahora. "
                             "¿Puedes enviar una versión más liviana?",
                }), 200
            if has_unsupported_type:
                return jsonify({
                    "error": "unsupported_file_type",
                    "reply": "Por ahora solo puedo leer archivos PDF, imágenes (JPG/PNG/WEBP) o texto plano (.txt). "
                             "¿Puedes intentar con ese tipo de archivo?",
                }), 200
        else:
            cleaned[-1] = {"role": "user", "content": blocks}

    # web_search ya NO viaja en todas las consultas (ver necesita_busqueda_web
    # más arriba) — solo cuando la pregunta parece necesitar algo actual/local.
    # Esto evita el ida-y-vuelta extra de una búsqueda innecesaria, que era
    # una de las dos causas principales de las respuestas lentas/timeout.
    usar_busqueda = necesita_busqueda_web(last_user_text)

    # user_id se captura AHORA (dentro del contexto de la request) porque el
    # generador de abajo corre mientras Flask va enviando la respuesta, y para
    # entonces `session` ya podría no estar disponible de forma confiable.
    user_id = session.get("user_id")

    def generate():
        reply_text = ""
        try:
            # with_options(timeout=...) + streaming: antes la respuesta completa
            # se armaba del lado del servidor y solo se enviaba al final, así que
            # el usuario veía "Pensando..." fijo mientras tanto (a veces más de
            # 30-40s, sobre todo si el modelo decidía buscar en la web). Ahora el
            # texto se transmite a medida que el modelo lo genera (Server-Sent
            # Events), igual que ChatGPT/Claude.ai, y el timeout de 55s sigue
            # protegiendo contra una llamada que nunca termina. 55s deja margen
            # bajo el --timeout de gunicorn en Render (ver nota en el comando de
            # arranque / README_WEB.md) — si ese valor cambia allá, este número
            # debe quedar varios segundos por debajo.
            create_kwargs = dict(
                model=MODEL,
                # 800 se quedaba corto y cortaba respuestas a mitad de palabra,
                # sobre todo cuando se usa la herramienta de búsqueda web (esas
                # llamadas también consumen parte de este mismo presupuesto de
                # tokens, dejando menos espacio para el texto final). 2048 da
                # margen de sobra para una respuesta completa aun con búsqueda
                # de por medio; el límite real de longitud lo pone el prompt
                # (ver "Formato de respuesta" en system_prompt.md), no este tope.
                max_tokens=2048,
                # Prompt caching: el system prompt + bases de conocimiento (~10k tokens)
                # no cambian entre mensajes, así que se marcan como cacheables (cache_control
                # ephemeral). Esto reduce fuertemente el costo y la latencia de cada turno,
                # ya que Anthropic reutiliza el procesamiento de ese bloque en vez de
                # reprocesarlo completo en cada llamada.
                system=[
                    {
                        "type": "text",
                        "text": FULL_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=cleaned,
            )
            if usar_busqueda:
                create_kwargs["tools"] = [WEB_SEARCH_TOOL]

            with anthropic_client.with_options(timeout=55).messages.stream(**create_kwargs) as stream:
                for texto in stream.text_stream:
                    reply_text += texto
                    yield f"data: {json.dumps({'delta': texto})}\n\n"
        except anthropic.APITimeoutError:
            logger.warning("Timeout llamando a la API de IA (>55s)")
            yield "data: " + json.dumps({
                "error": "upstream_timeout",
                "reply": "Esta pregunta tardó más de lo normal en procesarse (puede pasar cuando busco información "
                         "actualizada en internet) y tuve que detenerme. Intenta de nuevo — normalmente la segunda "
                         "vez responde rápido. Si es urgente, no esperes: contacta a tu médico o acude a urgencias.",
            }) + "\n\n"
            return
        except Exception:
            logger.exception("Error llamando a la API de IA")
            yield "data: " + json.dumps({
                "error": "upstream_error",
                "reply": "Tuve un problema técnico respondiendo tu pregunta. Intenta de nuevo en un momento. "
                         "Si es urgente, no esperes: contacta a tu médico o acude a urgencias.",
            }) + "\n\n"
            return

        # Si el usuario tiene sesión iniciada (registro con Google), guarda el
        # intercambio en su historial del servidor para que pueda continuarlo
        # desde cualquier dispositivo.
        if user_id:
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (user_id, "user", last_user_text),
                )
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (user_id, "assistant", reply_text),
                )
                conn.commit()
                conn.close()
            except Exception:
                logger.exception("No se pudo guardar el intercambio en el historial del usuario")

        # [ANALÍTICA] Tema de la consulta (solo la categoría, nunca el texto)
        try:
            tema = tema_futuro.result(timeout=5)
        except Exception:
            tema = "Otro"
        logger.info("consulta_respondida tema=%s", tema)

        yield "data: " + json.dumps({"done": True, "tema": tema}) + "\n\n"

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    # Evita que algún proxy intermedio (p.ej. Cloudflare, que está delante de
    # este sitio) almacene en búfer la respuesta esperando a que termine antes
    # de entregarla — eso anularía el streaming y se vería igual que antes.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        "error": "rate_limited",
        "reply": "Has enviado muchos mensajes en poco tiempo. Espera un momento y vuelve a intentarlo.",
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Servidor web iniciado en el puerto %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
