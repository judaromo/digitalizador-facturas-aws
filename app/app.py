from flask import Flask, request, jsonify, render_template_string, Response, redirect, url_for
import boto3
import uuid
from botocore.client import Config
from botocore.exceptions import ClientError
import pg8000.native
# date y timedelta se usan para calcular los rangos de fechas de las
# consultas nuevas del panel (ultimos 30 dias, dia de hoy, etc.) sin
# depender de que el usuario escriba las fechas a mano.
from datetime import date, timedelta
# xml_escape: para armar la respuesta TwiML del webhook de WhatsApp sin
# que un mensaje del usuario que contenga "<", ">" o "&" rompa el XML.
from xml.sax.saxutils import escape as xml_escape
# RequestValidator: valida la firma que Twilio agrega a cada peticion del
# webhook de WhatsApp. Se intento primero una implementacion manual del
# algoritmo de firma (HMAC-SHA1 documentado por Twilio) -- funcionaba
# correctamente, pero se descarto de todas formas: la propia
# documentacion de Twilio pide explicitamente no reimplementar esto,
# porque pueden agregar parametros nuevos sin aviso, y esta validacion
# controla el acceso a una ruta que invoca a Bedrock (que tiene costo).
from twilio.request_validator import RequestValidator

app = Flask(__name__)
s3 = boto3.client('s3', config=Config(signature_version='s3v4'))


# Formatea un numero al estilo colombiano: punto como separador de miles y
# coma como separador decimal (ej. 1500000.5 -> "1.500.000,50"). Python no
# tiene un formato local para esto sin instalar dependencias adicionales
# (el modulo "locale" existiria, pero depende de que el sistema operativo
# tenga el locale es_CO instalado, algo fragil de garantizar en un
# servidor). En vez de eso, se genera el numero en formato estadounidense
# (coma para miles, punto para decimales) y se intercambian los dos
# simbolos usando un marcador temporal, para no pisar uno con el otro a
# mitad de camino.
def formatear_numero(valor, decimales=2):
    texto_formato = '{:,.' + str(decimales) + 'f}'
    texto_us = texto_formato.format(valor)
    texto_co = texto_us.replace(',', '§').replace('.', ',').replace('§', '.')
    return texto_co


# Registra formatear_numero como un filtro de Jinja llamado "cop", para
# poder usarlo directamente en las plantillas HTML como {{ valor | cop }}.
app.jinja_env.filters['cop'] = formatear_numero


# ---- Helpers para el formulario de edicion manual de facturas ----
#
# A diferencia de limpiar_numero() (que interpreta texto libre leido por
# Textract, con simbolos de moneda y las dos convenciones de separador
# decimal), estos helpers leen campos de un formulario HTML propio: los
# inputs type="number" siempre envian su valor con punto como separador
# decimal sin importar el idioma del navegador (es un requisito del
# estandar HTML, no una eleccion de este proyecto), asi que no hace falta
# ninguna deteccion de convencion aqui -- solo distinguir "vacio" de "el
# usuario escribio un numero".

# Convierte un campo de texto del formulario a None si quedo vacio (en vez
# de guardar una cadena vacia ''), para que se comporte igual que un dato
# que Textract nunca detecto -- mismo criterio de NULL en toda la app.
def campo_texto_o_none(valor):
    if valor is None:
        return None
    valor = valor.strip()
    return valor if valor else None


# Igual que campo_texto_o_none, pero convierte a float. Si el usuario borra
# el campo a proposito (porque el dato que Textract leyo esta mal y no hay
# forma de saber el valor correcto a partir de la imagen), se guarda como
# NULL, nunca como 0 -- 0 significaria "el valor es cero", una afirmacion
# distinta a "no se sabe".
def campo_numerico_o_none(valor):
    if valor is None:
        return None
    valor = valor.strip()
    if not valor:
        return None
    return float(valor)


# Igual que los anteriores, para el campo de fecha (input type="date", que
# entrega 'AAAA-MM-DD' o cadena vacia). date.fromisoformat() es estricto
# con ese formato exacto, que es justo lo que un input type="date" nunca
# deja de cumplir cuando trae un valor -- el try/except es solo defensivo,
# no se espera que dispare en uso normal.
def campo_fecha_o_none(valor):
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return None

# Reconciliacion de factura completa: MISMA logica que la validacion que
# ya existe en la Lambda (lambda_procesar_factura.py, guardar_en_rds,
# seccion 5.29 de la bitacora) -- pero calculada aqui, en el momento de
# mostrar el panel, en vez de una sola vez cuando se procesa la factura.
# Se eligio asi a proposito, no por descuido: calculandolo aqui, a partir
# de lo que ya esta guardado en item_factura/factura, el chequeo aplica
# de una vez a las 46 facturas ya procesadas (incluida Dotaciones Gamero)
# sin necesidad de reprocesarlas ni de una migracion de datos -- el mismo
# resultado que daria repetir el calculo de la Lambda, sin sus costos.
#
# Devuelve un diccionario con 'estado' en uno de cuatro valores:
#   'ok'          -- el total cuadra (con o sin impuesto sumado encima)
#   'sin_impuesto'-- cuadra SOLO sin sumarle el impuesto (ver seccion 5.29:
#                    puede ser normal segun como el proveedor imprima el
#                    total, no necesariamente un error)
#   'no_cuadra'   -- no cuadra con ningun candidato -- amerita revisar la
#                    imagen original de la factura
#   'sin_datos'   -- falta informacion (sin total, sin lineas, o algun
#                    item sin subtotal) para poder comparar algo
def validar_total_factura(total, impuesto, lineas):
    if total is None:
        return {'estado': 'sin_datos'}
    subtotales = [linea['subtotal'] for linea in lineas]
    if not subtotales or any(s is None for s in subtotales):
        return {'estado': 'sin_datos'}

    suma_subtotales = round(float(sum(subtotales)), 2)
    total = float(total)
    candidatos = [suma_subtotales]
    if impuesto is not None:
        candidatos.append(round(suma_subtotales + float(impuesto), 2))

    if not any(abs(c - total) <= 0.5 for c in candidatos):
        return {'estado': 'no_cuadra', 'suma_subtotales': suma_subtotales}
    if (
        impuesto is not None
        and abs(suma_subtotales - total) <= 0.5
        and abs(candidatos[-1] - total) > 0.5
    ):
        return {'estado': 'sin_impuesto', 'suma_subtotales': suma_subtotales}
    return {'estado': 'ok', 'suma_subtotales': suma_subtotales}

# Cliente de SSM Parameter Store -- se crea una sola vez, a nivel de modulo,
# para no reconstruirlo en cada peticion HTTP.
ssm = boto3.client('ssm', region_name='us-east-1')

BUCKET_NAME = 'facturas-microempresarios-<TU-CUENTA-AWS>'
DB_HOST = '<TU-ENDPOINT-RDS>.rds.amazonaws.com'
DB_PORT = 5432
DB_NAME = 'facturas'
DB_USER = 'postgres'

# La contraseña ya no esta escrita aqui: se consulta a SSM Parameter Store
# una sola vez, cuando la aplicacion arranca. WithDecryption=True le dice a
# SSM que use la llave KMS asociada para devolver el valor real.
DB_PASSWORD = ssm.get_parameter(
    Name='/facturas-app/rds-password',
    WithDecryption=True
)['Parameter']['Value']

# Mismo criterio que DB_PASSWORD: el Auth Token de Twilio se usa para
# validar la firma de cada peticion al webhook de WhatsApp (ver
# validar_firma_twilio), asi que es tan sensible como una contraseña -- no
# va escrito en el codigo. Crear este parametro en SSM Parameter Store
# (tipo SecureString) con el Auth Token que aparece en la consola de
# Twilio, en la pagina principal del proyecto ("Account Info").
TWILIO_AUTH_TOKEN = ssm.get_parameter(
    Name='/facturas-app/twilio-auth-token',
    WithDecryption=True
)['Parameter']['Value']

# Se crea una sola vez, a nivel de modulo, igual que los clientes de
# boto3 -- se reutiliza en cada peticion al webhook de WhatsApp.
validador_twilio = RequestValidator(TWILIO_AUTH_TOKEN)

# --- Dashboard compartido (punto 3 de las mejoras pedidas el 2026-09-02:
# "consolidar la navegacion... en un dashboard unico"; rediseño visual
# agregado el mismo dia porque la barra de pestañas sola, sobre el CSS
# minimo que ya tenia cada pagina, seguia viendose "basica" -- ver seccion
# 5.39 de la bitacora).
#
# Sigue siendo la misma decision de arquitectura que el punto 3 original:
# pestañas sobre las paginas existentes, NO una pagina unica real con JS.
# Cada ruta sigue siendo su propio endpoint de Flask que renderiza su
# propia plantilla -- lo que cambia con el rediseño es que las 6
# plantillas (las 5 rutas del dashboard mas /facturas/<id>/editar) ahora
# comparten Tailwind (via CDN, mismo patron que ya se usaba con Chart.js
# -- sin build, sin npm) en vez de CSS escrito a mano por pagina.
#
# No se resuelve con un include de Jinja (no hay un DictLoader de
# plantillas, son strings de Python) -- en vez de eso, esta funcion arma
# el HTML ya resuelto en Python y cada ruta lo pasa como variable
# (barra_nav) a render_template_string, para insertarla con {{ barra_nav|safe }}.
PESTANAS_DASHBOARD = [
    ('/', 'Subir factura'),
    ('/facturas', 'Facturas'),
    ('/panel', 'Panel'),
    ('/registrar-venta', 'Registrar venta'),
    ('/asistente', 'Asistente'),
]

# Se inserta identico en el <head> de las 6 plantillas -- concatenado con
# Python (no hay Jinja include), igual que se hacia antes con
# BARRA_NAV_CSS. Tailwind CDN observa el DOM y compila las clases que
# encuentra (incluidas las que un script agrega despues de cargar, como
# el className de los mensajes del chat en ASISTENTE_HTML), asi que no
# hace falta ningun paso de build.
CABECERA_TAILWIND = '<script src="https://cdn.tailwindcss.com"></script>'


def generar_barra_nav(ruta_activa):
    """Arma el HTML de la barra de navegacion, marcando como activa la
    pestaña de `ruta_activa`. Se resuelve en Python (no hay estado
    dinamico que justifique hacerlo en Jinja)."""
    enlaces = []
    for ruta, etiqueta in PESTANAS_DASHBOARD:
        if ruta == ruta_activa:
            clase = 'bg-orange-100 text-orange-900'
        else:
            clase = 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
        enlaces.append(
            f'<a href="{ruta}" class="rounded-full px-3 py-1.5 text-sm font-medium {clase}">{etiqueta}</a>'
        )
    return (
        '<nav class="border-b border-gray-200 bg-white">'
        '<div class="mx-auto flex max-w-5xl flex-wrap items-center gap-1 px-4 py-3">'
        '<span class="mr-3 text-sm font-bold tracking-tight text-gray-800">Facturas</span>'
        + ''.join(enlaces) +
        '</div></nav>'
    )


