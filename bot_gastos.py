import asyncio
import os, json, re, datetime as dt
import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from notion import actualizar_deudor_deuda, add_new_page, generate_deudor, get_data_source_id, get_database_id, generate_page, get_deudor_deuda, get_deudores
from datetime import datetime
from threading import Thread
from flask import Flask

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
    print(f"[DEBUG] Verificando archivo service_account.json...")
    sa_json_env = os.getenv("SERVICE_ACCOUNT_JSON")
    if sa_json_env:
        try:
            if (not os.path.exists(SA_JSON_PATH)) or os.path.getsize(SA_JSON_PATH) == 0:
                print(f"[DEBUG] Creando archivo service_account.json desde variable de entorno...")
                with open(SA_JSON_PATH, "w", encoding="utf-8") as f:
                    f.write(sa_json_env)
                print(f"[DEBUG] Archivo creado exitosamente")
        except Exception as e:
            print("[DEBUG] No pude escribir service_account.json desde SERVICE_ACCOUNT_JSON:", e)

ensure_sa_file()

def gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SA_JSON_PATH, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_sheet():
    print(f"[DEBUG] Conectando a Google Sheets: {SHEET_NAME}")
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    ws = sh.sheet1
    first_row = ws.row_values(1)
    print(f"[DEBUG] Primera fila de la hoja: {first_row}")
    if [h.lower() for h in first_row] != HEADERS:
        print(f"[DEBUG] Headers no coinciden, limpiando y estableciendo nuevos...")
        ws.clear()
        ws.append_row(HEADERS)
        print(f"[DEBUG] Headers establecidos correctamente")
    return ws

