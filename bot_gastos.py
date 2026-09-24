import asyncio
import os, json, re, datetime as dt
import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from notion import actualizar_deudor_deuda, add_new_page, generate_deudor, generate_extra_allowance, get_data_source_id, get_database_id, generate_page, get_deudor_deuda, get_deudores, get_extra_allowances_month, get_month_expences, map_expences, sum_valor_data
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
HEADERS = ["fecha","hora","valor","categoria","detalle", "cuenta"]
HEADERS_ABONO = ['fecha',"hora","detalle","valor","pagado","restante"]
HEADERS_EXTRA = ["detalle","valor","fecha","hora"]

chatgpt_context =( "Eres un extractor estricto de gastos personales en Colombia. "
        "Devuelves SOLO JSON con estas claves exactas: "
        "{'fecha','valor','categoria','detalle', 'cuenta'}. "
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        f"- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío, el usuario puede pasar la fecha como en muchos formatos toma esa fecha y retorna dia, mes, año separado por guion, si no pasa año usa {year}, si no pasa fecha deja fecha vacía."
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "
        "- 'categoria' hay 5 categorias unicas: hobbies (todo lo relacionado con figuras, lego, funkos, nendoroid, videojuegos, amiibos, comics, manga,cosas de ese estilo), comida (restaurantes, supermercados, domicilios, etc), obligaciones (servicios, suscripciones, impuestos y pagos de empresas de credito como sistecredito, addi, credifin y mas almacenes y cosas medicas, cosas de belleza no va en esta categoria), compras personales (todas las compras que no sean en las otras categorias, no incluye cosas como servicios o no cosas de belleza como uñas y cejas) y otros (lo que no encaje en las demas)"
        "- 'detalle' es descripción breve, puede ser solo una palabra o multiples palabras puede ser incluso solo en nombre del comercio como Amazon, Temu, steam. "
        "- 'cuenta' es el nombre de la cuenta donde salio el dinero posibles opciones son colpatria, nu, rappi card, nequi, rappi cuenta, etc."
        "- No incluyas explicaciones ni comentarios, solo el JSON.")

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

def get_or_create_sheet(tipo):
    print(f"[DEBUG] Conectando a Google Sheets: {SHEET_NAME}")
    gc = gspread_client()
    sh = gc.open(SHEET_NAME)
    if tipo == "abono" or tipo == "deudor":
        ws = sh.worksheet("deudores")
    elif tipo == "dinero":
        ws = sh.worksheet("extra")
    elif  tipo == "pago" or  tipo == "deuda":
        ws = sh.worksheet("deudas")
    elif  tipo == "mes":
        ws = sh.worksheet("mes")        
    else:
        ws = sh.worksheet("gastos")  # Hoja por defecto
    first_row = ws.row_values(1)
    print(f"[DEBUG] Primera fila de la hoja: {first_row}")
    return ws

def update_sheet_row(tipo, update_data):
    ws = get_or_create_sheet(tipo)
    data = ws.get_all_records()
    for row_number, row in enumerate(data, start=2):
        if row["detalle"] == update_data["detalle"]:      
            print(row)
            pagado =row["pagado"] or 0 
            nuevo_pagado = pagado + update_data["valor"]
            ws.update_cell(row_number, 5, nuevo_pagado)
            ws.update_cell(row_number, 6, row["valor"] - nuevo_pagado)  
            break

def persist_to_gsheets(rec, tipo):
    print(f"[DEBUG] Conectando a Google Sheets para guardar: {rec}")
    ws = get_or_create_sheet(tipo)#modificar para que guarde en la hoja correcta segun el tipo
    if tipo == "abono" or tipo == "pago" or tipo == "deudor" or tipo == "deuda":
        row = [rec.get(k,"") for k in HEADERS_ABONO]
    elif tipo == "dinero":
        row = [rec.get(k,"") for k in HEADERS_EXTRA]
    else:
        row = [rec.get(k,"") for k in HEADERS]
    print(f"[DEBUG] Fila a insertar: {row}")
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[DEBUG] Fila insertada exitosamente")

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