PAGINA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Digitalizar Factura</title>
    """ + CABECERA_TAILWIND + """
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-md px-4 py-10">
        <div class="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
            <h1 class="text-xl font-bold text-gray-900">Digitalizar factura</h1>
            <p class="mt-1 text-sm text-gray-500">Toma una foto de tu factura o recibo:</p>
            <input type="file" id="archivo" accept="image/*,application/pdf" capture="environment"
                class="mt-4 block w-full text-sm text-gray-700 file:mr-4 file:rounded-md file:border-0 file:bg-gray-100 file:px-4 file:py-2 file:text-sm file:font-medium file:text-gray-700 hover:file:bg-gray-200">
            <button onclick="subirFactura()"
                class="mt-5 w-full rounded-md bg-orange-500 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-orange-600">
                Subir factura
            </button>
            <div id="estado" class="mt-4 text-sm font-medium text-gray-700"></div>
        </div>
    </main>
    <script>
        // Comprime y redimensiona una foto antes de subirla, usando un
        // <canvas> del navegador. Esto SOLO funciona con imagenes de verdad
        // (JPEG, PNG, etc.) -- un <img> del navegador no puede decodificar
        // un PDF, asi que si se le pasa un PDF, img.src nunca dispara
        // onload (ni onerror, sin el manejo de abajo), y la Promise se
        // queda esperando para siempre. Por eso subirFactura() de abajo
        // nunca llama a esta funcion con un PDF -- lo sube tal cual.
        // Se agrega tambien un manejo de error explicito (antes no existia)
        // para el caso de un archivo de imagen corrupto o no reconocido,
        // que antes se habria quedado colgado de la misma forma.
        function comprimirImagen(archivo) {
            return new Promise((resolve, reject) => {
                const img = new Image();
                const lector = new FileReader();
                lector.onload = (e) => {
                    img.onerror = () => reject(new Error('No se pudo leer la imagen.'));
                    img.src = e.target.result;
                    img.onload = () => {
                        const anchoMax = 1600;
                        const escala = Math.min(1, anchoMax / img.width);
                        const canvas = document.createElement('canvas');
                        canvas.width = img.width * escala;
                        canvas.height = img.height * escala;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        canvas.toBlob((blob) => {
                            resolve(new File([blob], archivo.name, { type: 'image/jpeg' }));
                        }, 'image/jpeg', 0.8);
                    };
                };
                lector.onerror = () => reject(new Error('No se pudo leer el archivo.'));
                lector.readAsDataURL(archivo);
            });
        }
        async function subirFactura() {
            const input = document.getElementById('archivo');
            const estado = document.getElementById('estado');
            if (!input.files.length) {
                estado.textContent = 'Primero selecciona o toma una foto.';
                return;
            }
            const archivoOriginal = input.files[0];
            let archivo;
            try {
                if (archivoOriginal.type === 'application/pdf') {
                    // Un PDF se sube tal cual, sin pasar por comprimirImagen
                    // -- ese paso es exclusivo de fotos y con un PDF nunca
                    // terminaria (ver el comentario de la funcion).
                    estado.textContent = 'Preparando subida...';
                    archivo = archivoOriginal;
                } else {
                    estado.textContent = 'Optimizando imagen...';
                    archivo = await comprimirImagen(archivoOriginal);
                    estado.textContent = 'Preparando subida...';
                }
            } catch (error) {
                estado.textContent = 'No se pudo procesar el archivo seleccionado.';
                return;
            }
            const respuestaUrl = await fetch('/get-upload-url?filename=' + encodeURIComponent(archivo.name));
            const datos = await respuestaUrl.json();
            estado.textContent = 'Subiendo a S3...';
            const respuestaSubida = await fetch(datos.upload_url, {
                method: 'PUT',
                body: archivo,
                headers: { 'Content-Type': archivo.type }
            });
            if (respuestaSubida.ok) {
                estado.textContent = 'Factura subida correctamente. Procesando...';
            } else {
                estado.textContent = 'Error al subir la factura.';
            }
        }
    </script>
</body>
</html>
"""

PANEL_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Facturas procesadas</title>
    """ + CABECERA_TAILWIND + """
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-3xl px-4 py-8">
        <h1 class="text-xl font-bold text-gray-900">Facturas procesadas ({{ facturas|length }})</h1>
        {% if not facturas %}
            <p class="mt-4 text-sm text-gray-500">Todavia no hay facturas procesadas.</p>
        {% endif %}
        <div class="mt-5 space-y-4">
        {% for factura in facturas %}
        <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h3 class="flex flex-wrap items-center gap-2 text-base font-semibold text-gray-900">
                {{ factura.proveedor_nombre or 'Proveedor no detectado' }}
                {% if factura.editado_manualmente %}<span class="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">editado a mano</span>{% endif %}
            </h3>
            {% if factura.nit %}
            <p class="text-xs text-gray-500">NIT: {{ factura.nit }}</p>
            {% endif %}
            {% if factura.telefono_proveedor %}
            <p class="mt-1 text-sm text-gray-600">Tel. proveedor: {{ factura.telefono_proveedor }}</p>
            {% endif %}
            {% if factura.direccion_proveedor %}
            <p class="mt-1 text-sm text-gray-600">Dir. proveedor: {{ factura.direccion_proveedor }}</p>
            {% endif %}
            {% if factura.numero_factura %}
            <p class="mt-1 text-sm text-gray-600">No. factura: {{ factura.numero_factura }}</p>
            {% endif %}
            <p class="mt-2 text-sm text-gray-600">Procesada: {{ factura.fecha_procesado }}</p>
            <p class="text-sm text-gray-600">Fecha de factura: {% if factura.fecha_factura %}{{ factura.fecha_factura }}{% else %}no detectada{% endif %}</p>
            <p class="mt-2 text-lg font-bold text-orange-600">Total: {% if factura.total is not none %}${{ factura.total | cop }}{% else %}sin total detectado{% endif %}</p>
            {% if factura.impuesto is not none %}
            <p class="text-sm text-gray-600">Impuesto: ${{ factura.impuesto | cop }}</p>
            {% endif %}
            {% if factura.comprador_nombre or factura.telefono_comprador or factura.direccion_comprador %}
            <div class="mt-3 border-l-2 border-gray-200 pl-3">
                <p class="text-xs font-medium uppercase tracking-wide text-gray-400">Vendido a</p>
                {% if factura.comprador_nombre %}<p class="text-sm text-gray-600">{{ factura.comprador_nombre }}</p>{% endif %}
                {% if factura.telefono_comprador %}<p class="text-sm text-gray-600">Tel: {{ factura.telefono_comprador }}</p>{% endif %}
                {% if factura.direccion_comprador %}<p class="text-sm text-gray-600">{{ factura.direccion_comprador }}</p>{% endif %}
            </div>
            {% endif %}
            {% if factura.validacion.estado == 'no_cuadra' %}
            <p class="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                &#9888; El total no cuadra con la suma de los items (${{ factura.validacion.suma_subtotales | cop }}){% if factura.impuesto is not none %} ni sumandole el impuesto{% endif %} -- revisar la imagen original.
            </p>
            {% elif factura.validacion.estado == 'sin_impuesto' %}
            <p class="mt-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600">
                El total parece no incluir el impuesto por encima de la suma de items (${{ factura.validacion.suma_subtotales | cop }}).
            </p>
            {% endif %}
            {% if factura.imagen_url %}
            <p class="mt-3 text-sm"><a href="{{ factura.imagen_url }}" target="_blank" rel="noopener" class="font-medium text-blue-600 hover:underline">Ver imagen original de la factura</a></p>
            {% endif %}
            <p class="mt-1 text-sm"><a href="/facturas/{{ factura.factura_id }}/editar" class="font-medium text-blue-600 hover:underline">Editar esta factura &rarr;</a></p>
            <table class="mt-3 w-full text-sm">
                <tr class="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-400">
                    <th class="py-1.5 pr-2 font-medium">Descripcion</th><th class="py-1.5 pr-2 font-medium">Cant.</th><th class="py-1.5 pr-2 font-medium">Precio</th><th class="py-1.5 font-medium">Subtotal</th>
                </tr>
                {% for item in factura.lineas %}
                <tr class="border-b border-gray-100 text-gray-700">
                    <td class="py-1.5 pr-2">{{ item.descripcion or '-' }}</td>
                    <td class="py-1.5 pr-2">{{ item.cantidad if item.cantidad is not none else '-' }}</td>
                    <td class="py-1.5 pr-2">{% if item.precio_unitario is not none %}${{ item.precio_unitario | cop }}{% else %}-{% endif %}</td>
                    <td class="py-1.5">{% if item.subtotal is not none %}${{ item.subtotal | cop }}{% else %}-{% endif %}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
        {% endfor %}
        </div>
    </main>
</body>
</html>
"""

