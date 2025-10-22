from notion_client import AsyncClient
import os
from dotenv import load_dotenv

load_dotenv()
notion = AsyncClient(auth=os.getenv("NOTION_TOKEN"))
finances_db_id = os.getenv("FINANCES_PAGE_TABLE")

def generate_page(detalle, categoria, subcategoria, valor, comercio, cuenta, fecha):
  page = {
      "Detalle":{
        "id":"title",
        "type":"title",
        "title":[
          {
              "text":{
                "content":detalle
              }
          }
        ]
    },
    "Cuenta":{
        "type":"select",
        "select":{
          "name": cuenta,
        }
    },
    "Categoria":{
        "type":"rich_text",
        "rich_text":[
          {
              "text":{
                "content":categoria,
              }
          }        
      ]
    },
    "Valor":{
        "type":"number",
        "number":valor
    },
    "Date":{
        "type":"date",
        "date":{
          "start":fecha,
        }
    },
    "Comercio":{
        "type":"rich_text",
        "rich_text":[
          {
              "text":{
                "content":comercio,
              },
          }]
    },
    "Subcategoria":{
        "id":"rgbW",
        "type":"rich_text",
        "rich_text":[
          {
              "text":{
                "content":subcategoria,
              }
          }
        ]
    },
    
  }
  return page

def add_new_page(dbId, page):
  query = {
      "parent": {
        "database_id": dbId,
      },
      "properties": page
  }
  return notion.pages.create(**query)

async def get_database_id(year):
  res = await notion.databases.query(database_id=finances_db_id, filter={
    "property": "Year",
    "title": {
        "equals": year
    }
  })
  res_properties = res['results'][0]['properties']
  id_gastos =res_properties['id_gastos']['rich_text'][0]['text']['content']
  id_deudas =res_properties['id_deudas']['rich_text'][0]['text']['content']
  id_deudores=res_properties['id_deudores']['rich_text'][0]['text']['content']
  return [id_gastos, id_deudas, id_deudores]

def map_deudores(deudores):
  text = ""
  for deudor in deudores:
    title = deudor['properties']['Detalle']['title'][0]['text']['content']
    total = deudor['properties']['total']['number']
    pagado = deudor['properties']['pagado']['number']
    restante = deudor['properties']['restante']['formula']['number']    
    text +=  f"Detalle: {title} Total: {total} Pagado: {pagado} Restante: {restante}\n-----------------\n"
  return text  

async def get_deudores(db_id):
  res = await notion.databases.query(database_id=db_id, filter={
    "property": "restante",
    "number": {
        "does_not_equal": 0
    }
    })
  text = map_deudores(res['results'])
  return text

