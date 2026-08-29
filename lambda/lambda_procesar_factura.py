import json
import re
import boto3
import pg8000.native
from datetime import date

textract = boto3.client('textract')

# Cliente de SSM Parameter Store -- se crea una sola vez, a nivel de modulo,
# para que se reutilice entre invocaciones "calientes" de la Lambda.
ssm = boto3.client('ssm', region_name='us-east-1')

# --- Valores especificos de esta instalacion -------------------------------
# Reemplaza este valor con el endpoint real de tu instancia RDS antes de
# desplegar. No es un secreto por si solo (RDS esta en una subred privada
# sin acceso publico), pero es un identificador propio de una cuenta real y
# no se deja como valor fijo en un repositorio publico.
DB_HOST = '<TU-ENDPOINT-RDS>.rds.amazonaws.com'  # Consola RDS > Bases de datos > Punto de enlace (endpoint)
# -----------------------------------------------------------------------

DB_PORT = 5432
DB_NAME = 'facturas'
DB_USER = 'postgres'

# La contraseña no esta escrita aqui: se consulta a SSM Parameter Store una
# sola vez, cuando arranca una instancia "fria" de la Lambda. WithDecryption
# le dice a SSM que use la llave KMS asociada para devolver el valor real.
# El parametro '/facturas-app/rds-password' debe existir en tu cuenta como
# SecureString antes de invocar esta funcion.
DB_PASSWORD = ssm.get_parameter(
    Name='/facturas-app/rds-password',
    WithDecryption=True
)['Parameter']['Value']

# Nombres y abreviaturas de mes reconocidas, en minuscula y sin tildes,
# en español e ingles (cubre los dos idiomas vistos en pruebas reales)
MESES = {
    'enero': 1, 'ene': 1,
    'febrero': 2, 'feb': 2,
    'marzo': 3, 'mar': 3,
    'abril': 4, 'abr': 4,
    'mayo': 5, 'may': 5,
    'junio': 6, 'jun': 6,
    'julio': 7, 'jul': 7,
    'agosto': 8, 'ago': 8,
    'septiembre': 9, 'setiembre': 9, 'sep': 9, 'set': 9,
    'octubre': 10, 'oct': 10,
    'noviembre': 11, 'nov': 11,
    'diciembre': 12, 'dic': 12,
    'january': 1, 'jan': 1,
    'february': 2,
    'march': 3,
    'april': 4, 'apr': 4,
    'june': 6,
    'july': 7,
    'august': 8, 'aug': 8,
    'september': 9, 'sept': 9,
    'october': 10,
    'november': 11,
    'december': 12, 'dec': 12,
}

# Recorre los campos generales de la factura (proveedor, fecha, total, etc.)
# que Textract detecta a nivel de documento completo, y arma un diccionario
# tipo -> valor de texto. Si un mismo tipo de campo aparece mas de una vez,
# se conserva solo la primera aparicion.
def extraer_campos_generales(response):
    campos = {}
    documento = response['ExpenseDocuments'][0]
    for field in documento['SummaryFields']:
        tipo = field['Type']['Text']
        valor = field.get('ValueDetection', {}).get('Text', '')
        if tipo not in campos:
            campos[tipo] = valor
    return campos

# Recorre los grupos de items de linea (line items) que Textract detecto en
# la factura -- por ejemplo cada producto o servicio facturado -- y arma una
# lista de diccionarios, uno por item, con sus campos (descripcion,
# cantidad, precio unitario, subtotal, etc.) tal como Textract los leyo.
def extraer_items(response):
    items = []
    documento = response['ExpenseDocuments'][0]
    for grupo in documento.get('LineItemGroups', []):
        for linea in grupo['LineItems']:
            item = {}
            for field in linea['LineItemExpenseFields']:
                tipo = field['Type']['Text']
                valor = field.get('ValueDetection', {}).get('Text', '')
                item[tipo] = valor
            items.append(item)
    return items

# Extrae el primer numero valido dentro de un texto como '$10.00',
# '500 units' o '199.65 €', ignorando simbolos de moneda, espacios o
# palabras alrededor. Si no encuentra ningun numero, devuelve None en vez
# de fallar. (Corregido: la version original solo quitaba el simbolo de
# euro y no reconocia palabras pegadas al numero -- ver seccion 5.42 de la
# documentacion del proyecto.)
def limpiar_numero(texto):
    if not texto:
        return None
    coincidencia = re.search(r'\d[\d.,]*', texto)
    if not coincidencia:
        return None
    numero_bruto = coincidencia.group()
    # Quita las comas (separador de miles en formato US/es-CO: "$5,000.00")
    limpio = numero_bruto.replace(',', '')
    try:
        return float(limpio)
    except ValueError:
        return None