# Plantilla del formulario de edicion manual (punto 1 de la lista de
# mejoras que pidio el usuario, 2026-09-02). Deliberadamente NO agrega ni
# quita filas de items en esta primera version -- solo corrige los valores
# de las filas que ya creo la Lambda al procesar la factura. Agregar/quitar
# items queda anotado como mejora futura opcional en la bitacora, mismo
# criterio que otras limitaciones ya documentadas: no es el problema que
# se pidio resolver aqui (dato mal leido, no item faltante).
EDITAR_FACTURA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Editar factura</title>
    """ + CABECERA_TAILWIND + """
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-3xl px-4 py-8">
        <h1 class="text-xl font-bold text-gray-900">Editar factura #{{ factura_id }}</h1>
        <p class="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            Corrige aqui cualquier dato que Textract haya leido mal a partir de la imagen original.
            Deja un campo vacio si no puedes confirmar el valor correcto -- se guarda como "no detectado",
            nunca como cero, para no reemplazar un error por otro.
        </p>
        <form method="POST" class="mt-5 space-y-5">
            <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <label class="block text-sm font-medium text-gray-600">Numero de factura
                        <input type="text" name="numero_factura" value="{{ factura.numero_factura or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Fecha de factura
                        <input type="date" name="fecha_factura" value="{{ factura.fecha_factura or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Total
                        <input type="number" step="0.01" name="total" value="{{ factura.total if factura.total is not none else '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Impuesto
                        <input type="number" step="0.01" name="impuesto" value="{{ factura.impuesto if factura.impuesto is not none else '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                </div>
            </div>

            <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h2 class="text-sm font-semibold uppercase tracking-wide text-gray-500">Proveedor</h2>
                <div class="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <label class="block text-sm font-medium text-gray-600">Nombre
                        <input type="text" name="proveedor_nombre" value="{{ factura.proveedor_nombre or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">NIT
                        <input type="text" name="nit" value="{{ factura.nit or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Telefono
                        <input type="text" name="telefono_proveedor" value="{{ factura.telefono_proveedor or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Direccion
                        <input type="text" name="direccion_proveedor" value="{{ factura.direccion_proveedor or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                </div>
            </div>

            <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h2 class="text-sm font-semibold uppercase tracking-wide text-gray-500">Comprador</h2>
                <div class="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <label class="block text-sm font-medium text-gray-600">Nombre
                        <input type="text" name="comprador_nombre" value="{{ factura.comprador_nombre or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600">Telefono
                        <input type="text" name="telefono_comprador" value="{{ factura.telefono_comprador or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                    <label class="block text-sm font-medium text-gray-600 sm:col-span-2">Direccion
                        <input type="text" name="direccion_comprador" value="{{ factura.direccion_comprador or '' }}"
                            class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                    </label>
                </div>
            </div>

            <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h2 class="text-sm font-semibold uppercase tracking-wide text-gray-500">Items</h2>
                {% if not items %}
                <p class="mt-2 text-sm text-gray-500">Esta factura no tiene items registrados.</p>
                {% endif %}
                <div class="mt-3 space-y-3">
                {% for item in items %}
                <div class="rounded-lg border border-gray-200 p-3">
                    <input type="hidden" name="item_id" value="{{ item.item_id }}">
                    <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
                        <label class="block text-xs font-medium text-gray-600 sm:col-span-2">Descripcion
                            <input type="text" name="descripcion_{{ item.item_id }}" value="{{ item.descripcion or '' }}"
                                class="mt-1 block w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                        </label>
                        <label class="block text-xs font-medium text-gray-600">Cantidad
                            <input type="number" step="0.01" name="cantidad_{{ item.item_id }}" value="{{ item.cantidad if item.cantidad is not none else '' }}"
                                class="mt-1 block w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                        </label>
                        <label class="block text-xs font-medium text-gray-600">Precio unitario
                            <input type="number" step="0.01" name="precio_unitario_{{ item.item_id }}" value="{{ item.precio_unitario if item.precio_unitario is not none else '' }}"
                                class="mt-1 block w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                        </label>
                        <label class="block text-xs font-medium text-gray-600">Subtotal
                            <input type="number" step="0.01" name="subtotal_{{ item.item_id }}" value="{{ item.subtotal if item.subtotal is not none else '' }}"
                                class="mt-1 block w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                        </label>
                    </div>
                </div>
                {% endfor %}
                </div>
            </div>

            <button type="submit" class="rounded-md bg-orange-500 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-orange-600">Guardar cambios</button>
        </form>
    </main>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(PAGINA_HTML, barra_nav=generar_barra_nav('/'))

@app.route('/get-upload-url')
def get_upload_url():
    nombre_original = request.args.get('filename', 'factura.jpg')
    extension = nombre_original.split('.')[-1]
    key = f"entrada/{uuid.uuid4()}.{extension}"
    upload_url = s3.generate_presigned_url(
        ClientMethod='put_object',
        Params={'Bucket': BUCKET_NAME, 'Key': key},
        ExpiresIn=300
    )
    return jsonify({'upload_url': upload_url, 'key': key})

@app.route('/facturas')
def ver_facturas():
    conexion = pg8000.native.Connection(
        user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT, database=DB_NAME
    )
    try:
        # s3_key ya se guarda desde la v1 (lambda_procesar_factura escribe
        # esta columna en cada INSERT -- ver la Lambda), asi que no hizo
        # falta ningun cambio de esquema ni de backfill para agregar esto:
        # el dato de todas las facturas ya procesadas estaba disponible,
        # solo no se estaba leyendo ni mostrando.
        filas_factura = conexion.run(
            """
            SELECT factura_id, proveedor_nombre, nit, fecha_factura, total, impuesto, fecha_procesado, s3_key, editado_manualmente,
                   telefono_proveedor, direccion_proveedor, comprador_nombre, telefono_comprador, direccion_comprador, numero_factura
            FROM factura ORDER BY fecha_procesado DESC
            """
        )
        facturas = []
        for fila in filas_factura:
            (
                factura_id, proveedor, nit, fecha_factura, total, impuesto, fecha_procesado, s3_key, editado_manualmente,
                telefono_proveedor, direccion_proveedor, comprador_nombre, telefono_comprador, direccion_comprador, numero_factura
            ) = fila
            filas_item = conexion.run(
                "SELECT descripcion, cantidad, precio_unitario, subtotal FROM item_factura WHERE factura_id = :fid",
                fid=factura_id
            )
            lineas = [
                {'descripcion': i[0], 'cantidad': i[1], 'precio_unitario': i[2], 'subtotal': i[3]}
                for i in filas_item
            ]
            # Genera una URL firmada temporal para ver la imagen original en
            # S3 -- el bucket no es publico, asi que sin esto el navegador
            # no podria abrir la imagen directamente. generate_presigned_url
            # no hace ninguna llamada de red (solo calcula una firma local),
            # asi que hacerlo una vez por factura en este bucle no tiene
            # costo de latencia real. Si por algun motivo una factura no
            # tiene s3_key guardado (no deberia pasar, pero por seguridad),
            # simplemente no se muestra el enlace para esa factura.
            imagen_url = None
            if s3_key:
                imagen_url = s3.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={'Bucket': BUCKET_NAME, 'Key': s3_key},
                    ExpiresIn=3600
                )
            facturas.append({
                'factura_id': factura_id,
                'proveedor_nombre': proveedor,
                'nit': nit,
                'fecha_factura': fecha_factura,
                'total': total,
                'impuesto': impuesto,
                'fecha_procesado': fecha_procesado,
                'editado_manualmente': editado_manualmente,
                'telefono_proveedor': telefono_proveedor,
                'direccion_proveedor': direccion_proveedor,
                'comprador_nombre': comprador_nombre,
                'telefono_comprador': telefono_comprador,
                'direccion_comprador': direccion_comprador,
                'numero_factura': numero_factura,
                'lineas': lineas,
                'imagen_url': imagen_url,
                'validacion': validar_total_factura(total, impuesto, lineas)
            })
        return render_template_string(PANEL_HTML, facturas=facturas, barra_nav=generar_barra_nav('/facturas'))
    finally:
        conexion.close()


# Formulario de edicion manual de una factura -- punto 1 de la lista de
# mejoras del 2026-09-02: permite corregir a mano un dato que Textract leyo
# mal (o que nunca detecto), en vez de dejarlo atrapado como estaba antes,
# solo visible en logs de CloudWatch o en el JSON crudo.
#
# GET: muestra el formulario precargado con lo que hay guardado ahora mismo.
# POST: guarda los cambios y marca la factura como editada_manualmente, para
# poder distinguir en /facturas un dato que viene de Textract de uno
# corregido a mano -- misma idea de trazabilidad que ya usa el resto del
# proyecto (NULL vs. 0, avisos en vez de silencio).
#
# Deliberadamente NO agrega ni quita filas de items en esta primera
# version -- ver el comentario de EDITAR_FACTURA_HTML.
@app.route('/facturas/<int:factura_id>/editar', methods=['GET', 'POST'])
def editar_factura(factura_id):
    conexion = pg8000.native.Connection(
        user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT, database=DB_NAME
    )
    try:
        if request.method == 'POST':
            conexion.run(
                """
                UPDATE factura
                SET proveedor_nombre = :proveedor_nombre,
                    nit = :nit,
                    fecha_factura = :fecha_factura,
                    total = :total,
                    impuesto = :impuesto,
                    telefono_proveedor = :telefono_proveedor,
                    direccion_proveedor = :direccion_proveedor,
                    comprador_nombre = :comprador_nombre,
                    telefono_comprador = :telefono_comprador,
                    direccion_comprador = :direccion_comprador,
                    numero_factura = :numero_factura,
                    editado_manualmente = TRUE,
                    fecha_ultima_edicion = NOW()
                WHERE factura_id = :factura_id
                """,
                proveedor_nombre=campo_texto_o_none(request.form.get('proveedor_nombre')),
                nit=campo_texto_o_none(request.form.get('nit')),
                fecha_factura=campo_fecha_o_none(request.form.get('fecha_factura')),
                total=campo_numerico_o_none(request.form.get('total')),
                impuesto=campo_numerico_o_none(request.form.get('impuesto')),
                telefono_proveedor=campo_texto_o_none(request.form.get('telefono_proveedor')),
                direccion_proveedor=campo_texto_o_none(request.form.get('direccion_proveedor')),
                comprador_nombre=campo_texto_o_none(request.form.get('comprador_nombre')),
                telefono_comprador=campo_texto_o_none(request.form.get('telefono_comprador')),
                direccion_comprador=campo_texto_o_none(request.form.get('direccion_comprador')),
                numero_factura=campo_texto_o_none(request.form.get('numero_factura')),
                factura_id=factura_id
            )

            # El WHERE incluye factura_id ademas de item_id -- no hace falta
            # para que esto funcione bien (item_id ya es unico por si solo),
            # pero evita que un item_id manipulado a mano en el formulario
            # (o repetido por error) pueda tocar una fila de otra factura.
            for item_id in request.form.getlist('item_id'):
                conexion.run(
                    """
                    UPDATE item_factura
                    SET descripcion = :descripcion,
                        cantidad = :cantidad,
                        precio_unitario = :precio_unitario,
                        subtotal = :subtotal
                    WHERE item_id = :item_id AND factura_id = :factura_id
                    """,
                    descripcion=campo_texto_o_none(request.form.get(f'descripcion_{item_id}')),
                    cantidad=campo_numerico_o_none(request.form.get(f'cantidad_{item_id}')),
                    precio_unitario=campo_numerico_o_none(request.form.get(f'precio_unitario_{item_id}')),
                    subtotal=campo_numerico_o_none(request.form.get(f'subtotal_{item_id}')),
                    item_id=int(item_id),
                    factura_id=factura_id
                )

            return redirect(url_for('ver_facturas'))

        fila = conexion.run(
            """
            SELECT proveedor_nombre, nit, fecha_factura, total, impuesto,
                   telefono_proveedor, direccion_proveedor,
                   comprador_nombre, telefono_comprador, direccion_comprador,
                   numero_factura
            FROM factura WHERE factura_id = :fid
            """,
            fid=factura_id
        )
        if not fila:
            return 'Factura no encontrada', 404

        (
            proveedor_nombre, nit, fecha_factura, total, impuesto,
            telefono_proveedor, direccion_proveedor,
            comprador_nombre, telefono_comprador, direccion_comprador,
            numero_factura
        ) = fila[0]
        factura = {
            'proveedor_nombre': proveedor_nombre,
            'nit': nit,
            # fecha_factura llega de RDS como un objeto date de Python;
            # se convierte a texto ISO ('AAAA-MM-DD') porque asi es como
            # el input type="date" del formulario espera su atributo value.
            'fecha_factura': fecha_factura.isoformat() if fecha_factura else None,
            'total': total,
            'impuesto': impuesto,
            'telefono_proveedor': telefono_proveedor,
            'direccion_proveedor': direccion_proveedor,
            'comprador_nombre': comprador_nombre,
            'telefono_comprador': telefono_comprador,
            'direccion_comprador': direccion_comprador,
            'numero_factura': numero_factura
        }

        filas_item = conexion.run(
            "SELECT item_id, descripcion, cantidad, precio_unitario, subtotal FROM item_factura WHERE factura_id = :fid ORDER BY item_id",
            fid=factura_id
        )
        items = [
            {
                'item_id': i[0], 'descripcion': i[1], 'cantidad': i[2],
                'precio_unitario': i[3], 'subtotal': i[4]
            }
            for i in filas_item
        ]

        return render_template_string(EDITAR_FACTURA_HTML, factura=factura, items=items, factura_id=factura_id, barra_nav=generar_barra_nav('/facturas'))
    finally:
        conexion.close()


