import asyncio
import os, json, re, datetime as dt
import threading
import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from notion import add_new_page, get_database_id, generate_page, get_deudores
from datetime import datetime
from threading import Thread
from flask import Flask, request

# === Cargar variables .env ===
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GSPREAD_SHEET_NAME", "gastos_diarios")
SA_JSON_PATH = os.getenv("GSPREAD_SA_JSON", "./service_account.json")
TZ = pytz.timezone(os.getenv("TZ", "America/Bogota"))

fecha = datetime.now()
year = str(fecha.year)

# === Inicializar clientes ===
client = OpenAI(api_key=OPENAI_API_KEY)

# === Google Sheets helpers ===
HEADERS = ["fecha","hora","valor","comercio","categoria","subcategoria","detalle", "cuenta"]

# --- Soporte para credencial desde variable de entorno ---
def ensure_sa_file():
    sa_json_env = os.getenv("SERVICE_ACCOUNT_JSON")
    if sa_json_env:
        try:
            if (not os.path.exists(SA_JSON_PATH)) or os.path.getsize(SA_JSON_PATH) == 0:
                with open(SA_JSON_PATH, "w", encoding="utf-8") as f:
                    f.write(sa_json_env)
        except Exception as e:
            print("No pude escribir service_account.json desde SERVICE_ACCOUNT_JSON:", e)

ensure_sa_file()

def gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SA_JSON_PATH, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_sheet():
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    ws = sh.sheet1
    first_row = ws.row_values(1)
    if [h.lower() for h in first_row] != HEADERS:
        ws.clear()
        ws.append_row(HEADERS)
    return ws

async def add_to_notion(rec):    
    fecha = datetime.strptime(f"{rec["fecha"]} {rec["hora"]}", "%Y-%m-%d %H:%M")

    page_data = generate_page(
        detalle=rec["detalle"],
        categoria=rec["categoria"],
        subcategoria=rec["subcategoria"],
        valor=rec["valor"],
        comercio=rec["comercio"],
        cuenta=rec["cuenta"].lower(),
        fecha=fecha.isoformat()
    )

    db_id = await get_database_id(str(fecha.year))#id_gastos

    await add_new_page(db_id[0], page_data)
    

# === Utilidades de validación de fecha/hora ===
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RX = re.compile(r"^[0-2]\d:[0-5]\d$")  # 00:00–29:59 (luego verificamos rango real)

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
        "{'fecha','hora','valor','comercio','categoria','subcategoria','detalle', 'cuenta'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- 'comercio' es comercio/lugar/tienda/app si se menciona. "
        "- 'categoria/subcategoria' concisas ('comida/almuerzo', 'transporte/taxi', etc.). "
        "- 'detalle' es descripción breve. "
        "- 'cuenta' es el nombre de la cuenta donde salio el dinero posibles opciones son colpatria, nu, rappi card, nequi, rappi cuenta"
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

    # valor -> entero
    val = rec.get("valor")
    if isinstance(val, str):
        v = re.sub(r"[^\d,\.]", "", val)
        v = v.replace(".", "").replace(",", ".")
        try:
            val = int(round(float(v)))
        except Exception:
            val = ""
    rec["valor"] = val

    # fecha/hora
    fecha = (rec.get("fecha") or "").strip()
    hora  = (rec.get("hora") or "").strip()
    if not is_valid_date(fecha):
        fecha = now.date().isoformat()
    if not is_valid_time(hora):
        hora = now.strftime("%H:%M")
    rec["fecha"] = fecha
    rec["hora"]  = hora

    # strings seguros
    for k in ["comercio","categoria","subcategoria","detalle"]:
        rec[k] = (rec.get(k,"") or "").strip()

    # asegurar todas las claves
    for k in HEADERS:
        rec.setdefault(k, "")

    return rec