async def add_to_notion(rec):
    print(f"[DEBUG] Preparando registro para Notion: {rec}")
    fecha = datetime.strptime(f"{rec['fecha']} {rec['hora']}", "%Y-%m-%d %H:%M")
    print(f"[DEBUG] Fecha parseada para Notion: {fecha}")

    page_data = generate_page(
        detalle=rec["detalle"],
        categoria=rec["categoria"],
        subcategoria='',
        valor=rec["valor"],
        comercio=rec["comercio"],
        cuenta=rec["cuenta"].lower() if rec["cuenta"] else "", 
        fecha=fecha.isoformat()
    )
    print(f"[DEBUG] Datos de página generados para Notion")

    print(f"[DEBUG] Obteniendo ID de base de datos para año {fecha.year}...")
    db_id = await get_database_id(str(fecha.year))#id_gastos
    print(f"[DEBUG] DB ID obtenido: {db_id}")

    print(f"[DEBUG] Agregando página a Notion...")
    await add_new_page(db_id[0], page_data)
    print(f"[DEBUG] Página agregada a Notion exitosamente")
    

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
    print(f"[DEBUG] Llamando GPT para extraer gasto de: {msg_text}")
    system_prompt = (
        "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON con estas claves exactas: "
        "{'fecha','hora','valor','comercio','categoria','detalle', 'cuenta'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- 'comercio' es comercio/lugar/tienda/app si se menciona. "
        "- 'categoria' concisas ('comida', 'transporte', 'videojuego', 'figuras', etc.). "
        "- 'detalle' es descripción breve, puede ser solo una palabra o multiples palabras puede ser incluso solo en nombre del comercio como Amazon, Temu, steam. "
        "- 'cuenta' es el nombre de la cuenta donde salio el dinero posibles opciones son colpatria, nu, rappi card, nequi, rappi cuenta, etc."
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
    print(f"[DEBUG] Respuesta de GPT sin parsear: {txt}")
    result = parse_json_strict(txt)
    print(f"[DEBUG] JSON parseado: {result}")
    return result

def call_gpt_deuda_deudor(msg_text):
    print(f"[DEBUG] Llamando GPT para clasificar: {msg_text}")
    system_prompt = (
        "Eres un extractor estricto de finanzas personales en Colombia. "
        "Devuelves SOLO JSON con estas claves exactas: "
        "{'detalle','valor','tipo'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        "- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío. "
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- 'valor' es un numero referente a pesos colombianos "
        "- 'detalle' es description breve. "
        "- 'tipo' es el tipo de transaccion puede ser '-deuda', '-deudor', '-pago' o '-abono' y debe estar al principio del texto, en caso de no estar pon, solo 'gasto' sin nada extra'"
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
    print(f"[DEBUG] Respuesta de GPT sin parsear: {txt}")
    result = parse_json_strict(txt)
    print(f"[DEBUG] JSON parseado: {result}")
    return result

# === Normalización: fecha/hora vacías o inválidas -> ahora; valor -> entero COP ===
def normalize_record(rec):
    print(f"[DEBUG] Normalizando registro inicial: {rec}")
    now = dt.datetime.now(TZ)

    # valor -> entero
    val = rec.get("valor")
    if isinstance(val, str):
        print(f"[DEBUG] Normalizando valor (string): {val}")
        v = re.sub(r"[^\d,\.]", "", val)
        v = v.replace(".", "").replace(",", ".")
        try:
            val = int(round(float(v)))
            print(f"[DEBUG] Valor normalizado: {val}")
        except Exception:
            print(f"[DEBUG] Error normalizando valor, dejando vacío")
            val = ""
    rec["valor"] = val

    # fecha/hora
    fecha = (rec.get("fecha") or "").strip()
    hora  = (rec.get("hora") or "").strip()
    if not is_valid_date(fecha):
        print(f"[DEBUG] Fecha inválida o vacía, usando fecha actual")
        fecha = now.date().isoformat()
    if not is_valid_time(hora):
        print(f"[DEBUG] Hora inválida o vacía, usando hora actual")
        hora = now.strftime("%H:%M")
    rec["fecha"] = fecha
    rec["hora"]  = hora
    print(f"[DEBUG] Fecha/hora normalizadas: {fecha} {hora}")

    # strings seguros
    for k in ["comercio","categoria","detalle"]:
        rec[k] = (rec.get(k,"") or "").strip()

    # asegurar todas las claves
    for k in HEADERS:
        rec.setdefault(k, "")

    print(f"[DEBUG] Registro después de normalización: {rec}")
    return rec

# === Reglas de negocio personalizadas ===
def enforce_business_rules(rec):
    print(f"[DEBUG] Aplicando reglas de negocio al registro: {rec}")
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
            print(f"[DEBUG] Detectada hora de cena ({hora}). Estableciendo subcategoria a 'cena'")
            rec["subcategoria"] = "cena"

    return rec

def persist_to_gsheets(rec):
    print(f"[DEBUG] Conectando a Google Sheets para guardar: {rec}")
    ws = get_or_create_sheet()
    row = [rec.get(k,"") for k in HEADERS]
    print(f"[DEBUG] Fila a insertar: {row}")
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[DEBUG] Fila insertada exitosamente")

# === Helpers de validación obligatoria ===
def has_required_description(rec) -> bool:
    return any(rec.get(k) for k in ("categoria", "subcategoria", "detalle"))

# === Telegram Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /start ejecutado por usuario: {update.message.from_user.id}")
    await update.message.reply_text(
        "👋 Soy tu bot de gastos y finanzas.\n"
        "-Para agregar gasto obligatorio: 💰 valor, 📝 descripción (categoría/subcategoría/detalle) y 🏦 cuenta.\n"
        "Ejemplos: 'Uber 7.820 a la oficina, colpatria', 'Nendoroid 200000 en Amazon japon, nu'\n-----------------\n"
        "Guardaré todo en tu Google Sheets 'gastos_diarios' y en Notion.\n"
        "-Para agregar un deudor: incluye la palabra **DEUDOR**. Ejemplo: 'Deudor luis netflix julio 15000'.\n-----------------\n"
        "-Para agregar un abono de deudor: usar /deudores para saber los que hay y luego pasa la misma descripcion y usa la palabra **ABONO**.\n"
        "Ejemplo: 'abono luis netflix julio 15000'.\n-----------------\n"
        "-Para agregar una deuda: incluye la palabra **DEUDA**. Ejemplo: 'Deuda novaventa 18.000'.\n-----------------\n"
        "-Para agregar un pago a deuda: usar /deudas para saber los que hay y luego pasa la misma descripcion y usa la palabra **PAGO**.\n"
        "Ejemplo: 'pago novaventa 15000'.\n-----------------\n"
    )
    print(f"[DEBUG] Mensaje de inicio enviado")

async def deudores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /deudores ejecutado")
    db_id = await get_database_id(year)
    print(f"[DEBUG] DB ID obtenido: {db_id}")
    data_source_id = await get_data_source_id(db_id[2])
    print(f"[DEBUG] Data source ID obtenido: {data_source_id}")
    deudores_list = await get_deudores(data_source_id)
    print(f"[DEBUG] Lista de deudores obtenida: {deudores_list}")
    await update.message.reply_text(deudores_list)
    
async def deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /deudas ejecutado")
    db_id = await get_database_id(year)
    print(f"[DEBUG] DB ID obtenido: {db_id}")
    data_source_id=await get_data_source_id(db_id[1])
    print(f"[DEBUG] Data source ID obtenido: {data_source_id}")
    deudores_list = await get_deudores(data_source_id)
    print(f"[DEBUG] Lista de deudas obtenida: {deudores_list}")
    await update.message.reply_text(deudores_list)

# actualizando tablas
async def add_deudor_deuda(update: Update, tipo, detalle, total):
    print(f"[DEBUG] Creando página para {tipo}: {detalle}")
    page = generate_deudor(detalle, total)
    print(f"[DEBUG] Obteniendo ID de base de datos para año {year}...")
    db = await get_database_id(year)
    db_id = db[2] if tipo=="-deudor" else db[1]
    print(f"[DEBUG] DB ID obtenido: {db_id}")
    print(f"[DEBUG] Agregando página a Notion...")
    await add_new_page(db_id, page)
    print(f"[DEBUG] {tipo.capitalize()} registrado en Notion")
    await update.message.reply_text(f"{tipo.capitalize()} {detalle} {total} registrado correctamente.")

async def add_abono_pago(update: Update,tipo,detalle, pago):
    print(f"[DEBUG] Procesando {tipo} para {detalle}")
    db = await get_database_id(year)
    db_id = db[2] if tipo=="-abono" else db[1]
    print(f"[DEBUG] DB ID obtenido: {db_id}")
    data_source_id = await get_data_source_id(db_id)
    print(f"[DEBUG] Data source ID obtenido: {data_source_id}")
    await actualizar_deudor_deuda(data_source_id, detalle, pago)
    print(f"[DEBUG] {tipo.capitalize()} actualizado en Notion")
    await update.message.reply_text(f"{tipo.capitalize()} {detalle} {pago} registrada correctamente.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    print(f"[DEBUG] Mensaje recibido: {text}")
    res = call_gpt_deuda_deudor(text)
    print(f"[DEBUG] Respuesta de GPT (deuda/deudor): {res}")
    if(res is None):
        print(f"[DEBUG] No se pudo parsear la respuesta de GPT")
        await update.message.reply_text("😅 No pude entender tu peticion, lee de nuevo las instrucciones")
    elif((res['tipo'].lower() == "-deudor" )or (res['tipo'].lower() == "-deuda" )):
        print(f"[DEBUG] Tipo detectado: {res['tipo']}")
        if(not res['valor']):
            print(f"[DEBUG] Falta valor en deuda/deudor")
            await update.message.reply_text("💰 Me falta el valor de la deuda/deudor. Enviame el monto (ej: 25000 o 28.500)")
            return
        if(not res['detalle']):
            print(f"[DEBUG] Falta detalle en deuda/deudor")
            await update.message.reply_text("📝 Necesito detalle de la deuda/deudor. Decime algo como: 'luis amazon', etc.")
            return
        print(f"[DEBUG] Agregando {res['tipo']}: {res['detalle']} - {res['valor']}")
        await add_deudor_deuda(update, res['tipo'].lower(), res['detalle'], res['valor'])
    elif((res['tipo'].lower() == "-abono") or (res['tipo'].lower() == "-pago")):
        print(f"[DEBUG] Tipo detectado: {res['tipo']}")
        if(not res['valor']):
            print(f"[DEBUG] Falta valor en abono/pago")
            await update.message.reply_text("💰 Me falta el valor del pago/abbono. Enviame el monto (ej: 25000 o 28.500)")
            return
        if(not res['detalle']):
            print(f"[DEBUG] Falta detalle en abono/pago")
            await update.message.reply_text("📝 Necesito detalle de la deuda/deudor. Decime algo como: 'luis amazon', etc.")
            return
        print(f"[DEBUG] Agregando {res['tipo']}: {res['detalle']} - {res['valor']}")
        await add_abono_pago(update, res['tipo'].lower(), res['detalle'], res['valor'])
    elif res['tipo'].lower() == "gasto":
        print(f"[DEBUG] Tipo detectado: gasto")
        try:
            print(f"[DEBUG] Llamando GPT para extraer detalles del gasto...")
            rec = call_gpt_extract(text)
            print(f"[DEBUG] Respuesta de GPT (gasto): {rec}")
            if not rec:
                print(f"[DEBUG] No se pudo parsear el gasto")
                await update.message.reply_text("😅 No pude entender el gasto. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
                return

            print(f"[DEBUG] Normalizando registro...")
            rec = normalize_record(rec)
            print(f"[DEBUG] Registro normalizado: {rec}")

            # Validación obligatoria
            if not rec["valor"]:
                print(f"[DEBUG] Validación fallida: falta valor")
                await update.message.reply_text("💰 Me falta el valor del gasto. Enviame el monto (ej: 25000 o 28.500).")
                return
            if not has_required_description(rec):
                print(f"[DEBUG] Validación fallida: falta descripción")
                await update.message.reply_text("📝 Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.")
                return
            if not rec["cuenta"]:
                await update.message.reply_text("🏦 Me falta la cuenta de donde salió el dinero. Por favor indícala (ej: colpatria, nu, rappi card, nequi, rappi cuenta).")
                return

            print(f"[DEBUG] Todas las validaciones pasaron. Aplicando reglas de negocio...")
            # Reglas de negocio
            rec = enforce_business_rules(rec)
            print(f"[DEBUG] Después de aplicar reglas: {rec}")

            # Guardar
            print(f"[DEBUG] Guardando en Google Sheets...")
            persist_to_gsheets(rec)
            print(f"[DEBUG] Guardado en Sheets exitosamente")
            
            print(f"[DEBUG] Agregando a Notion...")
            await add_to_notion(rec)
            print(f"[DEBUG] Agregado a Notion exitosamente")

            await update.message.reply_text(
                f"✅ Guardado: {rec['categoria']} | ${rec['valor']} | {rec['fecha']} {rec['hora']}"
                + (f" | {rec['comercio']}" if rec.get('comercio') else "")
                + (f" | {rec['cuenta']}" if rec.get('cuenta') else "")
            )

        except Exception as e:
            print(f"[DEBUG] Error durante el procesamiento: {e}")
            import traceback
            traceback.print_exc()
            await update.message.reply_text(f"Error: {e}")


def main():
    print("[DEBUG] Iniciando bot de gastos...")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    print("[DEBUG] Bot configurado correctamente")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deudores", deudores))
    app.add_handler(CommandHandler("deudas", deudas))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("[DEBUG] Handlers registrados. Iniciando polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