# =====================================================================
# A PARTIR DE AQUI: codigo nuevo de la version 2 (panel de gestion).
# Nada de lo anterior se modifico. Todo lo de abajo lee de las mismas
# tablas (factura, item_factura) mas la tabla nueva venta_diaria.
# =====================================================================

# ---- Lado GASTO (datos que ya existen, capturados por OCR/Textract) ----

# Devuelve el gasto total por dia dentro de un rango de fechas (inclusive
# en ambos extremos). "dia" se calcula a partir de fecha_procesado (cuando
# se subio la factura), no de fecha_factura (la fecha impresa en el papel,
# que Textract a veces no detecta) -- ver la nota de la bitacora sobre por
# que se eligio asi. Pensada para la grafica de tendencia del panel.
def obtener_gasto_por_dia(conexion, fecha_inicio, fecha_fin):
    filas = conexion.run(
        """
        SELECT fecha_procesado::date AS dia, SUM(total) AS total_dia
        FROM factura
        WHERE fecha_procesado::date BETWEEN :inicio AND :fin
        GROUP BY fecha_procesado::date
        ORDER BY dia
        """,
        inicio=fecha_inicio,
        fin=fecha_fin
    )
    return [{'dia': str(fila[0]), 'total': float(fila[1])} for fila in filas]


# Devuelve las facturas (gastos) procesadas en un dia especifico.
# Pensada para la lista "gastos de hoy" del panel.
def obtener_facturas_de_un_dia(conexion, fecha):
    filas = conexion.run(
        """
        SELECT factura_id, proveedor_nombre, total, fecha_procesado
        FROM factura
        WHERE fecha_procesado::date = :fecha
        ORDER BY fecha_procesado DESC
        """,
        fecha=fecha
    )
    return [
        {
            'factura_id': fila[0],
            'proveedor_nombre': fila[1] or 'Proveedor no detectado',
            'total': float(fila[2]) if fila[2] is not None else None,
            'fecha_procesado': str(fila[3])
        }
        for fila in filas
    ]


# Devuelve un resumen de una sola fila: cuantas facturas y cuanto gasto
# total hay en un rango de fechas. Pensada para las cifras destacadas del
# panel (ej. "5 facturas, $120.000 de gasto esta semana").
def obtener_resumen_gasto(conexion, fecha_inicio, fecha_fin):
    fila = conexion.run(
        """
        SELECT COUNT(*) AS cantidad_facturas,
               COALESCE(SUM(total), 0) AS total_periodo
        FROM factura
        WHERE fecha_procesado::date BETWEEN :inicio AND :fin
        """,
        inicio=fecha_inicio,
        fin=fecha_fin
    )[0]
    return {'cantidad_facturas': fila[0], 'total_periodo': float(fila[1])}


# Devuelve los proveedores a los que mas se les ha comprado (por monto
# total) dentro de un rango de fechas. Ignora las facturas donde Textract
# no detecto el proveedor, porque agruparlas bajo "None" no aportaria
# informacion util.
#
# NOTA (encontrada probando esta consulta con datos reales): Textract a
# veces extrae el nombre del proveedor como un bloque de varias lineas
# (ej. "SMALL\nHE\nRO" en vez de "SMALL HE RO"). Si se agrupara por el
# texto exacto, el mismo proveedor quedaria partido en variantes distintas
# solo por los saltos de linea. Por eso se agrupa por una version
# "normalizada" del nombre: REGEXP_REPLACE(..., '\s+', ' ', 'g') convierte
# cualquier secuencia de espacios/saltos de linea en un solo espacio, y
# TRIM() quita espacios sobrantes al inicio/final.
# Esto NO resuelve el caso distinto en que Textract extrae el mismo
# proveedor real como dos textos genuinamente diferentes (ej. "La Esquina
# del Real" vs "del Real Lit Esquina Restaurant") -- eso requeriria una
# comparacion difusa entre nombres, que queda fuera de alcance por ahora y
# se documenta como limitacion conocida, igual que las limitaciones de
# Textract ya documentadas en la v1.
def obtener_top_proveedores(conexion, fecha_inicio, fecha_fin, limite=5):
    filas = conexion.run(
        """
        SELECT TRIM(REGEXP_REPLACE(proveedor_nombre, '\\s+', ' ', 'g')) AS proveedor,
               COALESCE(SUM(total), 0) AS total_gastado,
               COUNT(*) AS cantidad_facturas
        FROM factura
        WHERE fecha_procesado::date BETWEEN :inicio AND :fin
          AND proveedor_nombre IS NOT NULL
        GROUP BY TRIM(REGEXP_REPLACE(proveedor_nombre, '\\s+', ' ', 'g'))
        ORDER BY total_gastado DESC
        LIMIT :limite
        """,
        inicio=fecha_inicio,
        fin=fecha_fin,
        limite=limite
    )
    return [
        {'proveedor': fila[0], 'total_gastado': float(fila[1]), 'cantidad_facturas': fila[2]}
        for fila in filas
    ]


# Devuelve los items (productos o servicios) que mas se repiten dentro de
# un rango de fechas, cruzando item_factura con factura para poder filtrar
# por fecha. Util para detectar gasto recurrente (ej. "compro cajas todas
# las semanas").
def obtener_item_mas_frecuente(conexion, fecha_inicio, fecha_fin, limite=5):
    filas = conexion.run(
        """
        SELECT i.descripcion, COUNT(*) AS veces, SUM(i.subtotal) AS total_gastado
        FROM item_factura i
        JOIN factura f ON f.factura_id = i.factura_id
        WHERE f.fecha_procesado::date BETWEEN :inicio AND :fin
          AND i.descripcion IS NOT NULL
        GROUP BY i.descripcion
        ORDER BY veces DESC
        LIMIT :limite
        """,
        inicio=fecha_inicio,
        fin=fecha_fin,
        limite=limite
    )
    return [
        {
            'descripcion': fila[0],
            'veces': fila[1],
            'total_gastado': float(fila[2]) if fila[2] is not None else 0.0
        }
        for fila in filas
    ]


# Devuelve las facturas de un rango de fechas a las que les falto el
# proveedor o el total (fallas de deteccion de Textract). No es una
# pregunta de negocio sino de calidad de datos: le indica al usuario
# cuales facturas conviene revisar o volver a subir con mejor foto.
def obtener_facturas_incompletas(conexion, fecha_inicio, fecha_fin):
    filas = conexion.run(
        """
        SELECT factura_id, proveedor_nombre, total, fecha_procesado
        FROM factura
        WHERE fecha_procesado::date BETWEEN :inicio AND :fin
          AND (proveedor_nombre IS NULL OR total IS NULL)
        ORDER BY fecha_procesado DESC
        """,
        inicio=fecha_inicio,
        fin=fecha_fin
    )
    return [
        {
            'factura_id': fila[0],
            'proveedor_nombre': fila[1],
            'total': float(fila[2]) if fila[2] is not None else None,
            'fecha_procesado': str(fila[3])
        }
        for fila in filas
    ]


# Compara el gasto de los ultimos 30 dias contra los 30 dias anteriores a
# esos (dos ventanas moviles del mismo tamano, no meses calendario -- ver
# la explicacion en la bitacora sobre por que se eligio asi). "hoy" se
# recibe como parametro en vez de calcularse adentro, para que la funcion
# sea facil de probar con una fecha fija si hiciera falta.
def obtener_comparacion_periodos(conexion, hoy):
    fin_actual = hoy
    inicio_actual = hoy - timedelta(days=29)
    fin_anterior = inicio_actual - timedelta(days=1)
    inicio_anterior = fin_anterior - timedelta(days=29)

    resumen_actual = obtener_resumen_gasto(conexion, inicio_actual, fin_actual)
    resumen_anterior = obtener_resumen_gasto(conexion, inicio_anterior, fin_anterior)

    gasto_actual = resumen_actual['total_periodo']
    gasto_anterior = resumen_anterior['total_periodo']

    # Si el periodo anterior no tuvo gasto registrado, calcular un
    # porcentaje de variacion no tiene sentido (division por cero) -- se
    # devuelve None en vez de una cifra inventada.
    if gasto_anterior > 0:
        variacion_porcentual = ((gasto_actual - gasto_anterior) / gasto_anterior) * 100
    else:
        variacion_porcentual = None

    return {
        'periodo_actual': {
            'inicio': str(inicio_actual), 'fin': str(fin_actual), **resumen_actual
        },
        'periodo_anterior': {
            'inicio': str(inicio_anterior), 'fin': str(fin_anterior), **resumen_anterior
        },
        'variacion_porcentual': variacion_porcentual
    }


# ---- Lado VENTA (dato nuevo, ingresado a mano por el microempresario) ----

# Guarda o actualiza la venta total de un dia especifico. Si ya existia un
# registro para esa fecha (por la restriccion UNIQUE de la tabla), lo
# actualiza en vez de duplicarlo -- asi, si el usuario se equivoca y vuelve
# a registrar el mismo dia, corrige el valor en vez de sumarlo dos veces.
def registrar_venta_diaria(conexion, fecha, monto):
    conexion.run(
        """
        INSERT INTO venta_diaria (fecha, monto)
        VALUES (:fecha, :monto)
        ON CONFLICT (fecha) DO UPDATE
        SET monto = EXCLUDED.monto, fecha_registrado = NOW()
        """,
        fecha=fecha,
        monto=monto
    )


# Devuelve la venta registrada por dia dentro de un rango de fechas.
# Misma forma que obtener_gasto_por_dia(), para poder graficar ambas
# series juntas en el mismo eje de tiempo.
def obtener_venta_por_dia(conexion, fecha_inicio, fecha_fin):
    filas = conexion.run(
        "SELECT fecha, monto FROM venta_diaria WHERE fecha BETWEEN :inicio AND :fin ORDER BY fecha",
        inicio=fecha_inicio,
        fin=fecha_fin
    )
    return [{'dia': str(fila[0]), 'total': float(fila[1])} for fila in filas]


