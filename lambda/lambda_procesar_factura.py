import json
import re
import boto3
import pg8000.native
from datetime import date

textract = boto3.client('textract')

# Cliente de SSM Parameter Store -- se crea una sola vez, a nivel de modulo,
# para que se reutilice entre invocaciones "calientes" de la Lambda.
ssm = boto3.client('ssm', region_name='us-east-1')

# Datos de conexion a RDS
DB_HOST = '<TU-ENDPOINT-RDS>.rds.amazonaws.com'  # Consola RDS > Bases de datos > Punto de enlace (endpoint)

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
# '500 units', '199.65 €' o '100.000,00', ignorando simbolos de moneda,
# espacios o palabras alrededor. Si no encuentra ningun numero, devuelve
# None en vez de fallar. (Corregido antes: la version original solo
# quitaba el simbolo de euro y no reconocia palabras pegadas al numero --
# ver seccion 5.42 de la documentacion del proyecto.)
#
# Corregido de nuevo (seccion 5.23): la version anterior de esta funcion
# asumia siempre la convencion estadounidense (coma = separador de miles,
# punto = decimal) -- por ejemplo "$5,000.00" -> 5000.0. Eso interpreta
# MAL cualquier cifra en convencion colombiana (punto = miles, coma =
# decimal), que es como las facturas electronicas colombianas reales
# imprimen sus numeros. El caso que expuso el bug: una factura electronica
# real traia "Cantidad: 1,00" (es decir, uno) y la funcion anterior lo
# convertia en 100.0 (le quitaba la coma sin mas, como si fuera un
# separador de miles).
#
# La regla para decidir cual convencion aplica en cada caso:
#   - Si el texto tiene punto Y coma, el separador decimal es el que
#     aparece MAS A LA DERECHA (mas cerca del final) -- el otro simbolo,
#     sea cual sea, es separador de miles. Ej: "100.000,00" (la coma va
#     de ultima) es colombiano -> 100000.0. "5,000.00" (el punto va de
#     ultimo) es estadounidense -> 5000.0.
#   - Si el texto solo tiene uno de los dos simbolos, se decide por la
#     cantidad de digitos despues de ese simbolo: exactamente 2 digitos
#     casi siempre es un decimal (centavos, en cualquiera de las dos
#     convenciones); exactamente 3 digitos casi siempre es una
#     agrupacion de miles sin decimales. Ej: "1,00" (2 digitos tras la
#     coma) -> 1.0. "5,000" (3 digitos tras la coma) -> 5000.0. "5.000"
#     (3 digitos tras el punto) -> 5000.0. "199.65" (2 digitos tras el
#     punto, el caso mas comun en las facturas de prueba en ingles ya
#     usadas en este proyecto) -> 199.65, sin cambios de comportamiento.
def limpiar_numero(texto):
    if not texto:
        return None
    coincidencia = re.search(r'\d[\d.,]*', texto)
    if not coincidencia:
        return None
    numero_bruto = coincidencia.group()

    tiene_punto = '.' in numero_bruto
    tiene_coma = ',' in numero_bruto

    if tiene_punto and tiene_coma:
        if numero_bruto.rfind(',') > numero_bruto.rfind('.'):
            # La coma va de ultima: convencion colombiana.
            # "100.000,00" -> quita los puntos (miles), la coma pasa a
            # ser el punto decimal -> "100000.00"
            limpio = numero_bruto.replace('.', '').replace(',', '.')
        else:
            # El punto va de ultimo: convencion estadounidense.
            # "5,000.00" -> quita las comas (miles) -> "5000.00"
            limpio = numero_bruto.replace(',', '')
    elif tiene_coma:
        digitos_despues = len(numero_bruto.split(',')[-1])
        if digitos_despues == 2:
            # "1,00" -> "1.00" (decimal colombiano)
            limpio = numero_bruto.replace(',', '.')
        else:
            # "5,000" -> "5000" (miles estadounidense, sin decimales)
            limpio = numero_bruto.replace(',', '')
    elif tiene_punto:
        digitos_despues = len(numero_bruto.split('.')[-1])
        if digitos_despues == 3:
            # "5.000" -> "5000" (miles colombiano, sin decimales)
            limpio = numero_bruto.replace('.', '')
        else:
            # "199.65" -> sin cambios (decimal estadounidense, el caso
            # ya usado por todas las facturas de prueba de este proyecto)
            limpio = numero_bruto
    else:
        limpio = numero_bruto

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