# === Reglas de negocio personalizadas ===
def enforce_business_rules(rec):
    """
    Regla solicitada:
    - Si categoria es 'alimentación'/'alimentacion'/'comida' y la hora está entre 18:00 y 02:00,
      entonces subcategoria = 'cena' (forzado).
    """
    cat = (rec.get("categoria") or "").strip().lower()
    hora = (rec.get("hora") or "00:00").strip()

    try:
        hh = int(hora.split(":")[0])
    except Exception:
        hh = -1  # fuerza a no coincidir si hora inválida, aunque normalmente ya está normalizada

    if cat in ("alimentación", "alimentacion", "comida"):
        # Ventana 18:00–23:59 o 00:00–01:59 (cruza medianoche)
        if (hh >= 18) or (0 <= hh < 2):
            rec["subcategoria"] = "cena"

    return rec

def persist_to_gsheets(rec):
    ws = get_or_create_sheet()
    row = [rec.get(k,"") for k in HEADERS]
    ws.append_row(row, value_input_option="USER_ENTERED")

# === Helpers de validación obligatoria ===
def has_required_description(rec) -> bool:
    return any(rec.get(k) for k in ("categoria", "subcategoria", "detalle"))

# === Telegram Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Soy tu bot de gastos y finanzas.\n"
        "-Para agregar gasto obligatorio: 💰 valor, 📝 descripción (categoría/subcategoría/detalle) y 🏦 cuenta.\n"
        "Ejemplos: 'Uber 7.820 a la oficina, colpatria', 'Nendoroid 200000 en Amazon japon, nu'\n-----------------\n"
        "Guardaré todo en tu Google Sheets 'gastos_diarios' y en Notion.\n"
        "-Para agregar un deudor: incluye la palabra DEUDOR. Ejemplo: 'Deudor luis netflix julio 15000'.\n-----------------\n"
        "-Para agregar un abono de deudor: usar /deudores para saber los que hay y luego pasa la misma descripcion y usa la palabra ABONO.\n-----------------\n"
        "Ejemplo: 'abono luis netflix julio 15000'.\n"
        "-Para agregar una deuda: incluye la palabra DEUDA. Ejemplo: 'Deuda novaventa 18.000'.\n-----------------\n"
        "-Para agregar un pago a deuda: usar /deudas para saber los que hay y luego pasa la misma descripcion y usa la palabra PAGO.\n-----------------\n"
        "Ejemplo: 'pago novaventa 15000'.\n"
    )

async def deudores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_id = await get_database_id(year)
    deudores_list = await get_deudores(db_id[2])
    await update.message.reply_text(deudores_list)
    
async def deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_id = await get_database_id(year)
    deudores_list = await get_deudores(db_id[1])
    await update.message.reply_text(deudores_list)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        rec = call_gpt_extract(text)
        if not rec:
            await update.message.reply_text("😅 No pude entender el gasto. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
            return

        rec = normalize_record(rec)

        # Validación obligatoria
        if not rec["valor"]:
            await update.message.reply_text("💰 Me falta el valor del gasto. Enviame el monto (ej: 25000 o 28.500).")
            return
        if not has_required_description(rec):
            await update.message.reply_text("📝 Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.")
            return
        if not rec["cuenta"]:
            await update.message.reply_text("🏦 Me falta la cuenta de donde salió el dinero. Por favor indícala (ej: colpatria, nu, rappi card, nequi, rappi cuenta).")
            return

        # Reglas de negocio
        rec = enforce_business_rules(rec)

        # Guardar
        persist_to_gsheets(rec)
        await add_to_notion(rec)

        await update.message.reply_text(
            f"✅ Guardado: {rec['categoria']} / {rec['subcategoria']} | ${rec['valor']} | {rec['fecha']} {rec['hora']}"
            + (f" | {rec['comercio']}" if rec.get('comercio') else "")
            + (f" | {rec['cuenta']}" if rec.get('cuenta') else "")
        )

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# --- Servidor Flask “dummy” ---
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render la crea automáticamente
PORT = int(os.environ.get("PORT", 8080))

app = Flask(__name__)
bot_app = None  # se inicializa luego

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Bot activo y listo en Render! 🟢")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Recibí: {update.message.text}")

@app.route("/")
def home():
    return "Bot de Telegram activo en Render 🟢"

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def receive_update():
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    asyncio.run(bot_app.process_update(update))
    return "ok"

async def setup_webhook():
    webhook_url = f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
    await bot_app.bot.set_webhook(url=webhook_url)
    print(f"Webhook configurado en {webhook_url}")

def main():
    global bot_app
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    asyncio.run(setup_webhook())
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()