# Compara el gasto total (de facturas) contra la venta total (registrada a
# mano) de un mismo rango de fechas, y calcula un margen aproximado. Es
# deliberadamente una aproximacion simple (venta menos gasto), no un
# estado de resultados contable real -- para eso harian falta datos que
# este proyecto no captura (impuestos, costos fijos, etc.).
def obtener_comparacion_gasto_venta(conexion, fecha_inicio, fecha_fin):
    gasto_total = obtener_resumen_gasto(conexion, fecha_inicio, fecha_fin)['total_periodo']

    fila_venta = conexion.run(
        "SELECT COALESCE(SUM(monto), 0) FROM venta_diaria WHERE fecha BETWEEN :inicio AND :fin",
        inicio=fecha_inicio,
        fin=fecha_fin
    )[0]
    venta_total = float(fila_venta[0])

    margen_aproximado = venta_total - gasto_total

    # Igual que con la variacion porcentual: si no hay venta registrada
    # todavia, no se puede calcular que porcentaje representa el gasto
    # sobre la venta -- se devuelve None en vez de una cifra inventada.
    if venta_total > 0:
        porcentaje_gasto_sobre_venta = (gasto_total / venta_total) * 100
    else:
        porcentaje_gasto_sobre_venta = None

    return {
        'gasto_total': gasto_total,
        'venta_total': venta_total,
        'margen_aproximado': margen_aproximado,
        'porcentaje_gasto_sobre_venta': porcentaje_gasto_sobre_venta
    }


# ---- Ruta temporal de verificacion (se reemplaza por el panel visual) ----

# ---- Panel visual (Chart.js) ----

# Plantilla del panel de gestion. Usa Chart.js (cargado desde un CDN
# publico, cdnjs) para dibujar una sola grafica de lineas con dos series
# (gasto y venta por dia). El resto de la informacion (proveedores, items,
# facturas incompletas) se muestra como tablas HTML normales -- no todo
# necesita ser una grafica para ser util.
PANEL_GESTION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel de gestion</title>
    """ + CABECERA_TAILWIND + """
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-5xl px-4 py-8">
        <h1 class="text-xl font-bold text-gray-900">Panel de gestion</h1>

        <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
                <div class="text-xs font-medium uppercase tracking-wide text-gray-400">Gasto de hoy</div>
                <div class="mt-1 text-xl font-bold text-gray-900">${{ resumen_gasto_hoy.total_periodo | cop }}</div>
            </div>
            <div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
                <div class="text-xs font-medium uppercase tracking-wide text-gray-400">Venta de hoy</div>
                <div class="mt-1 text-xl font-bold text-gray-900">
                    {% if venta_hoy is not none %}${{ venta_hoy | cop }}{% else %}Sin registrar{% endif %}
                </div>
            </div>
            <div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
                <div class="text-xs font-medium uppercase tracking-wide text-gray-400">Margen aprox. (30 dias)</div>
                <div class="mt-1 text-xl font-bold {{ 'text-green-700' if comparacion_gasto_venta.margen_aproximado >= 0 else 'text-red-600' }}">
                    ${{ comparacion_gasto_venta.margen_aproximado | cop }}
                </div>
            </div>
            <div class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
                <div class="text-xs font-medium uppercase tracking-wide text-gray-400">Gasto vs. 30 dias anteriores</div>
                <div class="mt-1 text-xl font-bold text-gray-900">
                    {% if comparacion_periodos.variacion_porcentual is none %}
                        <span class="text-base font-medium text-gray-500">Sin datos previos</span>
                    {% else %}
                        {{ comparacion_periodos.variacion_porcentual | cop(1) }}%
                    {% endif %}
                </div>
            </div>
        </div>

        <h2 class="mt-8 text-sm font-semibold uppercase tracking-wide text-gray-500">Gasto vs. venta por dia (ultimos 30 dias)</h2>
        <div class="mt-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <canvas id="graficaGastoVenta" height="100"></canvas>
        </div>

        <h2 class="mt-8 text-sm font-semibold uppercase tracking-wide text-gray-500">Proveedores principales (ultimos 30 dias)</h2>
        {% if top_proveedores %}
        <div class="mt-3 overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
        <table class="w-full text-sm">
            <tr class="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-400">
                <th class="px-4 py-2 font-medium">Proveedor</th><th class="px-4 py-2 font-medium">Facturas</th><th class="px-4 py-2 font-medium">Total gastado</th>
            </tr>
            {% for p in top_proveedores %}
            <tr class="border-b border-gray-100 text-gray-700 last:border-0">
                <td class="px-4 py-2">{{ p.proveedor }}</td><td class="px-4 py-2">{{ p.cantidad_facturas }}</td><td class="px-4 py-2">${{ p.total_gastado | cop }}</td>
            </tr>
            {% endfor %}
        </table>
        </div>
        {% else %}
        <p class="mt-3 text-sm text-gray-500">Todavia no hay suficientes datos.</p>
        {% endif %}

        <h2 class="mt-8 text-sm font-semibold uppercase tracking-wide text-gray-500">Items mas frecuentes (ultimos 30 dias)</h2>
        {% if item_mas_frecuente %}
        <div class="mt-3 overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
        <table class="w-full text-sm">
            <tr class="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-400">
                <th class="px-4 py-2 font-medium">Descripcion</th><th class="px-4 py-2 font-medium">Veces</th><th class="px-4 py-2 font-medium">Total gastado</th>
            </tr>
            {% for it in item_mas_frecuente %}
            <tr class="border-b border-gray-100 text-gray-700 last:border-0">
                <td class="px-4 py-2">{{ it.descripcion }}</td><td class="px-4 py-2">{{ it.veces }}</td><td class="px-4 py-2">${{ it.total_gastado | cop }}</td>
            </tr>
            {% endfor %}
        </table>
        </div>
        {% else %}
        <p class="mt-3 text-sm text-gray-500">Todavia no hay suficientes datos.</p>
        {% endif %}

        {% if facturas_incompletas %}
        <h2 class="mt-8 text-sm font-semibold uppercase tracking-wide text-gray-500">Facturas para revisar</h2>
        <div class="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            {{ facturas_incompletas|length }} factura(s) de los ultimos 30 dias quedaron con el proveedor o el total sin detectar. Puede que valga la pena revisarlas o volver a subir la foto.
        </div>
        <div class="mt-3 overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
        <table class="w-full text-sm">
            <tr class="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-400">
                <th class="px-4 py-2 font-medium">ID</th><th class="px-4 py-2 font-medium">Proveedor</th><th class="px-4 py-2 font-medium">Total</th><th class="px-4 py-2 font-medium">Procesada</th>
            </tr>
            {% for f in facturas_incompletas %}
            <tr class="border-b border-gray-100 text-gray-700 last:border-0">
                <td class="px-4 py-2">{{ f.factura_id }}</td>
                <td class="px-4 py-2">{{ f.proveedor_nombre or '(sin detectar)' }}</td>
                <td class="px-4 py-2">{% if f.total is not none %}${{ f.total | cop }}{% else %}(sin detectar){% endif %}</td>
                <td class="px-4 py-2">{{ f.fecha_procesado }}</td>
            </tr>
            {% endfor %}
        </table>
        </div>
        {% endif %}

        <p class="mt-6 text-sm"><a href="/registrar-venta" class="font-medium text-blue-600 hover:underline">Registrar la venta de hoy &rarr;</a></p>
    </main>

    <script>
        // datosGrafica llega ya calculado desde Flask (una lista de 30
        // fechas y sus totales de gasto/venta) via el filtro |tojson,
        // que convierte el diccionario de Python en un objeto JS de forma
        // segura para insertarlo dentro de un <script>.
        const datosGrafica = {{ datos_grafica | tojson }};
        const ctx = document.getElementById('graficaGastoVenta');
        new Chart(ctx, {
            type: 'line',
            data: {
                labels: datosGrafica.dias,
                datasets: [
                    {
                        label: 'Gasto',
                        data: datosGrafica.gasto,
                        borderColor: '#c0392b',
                        backgroundColor: 'rgba(192, 57, 43, 0.1)',
                        tension: 0.2
                    },
                    {
                        label: 'Venta',
                        data: datosGrafica.venta,
                        borderColor: '#1a7a1a',
                        backgroundColor: 'rgba(26, 122, 26, 0.1)',
                        tension: 0.2
                    }
                ]
            },
            options: {
                responsive: true,
                scales: { y: { beginAtZero: true } }
            }
        });
    </script>
</body>
</html>
"""

# Ruta del panel de gestion: arma la pagina completa que va a usar el
# microempresario (antes se verifico esta misma logica con la ruta
# temporal /panel-debug, ya retirada -- ver nota mas abajo).
@app.route('/panel')
def panel():
    hoy = date.today()
    inicio_30 = hoy - timedelta(days=29)

    conexion = pg8000.native.Connection(
        user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT, database=DB_NAME
    )
    try:
        gasto_por_dia = obtener_gasto_por_dia(conexion, inicio_30, hoy)
        venta_por_dia = obtener_venta_por_dia(conexion, inicio_30, hoy)
        resumen_gasto_hoy = obtener_resumen_gasto(conexion, hoy, hoy)
        top_proveedores = obtener_top_proveedores(conexion, inicio_30, hoy)
        item_mas_frecuente = obtener_item_mas_frecuente(conexion, inicio_30, hoy)
        facturas_incompletas = obtener_facturas_incompletas(conexion, inicio_30, hoy)
        comparacion_periodos = obtener_comparacion_periodos(conexion, hoy)
        comparacion_gasto_venta = obtener_comparacion_gasto_venta(conexion, inicio_30, hoy)
    finally:
        conexion.close()

    # Para graficar gasto y venta en el mismo eje de tiempo, ambas series
    # necesitan un valor para cada uno de los 30 dias, aunque ese dia no
    # haya tenido gasto o venta -- si no, Chart.js desalinearia los puntos
    # de una serie respecto a la otra. Se arma un diccionario dia->total
    # por cada serie, y se recorren los 30 dias rellenando con 0 los que
    # no tengan dato.
    mapa_gasto = {fila['dia']: fila['total'] for fila in gasto_por_dia}
    mapa_venta = {fila['dia']: fila['total'] for fila in venta_por_dia}
    dias = [str(inicio_30 + timedelta(days=i)) for i in range(30)]
    datos_grafica = {
        'dias': dias,
        'gasto': [mapa_gasto.get(d, 0) for d in dias],
        'venta': [mapa_venta.get(d, 0) for d in dias],
    }

    # La venta de hoy se muestra en su propia tarjeta; si todavia no se ha
    # registrado, simplemente no va a estar en mapa_venta.
    venta_hoy = mapa_venta.get(str(hoy))

    return render_template_string(
        PANEL_GESTION_HTML,
        resumen_gasto_hoy=resumen_gasto_hoy,
        venta_hoy=venta_hoy,
        top_proveedores=top_proveedores,
        item_mas_frecuente=item_mas_frecuente,
        facturas_incompletas=facturas_incompletas,
        comparacion_periodos=comparacion_periodos,
        comparacion_gasto_venta=comparacion_gasto_venta,
        datos_grafica=datos_grafica,
        barra_nav=generar_barra_nav('/panel')
    )


# NOTA: la ruta temporal /panel-debug (que devolvia todas las consultas en
# JSON crudo) ya cumplio su proposito -- confirmar que los datos eran
# correctos antes de construir la plantilla visual -- y se retiro aqui,
# igual que se retiro el log temporal de la Lambda en la seccion 5.45 de
# la bitacora de la v1. El panel real (/panel, arriba) ya la reemplaza.


# Formulario minimo (sin estilos todavia) para registrar la venta total
# de un dia. GET muestra el formulario; POST guarda el dato y vuelve a
# mostrar el formulario con un mensaje de confirmacion o de error.
FORM_VENTA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Registrar venta del dia</title>
    """ + CABECERA_TAILWIND + """
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-md px-4 py-10">
        <div class="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
            <h1 class="text-xl font-bold text-gray-900">Registrar venta del dia</h1>
            {% if mensaje %}
                <p class="mt-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm font-medium text-gray-700">{{ mensaje }}</p>
            {% endif %}
            <form method="POST" class="mt-4 space-y-4">
                <label class="block text-sm font-medium text-gray-600">Fecha
                    <input type="date" name="fecha" value="{{ fecha_hoy }}" required
                        class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                </label>
                <label class="block text-sm font-medium text-gray-600">Venta total del dia (COP)
                    <input type="number" name="monto" step="0.01" min="0" required
                        class="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                </label>
                <button type="submit" class="w-full rounded-md bg-orange-500 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-orange-600">Guardar</button>
            </form>
        </div>
    </main>
</body>
</html>
"""