# Reconoce si un texto tiene "forma" de NIT colombiano: una tira de 9 o 10
# digitos una vez quitados puntos, espacios o guiones (ej. '901640801',
# '901143842 7', '900.534.356-3'). Se compara la cantidad de digitos, no el
# texto crudo, porque el formato varia bastante entre facturas.
def _parece_nit(texto):
    solo_digitos = re.sub(r'\D', '', texto or '')
    return len(solo_digitos) in (9, 10)

# Busca el NIT del proveedor entre los SummaryFields sin filtrar de
# Textract, como respaldo cuando 'TAX_PAYER_ID' no aparece -- ver seccion
# 5.34: en las facturas colombianas probadas, Textract nunca devolvio ese
# campo. En su lugar, el NIT aparece clasificado como el tipo generico
# 'OTHER', junto con datos sin relacion (numero de factura, NIT del
# cliente, NIT del proveedor del software de facturacion, etc.).
#
# Heuristica, NO garantizada: se recorren todos los campos 'OTHER' en el
# orden en que Textract los devuelve -- que en la practica sigue de cerca
# el orden de lectura del documento -- y se toma el primer valor con forma
# de NIT. El emisor de una factura casi siempre encabeza el documento antes
# que el cliente o el pie de pagina, asi que el primer candidato suele ser
# el correcto, pero esto es una apuesta sobre el orden tipico de una
# factura, no una lectura de un campo dedicado -- a diferencia de
# VENDOR_NAME o TOTAL, que si vienen de un campo especifico de Textract.
# Si aparece mas de un candidato (como en la factura de prueba de Baterias
# Colombia, con tres: proveedor, cliente y software de facturacion), se dej
# a un aviso no bloqueante en el log para poder revisar despues cuales
# facturas cayeron en el caso ambiguo -- mismo criterio que ya usa
# guardar_en_rds para las reconciliaciones que no cuadran.
def extraer_nit_probable(response):
    documento = response['ExpenseDocuments'][0]
    candidatos = [
        field.get('ValueDetection', {}).get('Text', '')
        for field in documento['SummaryFields']
        if field['Type']['Text'] == 'OTHER'
        and _parece_nit(field.get('ValueDetection', {}).get('Text', ''))
    ]

    if not candidatos:
        return None
    if len(candidatos) > 1:
        print(
            f"AVISO: se encontraron {len(candidatos)} valores con forma de "
            f"NIT dentro de los campos OTHER {candidatos} -- se uso el "
            f"primero ({candidatos[0]!r}) como NIT del proveedor, sin "
            f"garantia de que sea el correcto (podria ser el del cliente o "
            f"el de un tercero, como el proveedor del software de "
            f"facturacion)."
        )
    return candidatos[0]

# Convierte un texto de Textract a None si viene vacio -- cadena vacia
# (paso con RECEIVER_PHONE en una factura real de prueba: Textract detecto
# el TIPO de campo pero el VALOR quedo en blanco) o solo espacios en
# blanco. Mismo criterio de NULL vs. dato inventado que ya siguen
# limpiar_numero() y parsear_fecha() con los campos numericos y de fecha --
# aqui no hace falta ninguna limpieza de formato (a diferencia de esas dos),
# porque telefono, direccion y numero de factura son identificadores/texto
# libre, no cantidades ni fechas que se puedan interpretar mal.
def campo_texto_o_none(texto):
    if not texto:
        return None
    texto = texto.strip()
    return texto if texto else None

