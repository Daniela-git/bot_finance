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

def generate_deudor(detalle, total):
  page = {
    "total":{
        "type":"number",
        "number":total
    },
    "pagado":{
        "type":"number",
        "number":0
    },
    "Detalle":{
        "id":"title",
        "type":"title",
        "title":[
          {
              "type":"text",
              "text":{
                "content":detalle,
              }
          }
        ]
    }
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

# use on queries
async def get_data_source_id(database_id):
  res = await notion.databases.retrieve(database_id=database_id)
  return res['data_sources'][0]['id']

async def get_database_id(year):
  id = await get_data_source_id(finances_db_id)
  res = await notion.data_sources.query(**{"data_source_id":id, "filter":{
    "property": "Year",
    "title": {
        "equals": year
    }
  }})
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

async def get_deudores(data_source_id):
  res = await notion.data_sources.query(
    **{
      "data_source_id": data_source_id,
      "filter": {
        "property": "restante",
        "number": {
            "does_not_equal": 0
        }
      }
    }
  )
  text = 'No se encontraron entradas' if len(res['results']) == 0 else map_deudores(res['results'])
  return text

async def get_deudor_deuda(data_source_id, detalle):
  return await notion.data_sources.query(**{"data_source_id":data_source_id, "filter":{
    "property": "Detalle",
    "title": {
        "equals": detalle
    }
}})

async def page_update(id, page):
  return await notion.pages.update(page_id=id, properties=page)

async def actualizar_deudor_deuda(data_source_id,detalle, pago):
  page = await get_deudor_deuda(data_source_id, detalle)
  page_id = page['results'][0]['id']
  pagado =page['results'][0]["properties"]["pagado"]["number"]
  print(f"pagado actual: {pagado}, nuevo pago: {pago}")
  update = {
      "pagado":{
          "type":"number",
          "number":int(pagado)+int(pago)
      }
  }
  await page_update(page_id, update)