@app.route('/registrar-venta', methods=['GET', 'POST'])
def registrar_venta():
    mensaje = None
    if request.method == 'POST':
        fecha_texto = request.form.get('fecha')
        monto_texto = request.form.get('monto')
        try:
            # date.fromisoformat espera exactamente 'AAAA-MM-DD', que es el
            # formato que envia un <input type="date"> de HTML.
            fecha_valor = date.fromisoformat(fecha_texto)
            monto_valor = float(monto_texto)

            conexion = pg8000.native.Connection(
                user=DB_USER, password=DB_PASSWORD,
                host=DB_HOST, port=DB_PORT, database=DB_NAME
            )
            try:
                registrar_venta_diaria(conexion, fecha_valor, monto_valor)
            finally:
                conexion.close()

            mensaje = f"Venta del {fecha_valor} registrada: ${monto_valor:,.2f}"
        except (ValueError, TypeError):
            # Se captura aqui cualquier fecha o monto mal escrito (por
            # ejemplo, texto en vez de numero) para mostrar un mensaje
            # claro en vez de que la aplicacion se caiga con un error 500.
            mensaje = "Datos invalidos. Verifica la fecha y el monto."

    return render_template_string(
        FORM_VENTA_HTML,
        mensaje=mensaje,
        fecha_hoy=str(date.today()),
        barra_nav=generar_barra_nav('/registrar-venta')
    )


# =====================================================================
# ASISTENTE CONVERSACIONAL (Amazon Bedrock, patron tool use)
# =====================================================================

# Cliente de Bedrock Runtime (el servicio que efectivamente invoca los
# modelos -- "bedrock", sin "-runtime", es el servicio de administracion,
# no el de inferencia). Se crea una sola vez a nivel de modulo, igual que
# los otros clientes de boto3.
bedrock_runtime = boto3.client('bedrock-runtime', region_name='us-east-1')

# El "modelId" que espera la Converse API puede ser el ARN de un inference
# profile (no el ID plano del modelo) cuando el modelo lo exige -- este es
# justo ese caso, confirmado en la consola de Bedrock (Claude Haiku 4.5
# aparece como "Cross-region inference").
ID_MODELO_ASISTENTE = 'arn:aws:bedrock:us-east-1:<TU-CUENTA-AWS>:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0'