# Se conecta a RDS, inserta una fila en 'factura' con los datos generales,
# y una fila en 'item_factura' por cada item detectado, enlazadas mediante
# el factura_id que genera la base de datos automaticamente.
def guardar_en_rds(campos, items, s3_key, nit):
    conexion = pg8000.native.Connection(
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME
    )

    try:
        # 'TAX' es el tipo de campo estandar que Textract usa para el valor
        # del impuesto en una factura, igual que 'TOTAL' o 'VENDOR_NAME' --
        # ver seccion 5.28 de la documentacion del proyecto. Cuando la
        # factura no tiene impuesto detectado (o no aplica, como en el caso
        # de un vendedor no responsable de IVA), campos.get('TAX') devuelve
        # None y limpiar_numero lo deja en None tambien -- se guarda como
        # NULL en la base de datos, nunca como 0 (0 significaria "el
        # impuesto es cero", que es una afirmacion distinta a "no se sabe").
        #
        # 'nit' llega ya resuelto desde lambda_handler: primero se intenta
        # 'TAX_PAYER_ID' (el campo estandar de Textract para esto -- ver
        # seccion 5.33), y si no aparece, se cae a extraer_nit_probable()
        # (la heuristica de la seccion 5.34, sobre los campos 'OTHER'). Se
        # guarda como texto tal cual, sin limpiar puntos ni guiones -- a
        # diferencia de un campo numerico, el NIT es un identificador, no
        # una cantidad para operar aritmeticamente.
        # Punto 2 de la lista de mejoras del usuario (2026-09-02): telefono y
        # direccion del proveedor y del comprador, y numero de factura --
        # Textract ya los detecta con tipos de campo propios (VENDOR_PHONE,
        # VENDOR_ADDRESS, RECEIVER_NAME, RECEIVER_PHONE, RECEIVER_ADDRESS,
        # INVOICE_RECEIPT_ID), pero hasta ahora quedaban solo dentro de
        # datos_textract_raw sin extraerse a su propia columna -- mismo
        # patron ya usado con impuesto (5.28) y NIT (5.33). De estos, solo
        # VENDOR_ADDRESS, RECEIVER_NAME, RECEIVER_PHONE e INVOICE_RECEIPT_ID
        # se confirmaron con datos reales (factura del Hotel Prado del
        # Huila); VENDOR_PHONE y RECEIVER_ADDRESS se agregan igual, por ser
        # tipos de campo estandar documentados por Textract, bajo el mismo
        # criterio que ya se sigue en todo el proyecto: si Textract nunca
        # los detecta, quedan en NULL, nunca en un valor inventado.
        resultado = conexion.run(
            """
            INSERT INTO factura (
                proveedor_nombre, nit, fecha_factura, total, impuesto, s3_key, datos_textract_raw,
                telefono_proveedor, direccion_proveedor, comprador_nombre, telefono_comprador, direccion_comprador, numero_factura
            )
            VALUES (
                :proveedor, :nit, :fecha, :total, :impuesto, :s3_key, :raw,
                :telefono_proveedor, :direccion_proveedor, :comprador_nombre, :telefono_comprador, :direccion_comprador, :numero_factura
            )
            RETURNING factura_id
            """,
            proveedor=campos.get('VENDOR_NAME'),
            nit=nit,
            fecha=parsear_fecha(campos.get('INVOICE_RECEIPT_DATE')),
            total=limpiar_numero(campos.get('TOTAL')),
            impuesto=limpiar_numero(campos.get('TAX')),
            s3_key=s3_key,
            raw=json.dumps(campos),
            telefono_proveedor=campo_texto_o_none(campos.get('VENDOR_PHONE')),
            direccion_proveedor=campo_texto_o_none(campos.get('VENDOR_ADDRESS')),
            comprador_nombre=campo_texto_o_none(campos.get('RECEIVER_NAME')),
            telefono_comprador=campo_texto_o_none(campos.get('RECEIVER_PHONE')),
            direccion_comprador=campo_texto_o_none(campos.get('RECEIVER_ADDRESS')),
            numero_factura=campo_texto_o_none(campos.get('INVOICE_RECEIPT_ID'))
        )
        factura_id = resultado[0][0]

        # Se acumulan aqui los subtotales de cada item (cuando Textract lo
        # detecto) para la validacion de factura completa que se hace
        # despues del for, una vez insertados todos los items.
        subtotales_detectados = []

        for item in items:
            descripcion = item.get('ITEM')
            cantidad = limpiar_numero(item.get('QUANTITY'))
            precio = limpiar_numero(item.get('UNIT_PRICE'))
            subtotal = limpiar_numero(item.get('PRICE'))
            subtotales_detectados.append(subtotal)

            # Validacion (no bloqueante, no correctiva): si Textract detecto
            # los tres valores, cantidad x precio deberia dar aprox. el
            # subtotal. Si no cuadra, NO se corrige ni se recalcula nada --
            # eso equivaldria a inventar un valor que Textract no reporto,
            # el mismo riesgo que ya se evito deliberadamente en el
            # asistente conversacional (reglas 9 y 10). Solo se deja un
            # aviso en los logs de CloudWatch para poder revisarlo despues.
            # La tolerancia (0.5) da margen a redondeos de centavos que
            # Textract a veces introduce al leer el subtotal impreso.
            if cantidad is not None and precio is not None and subtotal is not None:
                calculado = round(cantidad * precio, 2)
                if abs(calculado - subtotal) > 0.5:
                    print(
                        f"AVISO factura_id={factura_id}: el item "
                        f"'{descripcion}' no cuadra -- cantidad ({cantidad}) "
                        f"x precio ({precio}) = {calculado}, pero el "
                        f"subtotal detectado por Textract es {subtotal}"
                    )

            conexion.run(
                """
                INSERT INTO item_factura (factura_id, descripcion, cantidad, precio_unitario, subtotal)
                VALUES (:factura_id, :descripcion, :cantidad, :precio, :subtotal)
                """,
                factura_id=factura_id,
                descripcion=descripcion,
                cantidad=cantidad,
                precio=precio,
                subtotal=subtotal
            )

        # Validacion adicional a nivel de factura completa (no bloqueante,
        # no correctiva -- mismo criterio que la validacion por item de
        # arriba): la suma de los subtotales de todos los items deberia
        # aproximarse al total de la factura.
        #
        # OJO con un supuesto que esta validacion SI hace y que puede no
        # cumplirse: asume que 'total' es el valor final facturado, y que
        # ese total puede o no incluir el impuesto por encima de la suma de
        # subtotales -- eso varia segun como cada proveedor imprime su
        # factura. Por eso no se compara contra un solo candidato: se
        # compara contra la suma de subtotales sola, y (si hay impuesto
        # detectado) tambien contra esa suma mas el impuesto. Solo se avisa
        # si NINGUNA de las dos cuadra -- si se comparara contra un solo
        # candidato fijo, facturas correctas con IVA (o sin el) generarian
        # avisos falsos sistematicamente, no seria un caso raro. La
        # tolerancia (0.5) es la misma que la validacion por item; al sumar
        # varios items el redondeo acumulado podria en teoria superarla en
        # facturas con muchas lineas, algo a tener en cuenta si este aviso
        # empieza a salir seguido en facturas que sí cuadran a simple vista.
        total_factura = limpiar_numero(campos.get('TOTAL'))
        if total_factura is not None:
            if subtotales_detectados and all(s is not None for s in subtotales_detectados):
                suma_subtotales = round(sum(subtotales_detectados), 2)
                impuesto_factura = limpiar_numero(campos.get('TAX'))
                candidatos = [suma_subtotales]
                if impuesto_factura is not None:
                    candidatos.append(round(suma_subtotales + impuesto_factura, 2))

                if not any(abs(c - total_factura) <= 0.5 for c in candidatos):
                    detalle_impuesto = (
                        f", ni sumandole el impuesto detectado ({impuesto_factura}) "
                        f"para dar {candidatos[-1]}"
                        if impuesto_factura is not None else
                        " (no se detecto impuesto para probar esa alternativa)"
                    )
                    print(
                        f"AVISO factura_id={factura_id}: la suma de los "
                        f"subtotales de los items ({suma_subtotales}) no "
                        f"cuadra con el total de la factura ({total_factura})"
                        f"{detalle_impuesto}"
                    )
                elif (
                    impuesto_factura is not None
                    and abs(suma_subtotales - total_factura) <= 0.5
                    and abs(candidatos[-1] - total_factura) > 0.5
                ):
                    # Caso distinto del AVISO de arriba: aqui SI cuadra, pero
                    # cuadra con la suma de subtotales SOLA -- es decir, el
                    # total de esta factura en particular parece no incluirle
                    # el impuesto por encima (la excepcion que se menciono
                    # que existe, frente a la regla general de que el total
                    # deberia incluirlo). No es un error de la factura ni de
                    # la extraccion, por eso va como INFO y no como AVISO --
                    # pero se deja registrado para tener evidencia real de
                    # que tan seguido pasa esto, en vez de quedarse solo con
                    # la impresion de "en promedio, el total deberia incluir
                    # el impuesto".
                    print(
                        f"INFO factura_id={factura_id}: el total de esta "
                        f"factura ({total_factura}) parece NO incluir el "
                        f"impuesto detectado ({impuesto_factura}) -- cuadra "
                        f"con la suma de subtotales sola ({suma_subtotales}), "
                        f"no con suma+impuesto ({candidatos[-1]})"
                    )
            else:
                print(
                    f"AVISO factura_id={factura_id}: no se pudo validar suma "
                    f"de subtotales contra el total -- uno o mas items no "
                    f"tienen subtotal detectado por Textract"
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

    # Primero se intenta el campo estandar de Textract (TAX_PAYER_ID); si no
    # aparece -- el caso visto en todas las facturas colombianas probadas
    # hasta ahora -- se cae a la heuristica sobre los campos 'OTHER' (ver
    # extraer_nit_probable, seccion 5.34).
    nit = campos.get('TAX_PAYER_ID') or extraer_nit_probable(response)

    print("===== RESUMEN DE LA FACTURA =====")
    print(f"Proveedor: {campos.get('VENDOR_NAME', 'No detectado')}")
    print(f"NIT: {nit or 'No detectado'}")
    print(f"Fecha detectada: {campos.get('INVOICE_RECEIPT_DATE', 'No detectada')} -> {parsear_fecha(campos.get('INVOICE_RECEIPT_DATE'))}")
    print(f"Total: {campos.get('TOTAL', 'No detectado')}")
    print(f"Impuesto: {campos.get('TAX', 'No detectado')}")
    print(f"Numero de factura: {campo_texto_o_none(campos.get('INVOICE_RECEIPT_ID')) or 'No detectado'}")
    print(f"Telefono proveedor: {campo_texto_o_none(campos.get('VENDOR_PHONE')) or 'No detectado'}")
    print(f"Direccion proveedor: {campo_texto_o_none(campos.get('VENDOR_ADDRESS')) or 'No detectada'}")
    print(f"Comprador: {campo_texto_o_none(campos.get('RECEIVER_NAME')) or 'No detectado'}")
    print(f"Telefono comprador: {campo_texto_o_none(campos.get('RECEIVER_PHONE')) or 'No detectado'}")
    print(f"Direccion comprador: {campo_texto_o_none(campos.get('RECEIVER_ADDRESS')) or 'No detectada'}")
    print(f"Items detectados: {len(items)}")
    print("==================================")

    factura_id = guardar_en_rds(campos, items, key, nit)

    return {
        'statusCode': 200,
        'body': json.dumps(f'Factura procesada y guardada con ID {factura_id}')
    }
