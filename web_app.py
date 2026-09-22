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
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
import anthropic

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

# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="web_static", static_url_path="")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    document = data.get("document")  # opcional: {"name", "media_type", "data" (base64)}

    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "invalid_request"}), 400

    # Validación básica de forma (solo role/content, alternando, termina en user)
    cleaned = []
    for m in messages[-MAX_HISTORY_TURNS:]:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content})

    if not cleaned or cleaned[-1]["role"] != "user":
        return jsonify({"error": "invalid_request"}), 400

    # Si viene un documento adjunto, se agrega solo al último mensaje del usuario
    if document and isinstance(document, dict):
        media_type = document.get("media_type", "")
        b64data = document.get("data", "") or ""
        kind = ALLOWED_DOC_TYPES.get(media_type)

        if not kind:
            return jsonify({
                "error": "unsupported_file_type",
                "reply": "Por ahora solo puedo leer archivos PDF, imágenes (JPG/PNG/WEBP) o texto plano (.txt). "
                         "¿Puedes intentar con ese tipo de archivo?",
            }), 200

        approx_bytes = len(b64data) * 3 / 4
        if approx_bytes > MAX_DOC_BYTES:
            return jsonify({
                "error": "file_too_large",
                "reply": "Ese archivo pesa más de 10 MB, que es el máximo que puedo procesar por ahora. "
                         "¿Puedes enviar una versión más liviana?",
            }), 200

        last_text = cleaned[-1]["content"]
        blocks = [{"type": "text", "text": last_text}]

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

    return jsonify({"reply": reply_text})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Servidor web iniciado en el puerto %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