def format_number_with_decimals(number):
  """Formats a number to have a decimal point and thousand separators.
  Example: 100000 -> '100,000.00'
           1234567.89 -> '1,234,567.89'
  """
  # Use f-string formatting with ',' for thousand separator and '.2f' for two decimal places
  return f"{number:,}"

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
    system_prompt = chatgpt_context
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
        "{'detalle','valor','tipo', 'fecha'}."
        "Reglas: "
        "- JSON válido, sin texto adicional. "
        f"- NO infieras fecha ni hora: si el usuario no las menciona explícitamente, deja \"fecha\" y/o \"hora\" como string vacío, el usuario puede pasar la fecha como en muchos formatos toma esa fecha y retorna dia, mes, año separado por guion, si no pasa año usa {year}, si no pasa fecha deja fecha vacía."
        "- Moneda por defecto COP; normaliza '28.500' → 28500 (entero). "        
        "- 'tipo' es el tipo de transaccion puede ser 'deuda', 'deudor', 'pago', 'dinero' o 'abono' y debe estar al principio del texto, en caso de no estar pon, solo 'gasto' sin nada extra'"
        "- 'valor' es un numero referente a pesos colombianos "
        "- 'detalle' es description breve."
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
def normalize_record(rec, tipo):
    print(f"[DEBUG] Normalizando registro inicial: {rec}")
    now = dt.datetime.now(TZ)
    headers = HEADERS_ABONO if tipo in ["abono", "deudor", "deuda", "pago"] else HEADERS_EXTRA if tipo == "dinero" else HEADERS
    print(headers)
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

    for k in headers:
        rec.setdefault(k, "")

    print(f"[DEBUG] Registro después de normalización: {rec}")
    return rec

# === Helpers de validación obligatoria ===
def has_required_description(rec) -> bool:
    return any(rec.get(k) for k in ("categoria", "detalle"))

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
        "-Para agregar un dinero extra: usar **-dinero** al principio del mensaje luego pasar de que es y el valor.\n"        
        "-Para mirar cuanto se ha gastado en el mes: usar /balance.\n"
        "-Para mirar todos los gastos del mes: usar /gastos.\n"
    )
    print(f"[DEBUG] Mensaje de inicio enviado")

async def deudores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /deudores ejecutado")
    ws = get_or_create_sheet('deudor')
    data = ws.get_all_records()
    text =""
    for row_number, row in enumerate(data, start=2):
        text +=  f"Detalle: {row['detalle']} Total: {format_number_with_decimals(row['valor'])} Pagado: {format_number_with_decimals(row['pagado'])} Restante: {format_number_with_decimals(row['restante'])}\n-----------------\n"     
    await update.message.reply_text(text if text else "No se encontraron entradas")
    
async def deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /deudas ejecutado")
    ws = get_or_create_sheet('deuda')
    data = ws.get_all_records()
    text =""
    for row_number, row in enumerate(data, start=2):
        text +=  f"Detalle: {row['detalle']} Total: {format_number_with_decimals(row['valor'])} Pagado: {format_number_with_decimals(row['pagado'])} Restante: {format_number_with_decimals(row['restante'])}\n-----------------\n"     
    await update.message.reply_text(text if text else "No se encontraron entradas")

async def month_valance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valor=4500000
    print(f"[DEBUG] Comando /deudas ejecutado")
    ws = get_or_create_sheet('dinero')
    ws2 = get_or_create_sheet('mes')
    total_extra = int(ws.acell('E2').value) or 0
    total = int(ws2.acell('G2').value) or 0
    print(f"[DEBUG] Total extra obtenido: {total_extra}")
    total_disponible=valor+total_extra
    print(f"[DEBUG] Valance obtenido: {total}")
    print(f"[DEBUG] Total disponible obtenido: {total_disponible}")
    await update.message.reply_text(f"Gastos del mes: {format_number_with_decimals(total)}\n-----------------\n{format_number_with_decimals(total_disponible-total)} disponible")

