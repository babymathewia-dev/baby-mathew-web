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
import base64
import logging
import sqlite3
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for
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
# App Flask
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
# Registro con Google + historial sincronizado
# ---------------------------------------------------------------------------
#
# Nota de infraestructura importante: esta base de datos es un archivo SQLite
# en el disco local del servidor. En Render, el disco de la instancia gratuita/
# estándar NO es persistente: si el servidor se reinicia o se redepliega, este
# archivo se borra y los usuarios/historiales registrados se pierden. Para que
# esto sea confiable en producción hace falta uno de los dos:
#   (a) un "persistent disk" de Render montado en esta ruta (tiene costo mensual), o
#   (b) migrar a una base de datos administrada (ej. Postgres de Render).
# Por ahora se deja en SQLite para poder probar la función completa ya mismo;
# esta migración queda pendiente antes de anunciar el registro como definitivo.
DB_PATH = BASE_DIR / "babymathew.db"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT,
            picture TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    return oauth.google.authorize_redirect(redirect_uri)


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
    return jsonify({
        "logged_in": True,
        "name": session.get("name"),
        "picture": session.get("picture"),
        "google_login_enabled": GOOGLE_LOGIN_ENABLED,
    })


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

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=FULL_SYSTEM_PROMPT,
            messages=cleaned,
            tools=[WEB_SEARCH_TOOL],
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
    except Exception:
        logger.exception("Error llamando a la API de IA")
        return jsonify({
            "error": "upstream_error",
            "reply": "Tuve un problema técnico respondiendo tu pregunta. Intenta de nuevo en un momento. "
                     "Si es urgente, no esperes: contacta a tu médico o acude a urgencias."
        }), 200

    # Si el usuario tiene sesión iniciada (registro con Google), guarda el
    # intercambio en su historial del servidor para que pueda continuarlo
    # desde cualquier dispositivo.
    if "user_id" in session:
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (session["user_id"], "user", last_user_text),
            )
            conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (session["user_id"], "assistant", reply_text),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("No se pudo guardar el intercambio en el historial del usuario")

    return jsonify({"reply": reply_text})


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