# Definicion de las "herramientas" que el modelo puede pedir usar. Cada una
# corresponde exactamente a una de las funciones de consulta ya definidas
# arriba -- no se le da al modelo ninguna capacidad nueva, solo se le
# describe lo que ya existe para que pueda elegir cual usar. Nada de esto
# incluye registrar_venta_diaria a proposito (ver la nota de diseno: el
# asistente es de solo lectura por ahora).
CONFIGURACION_HERRAMIENTAS = {
    'tools': [
        {
            'toolSpec': {
                'name': 'obtener_gasto_por_dia',
                'description': (
                    'Devuelve el gasto total (segun las facturas de proveedores) '
                    'de cada dia dentro de un rango de fechas. Util para ver la '
                    'tendencia del gasto en el tiempo.'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_facturas_de_un_dia',
                'description': 'Devuelve el detalle de las facturas (gastos) procesadas en un dia especifico.',
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha': {'type': 'string', 'description': 'La fecha exacta a consultar, formato AAAA-MM-DD'}
                    },
                    'required': ['fecha']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_resumen_gasto',
                'description': (
                    'Devuelve cuantas facturas y cuanto gasto total hubo dentro '
                    'de un rango de fechas. Es la forma mas directa de responder '
                    '"cuanto he gastado" en un periodo.'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_top_proveedores',
                'description': 'Devuelve los proveedores a los que mas se les ha comprado (por monto) dentro de un rango de fechas.',
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'},
                        'limite': {'type': 'integer', 'description': 'Cuantos proveedores devolver como maximo (por defecto 5)'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_item_mas_frecuente',
                'description': 'Devuelve los productos o servicios comprados con mas frecuencia dentro de un rango de fechas.',
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'},
                        'limite': {'type': 'integer', 'description': 'Cuantos items devolver como maximo (por defecto 5)'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_facturas_incompletas',
                'description': (
                    'Devuelve las facturas de un rango de fechas a las que les '
                    'falto el proveedor o el total (fallas de deteccion). Util '
                    'para preguntas sobre calidad de datos o "que facturas debo revisar".'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_venta_por_dia',
                'description': (
                    'Devuelve la venta total registrada a mano por el usuario, '
                    'por cada dia dentro de un rango de fechas.'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_comparacion_gasto_venta',
                'description': (
                    'Compara el gasto total contra la venta total registrada en un '
                    'rango de fechas, y calcula un margen aproximado (venta menos '
                    'gasto). Util para preguntas sobre ganancia o perdida.'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'properties': {
                        'fecha_inicio': {'type': 'string', 'description': 'Fecha de inicio del rango, formato AAAA-MM-DD'},
                        'fecha_fin': {'type': 'string', 'description': 'Fecha de fin del rango (inclusive), formato AAAA-MM-DD'}
                    },
                    'required': ['fecha_inicio', 'fecha_fin']
                }}
            }
        },
        {
            'toolSpec': {
                'name': 'obtener_comparacion_gasto_ultimos_30_dias',
                'description': (
                    'Compara el gasto de los ultimos 30 dias contra los 30 dias '
                    'anteriores a esos, y da el porcentaje de variacion. Util para '
                    '"esta subiendo mi gasto?" sin que el usuario tenga que dar fechas.'
                ),
                'inputSchema': {'json': {'type': 'object', 'properties': {}}}
            }
        }
    ]
}


# Cuantos mensajes anteriores de la conversacion (preguntas del usuario +
# respuestas finales del asistente, sin contar los mensajes intermedios de
# uso de herramientas) se le mandan a Bedrock como contexto de una pregunta
# nueva. Cada mensaje de historial que se manda es texto de entrada
# adicional para el modelo, y los tokens de entrada tienen costo (ver
# bitacora, seccion 5.9) -- este limite evita que una conversacion muy
# larga vaya encareciendo cada pregunta nueva sin limite. 10 mensajes
# equivalen a las ultimas 5 preguntas con sus 5 respuestas.
HISTORIAL_MAXIMO_MENSAJES = 10


# Arma el mensaje de sistema que se le manda al modelo en cada pregunta.
# Es una funcion (no una constante) porque la fecha de hoy cambia cada dia
# -- si fuera una constante calculada una sola vez al arrancar la
# aplicacion, quedaria "congelada" en el dia en que el servidor arranco.
def construir_instruccion_sistema():
    return (
        "Eres un asistente que ayuda a un microempresario colombiano a entender "
        "el gasto y la venta de su negocio, a partir de datos reales guardados "
        "en su base de datos.\n"
        f"La fecha de hoy es {date.today().isoformat()} (formato AAAA-MM-DD).\n"
        "Reglas estrictas que debes seguir siempre:\n"
        "1. Nunca inventes ni calcules tu mismo una cifra de gasto, venta, "
        "margen o cualquier numero relacionado con el negocio. Para CUALQUIER "
        "pregunta que involucre un numero de este tipo, SIEMPRE debes usar una "
        "de las herramientas disponibles y basar tu respuesta unicamente en el "
        "resultado real que te devuelva.\n"
        "2. Si necesitas un rango de fechas y el usuario uso una expresion "
        "relativa (\"esta semana\", \"el mes pasado\", \"hoy\"), calcula las "
        "fechas exactas tu mismo usando la fecha de hoy de arriba, y usa esas "
        "fechas al invocar la herramienta.\n"
        "3. Si la pregunta no se puede responder con las herramientas "
        "disponibles, dilo explicitamente en vez de inventar una respuesta.\n"
        "4. Responde siempre en espanol, de forma breve y clara, como si le "
        "hablaras al dueno de una tienda -- no uses jerga tecnica ni menciones "
        "nombres de funciones o de la base de datos.\n"
        "5. No puedes registrar ventas ni modificar ningun dato: si te piden "
        "eso, indica que hay que hacerlo desde la pagina de registrar venta.\n"
        "6. Es posible que abajo veas mensajes anteriores de esta misma "
        "conversacion. Si la pregunta actual hace referencia a algo "
        "mencionado antes (ej. \"y la semana pasada?\", \"compara eso con "
        "el mes anterior\"), usa ese contexto para entender a que se "
        "refiere, pero SIGUE aplicando la regla 1: cualquier cifra nueva "
        "que menciones debe venir de una herramienta invocada en esta "
        "respuesta, nunca copiada o recalculada a mano de un mensaje "
        "anterior.\n"
        "7. Si una herramienta te devuelve una lista de registros (por "
        "ejemplo, facturas de un dia) y tu respuesta los agrupa o resume "
        "(por ejemplo, por proveedor), verifica antes de responder que la "
        "suma de las cantidades de cada grupo sea igual a la cantidad "
        "total de registros que recibiste. Si no cuadra, vuelve a contar "
        "con cuidado en vez de reportar numeros inconsistentes -- un "
        "resumen agrupado mal contado es tan enganoso como una cifra "
        "inventada.\n"
        "8. Si la pregunta es sobre facturas con datos incompletos o sin "
        "detectar (proveedor o total faltante), usa la herramienta "
        "obtener_facturas_incompletas en vez de inferirlo tu mismo "
        "revisando una lista de facturas de otra herramienta -- esa "
        "herramienta ya cuenta ese caso exactamente con SQL, y contarlo "
        "tu mismo en la respuesta repite el mismo riesgo de la regla 7.\n"
        "9. Si la pregunta pide el detalle o la lista de facturas de un dia "
        "o periodo (por ejemplo \"que facturas se procesaron el...\"), y NO "
        "pide explicitamente un resumen, total o agrupacion, presenta esa "
        "lista tal cual -- proveedor, total y fecha de cada factura -- sin "
        "agruparla por proveedor ni calcular subtotales por tu cuenta. "
        "Armar una tabla agrupada que nadie pidio es la forma mas facil de "
        "cometer el error de la regla 7 sin darte cuenta.\n"
        "10. Si la pregunta si pide un total, un desglose por proveedor o "
        "item, o cualquier otro tipo de agregacion, NUNCA sumes o cuentes "
        "tu mismo los montos de una lista cruda de facturas -- usa la "
        "herramienta que ya calcula ese agregado con SQL (por ejemplo "
        "obtener_top_proveedores para desgloses por proveedor, o "
        "obtener_item_mas_frecuente para el item mas comprado). Sumar en "
        "prosa una lista cruda es como copiar a mano un calculo que una "
        "herramienta ya hizo bien -- el riesgo de asignarle un valor "
        "equivocado a una factura sin total detectado (violando la regla 1 "
        "de forma indirecta) es alto y ya paso antes.\n"
        "11. Esta conversacion se muestra en el navegador como texto plano, "
        "NO como markdown -- si usas tablas con \"|\", separadores \"---\", "
        "negritas con \"**\" o encabezados con \"#\", el usuario vera esos "
        "simbolos literalmente en la pantalla, no un formato bonito. Por "
        "eso nunca uses sintaxis de markdown. Para listas (por ejemplo de "
        "facturas), escribe un elemento por linea con saltos de linea "
        "simples, usando un guion o un numero al inicio de cada linea (ej. "
        "\"- Factura de Rojo Polo Paella Inc.: $199.650\"). Si quieres "
        "resaltar algo, usa palabras normales o mayusculas, nunca "
        "asteriscos ni otros simbolos de formato.\n"
        "12. Cualquier cifra en pesos colombianos que escribas debe usar "
        "el formato colombiano: el punto se usa como separador de miles, "
        "y NUNCA agregues decimales ni centavos (el peso colombiano no se "
        "usa en centavos en la practica). Por ejemplo, escribe $32.761 y "
        "$17.620, nunca $32.761.00 ni $32,761.00 ni $32761. Aplica esto en "
        "cada cifra de cada respuesta, sin excepcion, incluyendo listas y "
        "totales."
    )


# Convierte un texto 'AAAA-MM-DD' (lo que el modelo va a mandar como
# parametro) a un objeto date de Python. Se separa en su propia funcion
# para poder dar un mensaje de error claro si el modelo manda una fecha
# mal formada, en vez de que truene con un error críptico de Python.
def _interpretar_fecha(texto):
    try:
        return date.fromisoformat(texto)
    except (TypeError, ValueError):
        raise ValueError(f"Fecha invalida: '{texto}'. Debe tener el formato AAAA-MM-DD.")


# Punto unico de despacho: recibe el nombre de la herramienta que pidio el
# modelo y sus argumentos (ya parseados de JSON), y llama a la funcion de
# consulta real correspondiente. Cualquier herramienta no reconocida, o
# cualquier argumento invalido, levanta una excepcion -- el llamador
# (procesar_pregunta) la convierte en un "tool result" de error para que
# el modelo pueda reaccionar, en vez de que la peticion HTTP completa falle.
def ejecutar_herramienta(conexion, nombre, argumentos):
    if nombre == 'obtener_gasto_por_dia':
        return obtener_gasto_por_dia(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin'])
        )
    elif nombre == 'obtener_facturas_de_un_dia':
        return obtener_facturas_de_un_dia(conexion, _interpretar_fecha(argumentos['fecha']))
    elif nombre == 'obtener_resumen_gasto':
        return obtener_resumen_gasto(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin'])
        )
    elif nombre == 'obtener_top_proveedores':
        return obtener_top_proveedores(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin']),
            argumentos.get('limite', 5)
        )
    elif nombre == 'obtener_item_mas_frecuente':
        return obtener_item_mas_frecuente(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin']),
            argumentos.get('limite', 5)
        )
    elif nombre == 'obtener_facturas_incompletas':
        return obtener_facturas_incompletas(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin'])
        )
    elif nombre == 'obtener_venta_por_dia':
        return obtener_venta_por_dia(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin'])
        )
    elif nombre == 'obtener_comparacion_gasto_venta':
        return obtener_comparacion_gasto_venta(
            conexion,
            _interpretar_fecha(argumentos['fecha_inicio']),
            _interpretar_fecha(argumentos['fecha_fin'])
        )
    elif nombre == 'obtener_comparacion_gasto_ultimos_30_dias':
        return obtener_comparacion_periodos(conexion, date.today())
    else:
        raise ValueError(f"Herramienta desconocida: '{nombre}'")


# Logica central del asistente: recibe el historial de la conversacion
# (una lista de turnos anteriores, ya validada por la ruta /chat) y la
# pregunta nueva del usuario, y devuelve la respuesta final en texto
# plano, manejando por dentro todas las rondas de ida y vuelta con
# Bedrock que hagan falta (pregunta -> el modelo pide una herramienta ->
# se ejecuta -> se le devuelve el resultado -> el modelo responde, o pide
# otra herramienta...).
#
# NOTA DE DISENO IMPORTANTE: el historial no se guarda en el servidor (ni
# en memoria ni en la base de datos). Viaja completo en cada peticion HTTP
# desde el navegador, y esta funcion solo lo usa para armar los mensajes
# de esta llamada -- el servidor no recuerda nada entre una peticion y la
# siguiente. Esto es deliberado: hay dos instancias EC2 detras de un ALB
# que reparte peticiones por turnos (ver bitacora, secciones 5.7 y 5.13),
# y si el historial viviera en la memoria de una sola instancia, la
# siguiente pregunta del mismo usuario podria caer en la otra instancia y
# "olvidar" la conversacion. Guardarlo en el navegador evita ese problema
# por completo, sin necesidad de sesiones pegajosas (sticky sessions) ni
# de una tabla nueva en la base de datos.
def procesar_pregunta(historial, pregunta_usuario):
    # Cada turno del historial (ver formato validado en la ruta /chat) es
    # un diccionario simple {'role': 'user'|'assistant', 'texto': '...'}
    # -- se convierte aqui al formato que espera la Converse API de
    # Bedrock ({'role': ..., 'content': [{'text': ...}]}). Solo se toman
    # los ultimos HISTORIAL_MAXIMO_MENSAJES, para no encarecer cada
    # pregunta nueva con una conversacion indefinidamente larga.
    mensajes = [
        {'role': turno['role'], 'content': [{'text': turno['texto']}]}
        for turno in historial[-HISTORIAL_MAXIMO_MENSAJES:]
    ]
    mensajes.append({'role': 'user', 'content': [{'text': pregunta_usuario}]})

    conexion = pg8000.native.Connection(
        user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT, database=DB_NAME
    )
    try:
        # Limite de rondas: es una proteccion barata contra un ciclo que no
        # termine (el modelo pidiendo herramientas indefinidamente). Cinco
        # rondas son de sobra para cualquier pregunta razonable de este
        # panel -- la mayoria se resuelve en una sola.
        for _ in range(5):
            try:
                respuesta = bedrock_runtime.converse(
                    modelId=ID_MODELO_ASISTENTE,
                    system=[{'text': construir_instruccion_sistema()}],
                    messages=mensajes,
                    toolConfig=CONFIGURACION_HERRAMIENTAS,
                    inferenceConfig={'maxTokens': 500, 'temperature': 0}
                )
            except ClientError as error:
                # Errores de permisos, del ID del modelo, limites de la
                # cuenta, etc. -- se le muestra al usuario un mensaje
                # entendible en vez de un error 500 crudo. Solo se expone el
                # codigo del error (ej. "ValidationException"), no el
                # mensaje completo de AWS, para no mostrarle al usuario
                # final detalles tecnicos internos de la infraestructura.
                return f"No pude conectarme con el asistente en este momento ({error.response['Error']['Code']})."

            razon_de_parada = respuesta['stopReason']
            mensaje_modelo = respuesta['output']['message']
            mensajes.append(mensaje_modelo)

            if razon_de_parada != 'tool_use':
                # El modelo ya dio su respuesta final en texto -- se junta
                # todo el texto de la respuesta (normalmente es un solo
                # bloque, pero se recorren todos por seguridad).
                return ''.join(
                    bloque['text'] for bloque in mensaje_modelo['content'] if 'text' in bloque
                )

            # El modelo pidio usar una o mas herramientas en este turno.
            resultados_de_herramientas = []
            for bloque in mensaje_modelo['content']:
                if 'toolUse' not in bloque:
                    continue
                solicitud = bloque['toolUse']
                try:
                    resultado = ejecutar_herramienta(conexion, solicitud['name'], solicitud.get('input') or {})
                    # IMPORTANTE: el campo "json" de un toolResult en la Converse
                    # API de Bedrock debe ser un objeto (diccionario), no un
                    # arreglo -- si resultado es una lista (como devuelven varias
                    # de las funciones de consulta, ej. obtener_top_proveedores),
                    # Bedrock rechaza la peticion completa con ValidationException
                    # antes de que el modelo la vea. Por eso se envuelve siempre
                    # en un diccionario con la clave "resultado", sin importar si
                    # el valor original era una lista, un diccionario, o incluso
                    # None -- asi el nivel superior siempre es un objeto valido.
                    resultados_de_herramientas.append({
                        'toolResult': {
                            'toolUseId': solicitud['toolUseId'],
                            'content': [{'json': {'resultado': resultado}}]
                        }
                    })
                except Exception as error:
                    # Si la herramienta falla (fecha invalida, herramienta
                    # desconocida, etc.), se le informa el error al modelo
                    # -- puede intentar corregir el parametro, o explicarle
                    # al usuario que no pudo resolver la pregunta.
                    resultados_de_herramientas.append({
                        'toolResult': {
                            'toolUseId': solicitud['toolUseId'],
                            'content': [{'text': str(error)}],
                            'status': 'error'
                        }
                    })

            mensajes.append({'role': 'user', 'content': resultados_de_herramientas})

        # Se agotaron las rondas sin una respuesta final -- se corta aqui
        # para no dejar la peticion HTTP esperando indefinidamente.
        return "No pude terminar de procesar tu pregunta. Intenta reformularla de forma mas simple."
    finally:
        conexion.close()


# Ruta que recibe la pregunta del widget de chat (ver ASISTENTE_HTML), junto
# con el historial de la conversacion que el propio navegador viene
# acumulando, y devuelve la respuesta del asistente en JSON.
@app.route('/chat', methods=['POST'])
def chat():
    datos = request.get_json(silent=True) or {}
    pregunta = (datos.get('pregunta') or '').strip()
    if not pregunta:
        return jsonify({'respuesta': 'Escribe una pregunta primero.'}), 400

    # El historial viene del navegador (ver nota de diseno en
    # procesar_pregunta): nunca se debe confiar en que tenga exactamente
    # la forma esperada, asi que se filtra aqui cualquier turno mal
    # formado en vez de pasarselo tal cual a Bedrock -- un solo campo
    # invalido en el JSON del historial no debe tumbar la peticion con un
    # error 500.
    historial_bruto = datos.get('historial') or []
    historial_valido = [
        turno for turno in historial_bruto
        if isinstance(turno, dict)
        and turno.get('role') in ('user', 'assistant')
        and isinstance(turno.get('texto'), str)
    ]

    return jsonify({'respuesta': procesar_pregunta(historial_valido, pregunta)})


# Pagina del widget de chat. El historial de la conversacion se guarda
# solo en el navegador (en la variable JS "historial", no en el DOM) y se
# manda completo en cada peticion a /chat -- el backend es quien decide
# cuantos mensajes de ese historial realmente usa (ver
# HISTORIAL_MAXIMO_MENSAJES en el backend). Ver la nota de diseno junto a
# procesar_pregunta() sobre por que el historial no se guarda en el
# servidor.
ASISTENTE_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Asistente de negocio</title>
    """ + CABECERA_TAILWIND + """
</head>
<body class="min-h-screen bg-gray-50 font-sans text-gray-900">
    {{ barra_nav|safe }}
    <main class="mx-auto max-w-2xl px-4 py-8">
        <div class="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div class="flex items-center justify-between">
                <h1 class="text-lg font-bold text-gray-900">Preguntale a tu negocio</h1>
                <button class="rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50" onclick="nuevaConversacion()">Nueva conversacion</button>
            </div>
            <div id="conversacion" class="mt-4 min-h-[200px] space-y-1"></div>
            <div class="mt-4 flex gap-2">
                <input id="entrada" type="text" placeholder="Ej: cuanto he gastado esta semana?"
                    class="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 shadow-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500">
                <button class="rounded-md bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-orange-600" onclick="enviarPregunta()">Enviar</button>
            </div>
        </div>
    </main>

    <script>
        // El historial vive solo en esta pestana del navegador (se pierde
        // al recargar la pagina o cerrarla -- es una limitacion conocida
        // y aceptada de esta primera version, no un descuido). Cada
        // elemento es {role: 'user'|'assistant', texto: '...'}, el mismo
        // formato simple que valida la ruta /chat en el backend.
        let historial = [];

        // Enter tambien envia la pregunta, no solo el boton.
        document.getElementById('entrada').addEventListener('keydown', function (evento) {
            if (evento.key === 'Enter') enviarPregunta();
        });

        async function enviarPregunta() {
            const entrada = document.getElementById('entrada');
            const pregunta = entrada.value.trim();
            if (!pregunta) return;

            agregarMensaje('usuario', pregunta);
            entrada.value = '';

            const indicador = agregarMensaje('asistente', 'Pensando...');

            try {
                const respuestaHttp = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pregunta: pregunta, historial: historial })
                });
                const datos = await respuestaHttp.json();
                indicador.textContent = datos.respuesta;

                // Solo se agrega al historial DESPUES de una respuesta
                // exitosa -- si la peticion falla (catch de abajo), la
                // pregunta no queda "a medias" en el historial que se
                // manda la proxima vez.
                historial.push({ role: 'user', texto: pregunta });
                historial.push({ role: 'assistant', texto: datos.respuesta });
            } catch (error) {
                indicador.textContent = 'Hubo un error de conexion. Intenta de nuevo.';
            }
        }

        // Borra el historial en memoria y la conversacion visible, para
        // empezar de cero -- util tanto si el usuario quiere cambiar de
        // tema como para controlar el costo (una conversacion mas corta
        // manda menos texto de contexto en cada pregunta nueva).
        function nuevaConversacion() {
            historial = [];
            document.getElementById('conversacion').innerHTML = '';
        }

        function agregarMensaje(quien, texto) {
            const conversacion = document.getElementById('conversacion');
            const parrafo = document.createElement('p');
            // quien sigue siendo 'usuario' o 'asistente' (mismo valor de
            // siempre) -- lo unico que cambia es que ahora se traduce a
            // clases de Tailwind en vez de a un nombre de clase CSS propio.
            // Tailwind CDN observa el DOM y compila clases agregadas en
            // tiempo de ejecucion, asi que esto no requiere ningun paso
            // de build adicional.
            const clasesBase = 'my-1 max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm';
            parrafo.className = quien === 'usuario'
                ? clasesBase + ' ml-auto bg-blue-50 text-right text-gray-800'
                : clasesBase + ' bg-gray-100 text-gray-800';
            parrafo.textContent = texto;
            conversacion.appendChild(parrafo);
            return parrafo;
        }
    </script>
