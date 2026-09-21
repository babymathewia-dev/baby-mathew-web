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

MAX_HISTORY_TURNS = 12  # mensajes (usuario+bot) que se envían como contexto por request

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

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=FULL_SYSTEM_PROMPT,
            messages=cleaned,
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
