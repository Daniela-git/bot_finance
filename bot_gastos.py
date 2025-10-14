import os, json, re, datetime as dt
import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# === Cargar variables .env ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GSPREAD_SHEET_NAME", "gastos_diarios")
SA_JSON_PATH = os.getenv("GSPREAD_SA_JSON", "./service_account.json")
TZ = pytz.timezone(os.getenv("TZ", "America/Bogota"))

# === Inicializar clientes ===
client = OpenAI(api_key=OPENAI_API_KEY)

# === Google Sheets helpers ===
HEADERS = ["fecha","hora","valor","plataforma","tienda","categoria","subcategoria","detalle"]

# --- (NUEVO) Soporte para credencial desde variable de entorno ---
def ensure_sa_file():
    """
    Si existe la variable SERVICE_ACCOUNT_JSON con el contenido JSON de la
    cuenta de servicio, escribe ese contenido en el path SA_JSON_PATH
    (por defecto ./service_account.json). Así evitamos subir el archivo al repo.
    """
    sa_json_env = os.getenv("SERVICE_ACCOUNT_JSON")  # <-- pondremos esto en Railway/Render
    if sa_json_env:
        try:
            # Crear el archivo si no existe o si está vacío
            if (not os.path.exists(SA_JSON_PATH)) or os.path.getsize(SA_JSON_PATH) == 0:
                with open(SA_JSON_PATH, "w", encoding="utf-8") as f:
                    f.write(sa_json_env)
        except Exception as e:
            print("No pude escribir service_account.json desde SERVICE_ACCOUNT_JSON:", e)

# Llamar apenas arranca el proceso
ensure_sa_file()

def gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SA_JSON_PATH, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_sheet():
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    ws = sh.sheet1
    # Forzar encabezados correctos y en orden
    first_row = ws.row_values(1)
    if [h.lower() for h in first_row] != HEADERS:
        ws.clear()
        ws.append_row(HEADERS)
    return ws

# === Utilidades de validación de fecha/hora ===
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RX = re.compile(r"^[0-2]\d:[0-5]\d$")  # simple: 00:00–29:59 (luego validamos rango)

def is_valid_date(s: str) -> bool:
    if not s or not DATE_RX.match(s):
        return False
    try:
        dt.date.fromisoformat(s)
        return True
    except Exception:
        return False

def is_valid_time(s: str) -> bool:
    if not s or not TIME_RX.match(s):
        return False
    try:
        hh, mm = s.split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except Exception:
        return False

# === Parseo de JSON estricto desde la respuesta de GPT ===
def parse_json_strict(text):
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
    except Exception:
        pass
    return None

# === Llamada a GPT: NO inferir fecha/hora; dejarlas vacías si no están en el texto ===
def call_gpt_extract(msg_text):
    system_prompt = (
        "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON con estas claves exactas: "
        "{'fecha','hora','valor','plataforma','tienda','categoria','subcategoria','detalle'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- 'plataforma' es app (Uber, DiDi, Rappi, iFood, etc.) o vacío. "
        "- 'tienda' es comercio/lugar si se menciona. "
        "- 'categoria/subcategoria' concisas ('comida/almuerzo', 'transporte/taxi', etc.). "
        "- 'detalle' es descripción breve. "
        "- No incluyas explicaciones ni comentarios, solo el JSON."
    )
    user_prompt = f'Texto: "{msg_text}"'

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0.1,
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":user_prompt}
        ]
    )
    txt = resp.choices[0].message.content.strip()
    return parse_json_strict(txt)

# === Normalización: fecha/hora vacías o inválidas -> ahora; valor -> entero COP ===
def normalize_record(rec):
    now = dt.datetime.now(TZ)

    # valor -> entero (ej. "28.500" -> 28500)
    val = rec.get("valor")
    if isinstance(val, str):
        v = re.sub(r"[^\d,\.]", "", val)
        v = v.replace(".", "").replace(",", ".")
        try:
            val = int(round(float(v)))
        except Exception:
            val = ""
    rec["valor"] = val

    # fecha/hora: si vienen vacías o inválidas, usar actuales
    fecha = (rec.get("fecha") or "").strip()
    hora  = (rec.get("hora") or "").strip()

    if not is_valid_date(fecha):
        fecha = now.date().isoformat()
    if not is_valid_time(hora):
        hora = now.strftime("%H:%M")

    rec["fecha"] = fecha
    rec["hora"]  = hora

    # strings seguros y sin espacios sobrantes
    for k in ["plataforma","tienda","categoria","subcategoria","detalle"]:
        rec[k] = (rec.get(k,"") or "").strip()

    # asegurar todas las claves (por si GPT omite algo)
    for k in HEADERS:
        rec.setdefault(k, "")

    return rec

def persist_to_gsheets(rec):
    ws = get_or_create_sheet()
    row = [rec.get(k,"") for k in HEADERS]
    ws.append_row(row, value_input_option="USER_ENTERED")

# === Helpers de validación obligatoria ===
def has_required_description(rec) -> bool:
    """Debe existir al menos una: categoria, subcategoria o detalle."""
    return any(rec.get(k) for k in ("categoria", "subcategoria", "detalle"))

# === Telegram Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Soy tu bot de gastos.\n"
        "Datos obligatorios: 💰 valor y 📝 descripción (categoría/subcategoría/detalle).\n"
        "Ejemplos:\n"
        "• 'Uber 7.820 a la oficina'\n"
        "• 'Almuerzo 28.500 en El Corral'\n"
        "• 'Mercado Carulla 99.900 frutas y verduras'\n"
        "Guardaré todo en tu Google Sheets 'gastos_diarios'."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        rec = call_gpt_extract(text)
        if not rec:
            await update.message.reply_text("😅 No pude entender el gasto. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
            return

        rec = normalize_record(rec)

        # 🔒 Validación de campos obligatorios
        if not rec["valor"]:
            await update.message.reply_text("💰 Me falta el valor del gasto. Enviame el monto (ej: 25000 o 28.500).")
            return

        if not has_required_description(rec):
            await update.message.reply_text("📝 Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.")
            return

        # Persistir
        persist_to_gsheets(rec)

        await update.message.reply_text(
            f"✅ Guardado: {rec['categoria']} / {rec['subcategoria']} | ${rec['valor']} | {rec['fecha']} {rec['hora']}"
            + (f" | {rec['plataforma']}" if rec.get('plataforma') else "")
            + (f" | {rec['tienda']}" if rec.get('tienda') else "")
        )

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()