</body>
</html>
"""

@app.route('/asistente')
def asistente():
    return render_template_string(ASISTENTE_HTML, barra_nav=generar_barra_nav('/asistente'))


# =====================================================================
# Segundo canal del asistente: WhatsApp, via el WhatsApp Sandbox de
# Twilio. Prueba de concepto -- el Sandbox de Twilio es un entorno de
# pruebas (quien quiera escribirle al bot tiene que primero enviar un
# codigo "join" desde su propio WhatsApp), no un canal apto para
# usuarios finales reales todavia; para eso Twilio requiere verificacion
# de WhatsApp Business, un proceso fuera del control de este proyecto
# (ver bitacora, decision de canal del asistente).
#
# Reutiliza procesar_pregunta() tal cual -- la misma logica de
# herramientas/Bedrock que ya usa /chat. Lo unico nuevo aqui es la
# "puerta de entrada": donde /chat recibe el historial desde el
# navegador (ver nota de diseno junto a procesar_pregunta), WhatsApp no
# tiene navegador que lo guarde, asi que el historial se guarda en una
# tabla nueva de RDS, indexada por numero de telefono -- la misma idea
# de fondo (la base de datos compartida como fuente de verdad entre las
# dos instancias EC2, no la memoria de una sola instancia), aplicada al
# unico lugar donde SI hace falta que el servidor recuerde algo.
#
# Tabla nueva requerida (crearla una sola vez, via psql, antes de usar
# este canal):
#
#   CREATE TABLE whatsapp_historial (
#       id SERIAL PRIMARY KEY,
#       telefono TEXT NOT NULL,
#       rol TEXT NOT NULL CHECK (rol IN ('user', 'assistant')),
#       texto TEXT NOT NULL,
#       creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
#   );
#   CREATE INDEX idx_whatsapp_historial_telefono ON whatsapp_historial (telefono, creado_en);
# =====================================================================

# Devuelve los ultimos "limite" turnos de la conversacion de WhatsApp con
# un numero de telefono especifico, en el mismo formato que espera
# procesar_pregunta() ({'role': ..., 'texto': ...}), ordenados del mas
# antiguo al mas reciente (Bedrock espera el historial en ese orden).
def obtener_historial_whatsapp(conexion, telefono, limite):
    filas = conexion.run(
        """
        SELECT rol, texto FROM whatsapp_historial
        WHERE telefono = :telefono
        ORDER BY creado_en DESC
        LIMIT :limite
        """,
        telefono=telefono,
        limite=limite
    )
    return [{'role': fila[0], 'texto': fila[1]} for fila in reversed(filas)]


# Guarda un turno (del usuario o del asistente) en el historial de
# WhatsApp de un numero especifico.
def guardar_turno_whatsapp(conexion, telefono, rol, texto):
    conexion.run(
        "INSERT INTO whatsapp_historial (telefono, rol, texto) VALUES (:telefono, :rol, :texto)",
        telefono=telefono, rol=rol, texto=texto
    )


# Limite de turnos de historial para WhatsApp -- mismo valor que
# HISTORIAL_MAXIMO_MENSAJES (linea ~1058) para no encarecer cada mensaje
# con una conversacion indefinidamente larga, mismo criterio que /chat.
HISTORIAL_MAXIMO_MENSAJES_WHATSAPP = HISTORIAL_MAXIMO_MENSAJES

@app.route('/whatsapp-webhook', methods=['POST'])
def whatsapp_webhook():
    # "not firma_recibida" se revisa ANTES de llamar a validate(): si el
    # header no viene (ej. alguien llamando a esta URL directamente, sin
    # pasar por Twilio), el propio metodo validate() del SDK lanza una
    # excepcion en vez de devolver False -- se evita por completo
    # llamandolo solo cuando si hay una firma que comparar.
    firma_recibida = request.headers.get('X-Twilio-Signature')
    if not firma_recibida or not validador_twilio.validate(request.url, request.form.to_dict(), firma_recibida):
        # 403 generico, sin detalle -- no le da a un atacante ninguna
        # pista de por que fallo la validacion.
        return Response(status=403)

    telefono = request.form.get('From', '')
    pregunta = (request.form.get('Body') or '').strip()

    if not telefono or not pregunta:
        respuesta_texto = 'No recibi ningun mensaje de texto para responder.'
    else:
        conexion = pg8000.native.Connection(
            user=DB_USER, password=DB_PASSWORD,
            host=DB_HOST, port=DB_PORT, database=DB_NAME
        )
        try:
            historial = obtener_historial_whatsapp(conexion, telefono, HISTORIAL_MAXIMO_MENSAJES_WHATSAPP)
            respuesta_texto = procesar_pregunta(historial, pregunta)
            guardar_turno_whatsapp(conexion, telefono, 'user', pregunta)
            guardar_turno_whatsapp(conexion, telefono, 'assistant', respuesta_texto)
        finally:
            conexion.close()

    # TwiML: el formato XML que Twilio espera como respuesta para saber
    # que decirle de vuelta al usuario por WhatsApp. xml_escape evita que
    # un mensaje (la pregunta del usuario influye indirectamente en la
    # respuesta del modelo) con "<", ">" o "&" rompa el XML.
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Message>' + xml_escape(respuesta_texto) + '</Message></Response>'
    )
    return Response(twiml, mimetype='text/xml')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