async def month_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[DEBUG] Comando /deudas ejecutado")
    ws = get_or_create_sheet('mes')
    data = ws.get_all_records()
    text =""
    for row_number, row in enumerate(data, start=2):
        text +=  f"Detalle: {row['detalle']} Valor: {row['valor']} Fecha: {row['fecha']}\n-----------------\n"     
    await update.message.reply_text(text if text else "No se encontraron entradas")
    print(f"[DEBUG] Gastos del mes obtenidos")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    print(f"[DEBUG] Mensaje recibido: {text}")
    res = call_gpt_deuda_deudor(text)
    print(f"[DEBUG] Respuesta de GPT (deuda/deudor): {res}")

    if(res is None):
        print(f"[DEBUG] No se pudo parsear la respuesta de GPT")
        await update.message.reply_text("😅 No pude entender tu peticion, lee de nuevo las instrucciones")
    else:
        tipo = res['tipo'].lower()
        print(f"[DEBUG] Tipo detectado: {tipo}")
        try:
            if(tipo == "gasto"):
                print(f"[DEBUG] Llamando GPT para extraer detalles del gasto...")
                rec = call_gpt_extract(text)
            else:
                rec = res
            print(f"[DEBUG] Respuesta de GPT ({tipo}): {rec}")
            if not rec:
                print(f"[DEBUG] No se pudo parsear el {tipo}")
                await update.message.reply_text("😅 No pude entender el gasto. Decime el monto y una descripción corta (ej: 'comida almuerzo 28000').")
                return

            print(f"[DEBUG] Normalizando registro...")
            rec = normalize_record(rec, tipo)
            print(f"[DEBUG] Registro normalizado: {rec}")

            # Validación obligatoria
            if not rec["valor"]:
                print(f"[DEBUG] Validación fallida: falta valor")
                await update.message.reply_text("💰 Me falta el valor del {tipo}. Enviame el monto (ej: 25000 o 28.500).")
                return
            if not has_required_description(rec):
                print(f"[DEBUG] Validación fallida: falta descripción")
                await update.message.reply_text("📝 Necesito una descripción/categoría. Decime algo como: 'comida/almuerzo', 'transporte/taxi' o un detalle corto.")
                return
            if tipo=="gasto" and not rec["cuenta"]:
                await update.message.reply_text("🏦 Me falta la cuenta de donde salió el dinero. Por favor indícala (ej: colpatria, nu, rappi card, nequi, rappi cuenta).")
                return

            print(f"[DEBUG] Todas las validaciones pasaron")

            # Guardar
            if(tipo == "abono" or tipo == "pago"):
                print(f"[DEBUG] Actualizando en Google Sheets...")
                update_sheet_row(tipo,rec)
                print(f"[DEBUG] Actualizado en Sheets exitosamente")
            else:
                print(f"[DEBUG] Guardando en Google Sheets...")
                persist_to_gsheets(rec, tipo)
                print(f"[DEBUG] Guardado en Sheets exitosamente")
            
            if tipo == "gasto":
                await update.message.reply_text(
                    f"✅ Guardado: {rec['categoria']} | ${format_number_with_decimals(int(rec['valor']))} | {rec['fecha']} {rec['hora']}"
                    + (f" | {rec['comercio']}" if rec.get('comercio') else "")
                    + (f" | {rec['cuenta']}" if rec.get('cuenta') else "")
                )
            else:
                await update.message.reply_text(f"{tipo.upper()} {rec['detalle']} {format_number_with_decimals(int(rec['valor']))} registrado correctamente.")



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
    app.add_handler(CommandHandler("balance", month_valance))
    app.add_handler(CommandHandler("gastos", month_expenses))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("[DEBUG] Handlers registrados. Iniciando polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