# Convierte vocales acentuadas a su version simple (ej. 'á' -> 'a'), para
# que un mes escrito con tilde no deje de reconocerse.
def _quitar_tildes(texto):
    tabla = str.maketrans('áéíóúÁÉÍÓÚ', 'aeiouAEIOU')
    return texto.translate(tabla)

# Convierte el texto de fecha libre que devuelve Textract a un objeto date
# de Python. Maneja dos formatos, ambos vistos en pruebas reales:
#   1. Numerico con separador:      '15.06.2021', '29/01/2019'
#   2. Con el mes escrito en letras: '05/may/2022', 'September 15,2028'
# Devuelve None si no logra interpretarlo (campo no detectado, o formato
# no reconocido), en vez de fallar -- mismo criterio de limpiar_numero().
def parsear_fecha(texto):
    if not texto:
        return None

    texto_normalizado = _quitar_tildes(texto).lower()

    # Busca si alguna palabra del texto es un mes conocido
    mes_encontrado = None
    for palabra in re.findall(r'[a-z]+', texto_normalizado):
        if palabra in MESES:
            mes_encontrado = MESES[palabra]
            break

    numeros = re.findall(r'\d+', texto)

    if mes_encontrado is not None:
        # Con el mes ya identificado por nombre, el anio es el numero de
        # 4 digitos, y el dia es el otro numero
        if len(numeros) < 2:
            return None
        anio = next((n for n in numeros if len(n) == 4), None)
        dia = next((n for n in numeros if len(n) != 4), None)
        if anio is None or dia is None:
            return None
        try:
            return date(int(anio), mes_encontrado, int(dia))
        except ValueError:
            return None

    # Sin mes en palabras: metodo numerico puro, igual que antes
    if len(numeros) != 3:
        return None

    a, b, c = numeros
    if len(a) == 4:
        anio, resto1, resto2 = a, b, c
    elif len(c) == 4:
        anio, resto1, resto2 = c, a, b
    else:
        return None

    dia, mes = int(resto1), int(resto2)
    if mes > 12 and dia <= 12:
        dia, mes = mes, dia

    try:
        return date(int(anio), mes, dia)
    except ValueError:
        return None

# Se conecta a RDS, inserta una fila en 'factura' con los datos generales,
# y una fila en 'item_factura' por cada item detectado, enlazadas mediante
# el factura_id que genera la base de datos automaticamente.
def guardar_en_rds(campos, items, s3_key):
    conexion = pg8000.native.Connection(
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME
    )

    try:
        resultado = conexion.run(
            """
            INSERT INTO factura (proveedor_nombre, fecha_factura, total, s3_key, datos_textract_raw)
            VALUES (:proveedor, :fecha, :total, :s3_key, :raw)
            RETURNING factura_id
            """,
            proveedor=campos.get('VENDOR_NAME'),
            fecha=parsear_fecha(campos.get('INVOICE_RECEIPT_DATE')),
            total=limpiar_numero(campos.get('TOTAL')),
            s3_key=s3_key,
            raw=json.dumps(campos)
        )
        factura_id = resultado[0][0]

        for item in items:
            conexion.run(
                """
                INSERT INTO item_factura (factura_id, descripcion, cantidad, precio_unitario, subtotal)
                VALUES (:factura_id, :descripcion, :cantidad, :precio, :subtotal)
                """,
                factura_id=factura_id,
                descripcion=item.get('ITEM'),
                cantidad=limpiar_numero(item.get('QUANTITY')),
                precio=limpiar_numero(item.get('UNIT_PRICE')),
                subtotal=limpiar_numero(item.get('PRICE'))
            )

        print(f"Factura guardada en RDS con factura_id: {factura_id}")
        return factura_id
    finally:
        conexion.close()

# Punto de entrada de la Lambda: se dispara automaticamente cuando llega un
# archivo nuevo al bucket S3 (evento s3:ObjectCreated en la carpeta entrada/).
def lambda_handler(event, context):
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']

    print(f"Procesando archivo: {key} del bucket: {bucket}")

    # Le pide a Textract que analice la imagen como un documento de gasto
    # (factura/recibo), extrayendo campos generales e items de linea.
    response = textract.analyze_expense(
        Document={'S3Object': {'Bucket': bucket, 'Name': key}}
    )

    campos = extraer_campos_generales(response)
    items = extraer_items(response)

    print("===== RESUMEN DE LA FACTURA =====")
    print(f"Proveedor: {campos.get('VENDOR_NAME', 'No detectado')}")
    print(f"Fecha detectada: {campos.get('INVOICE_RECEIPT_DATE', 'No detectada')} -> {parsear_fecha(campos.get('INVOICE_RECEIPT_DATE'))}")
    print(f"Total: {campos.get('TOTAL', 'No detectado')}")
    print(f"Items detectados: {len(items)}")
    print("==================================")

    factura_id = guardar_en_rds(campos, items, key)

    return {
        'statusCode': 200,
        'body': json.dumps(f'Factura procesada y guardada con ID {factura_id}')
    }
