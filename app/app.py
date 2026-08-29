from flask import Flask, request, jsonify, render_template_string
import boto3
import uuid
from botocore.client import Config
import pg8000.native

app = Flask(__name__)
s3 = boto3.client('s3', config=Config(signature_version='s3v4'))

# Cliente de SSM Parameter Store -- se crea una sola vez, a nivel de modulo,
# para no reconstruirlo en cada peticion HTTP.
ssm = boto3.client('ssm', region_name='us-east-1')

# --- Valores especificos de esta instalacion -------------------------------
# Reemplaza estos dos valores con los de tu propia cuenta de AWS antes de
# desplegar. Ninguno de los dos es un secreto (no dan acceso por si solos:
# el bucket exige credenciales de IAM y RDS esta en una subred privada sin
# acceso publico), pero son identificadores propios de una cuenta real y no
# se dejan como valores fijos en un repositorio publico.
BUCKET_NAME = 'facturas-microempresarios-<TU-CUENTA-AWS>'  # Consola S3 > nombre del bucket
DB_HOST = '<TU-ENDPOINT-RDS>.rds.amazonaws.com'            # Consola RDS > Bases de datos > Punto de enlace (endpoint)
# -----------------------------------------------------------------------

DB_PORT = 5432
DB_NAME = 'facturas'
DB_USER = 'postgres'

# La contraseña no esta escrita aqui: se consulta a SSM Parameter Store una
# sola vez, cuando la aplicacion arranca. WithDecryption=True le dice a SSM
# que use la llave KMS asociada para devolver el valor real. El parametro
# '/facturas-app/rds-password' debe existir en tu cuenta como SecureString
# antes de arrancar la aplicacion.
DB_PASSWORD = ssm.get_parameter(
    Name='/facturas-app/rds-password',
    WithDecryption=True
)['Parameter']['Value']

# HTML de la pagina principal: un formulario simple para tomar/seleccionar
# una foto de la factura, comprimirla en el navegador y subirla a S3.
PAGINA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Digitalizar Factura</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 420px; margin: 40px auto; padding: 0 16px; }
        h1 { font-size: 22px; }
        input[type=file] { display: block; margin: 20px 0; }
        button { background: #FF9900; border: none; padding: 12px 20px; font-size: 16px; border-radius: 6px; }
        #estado { margin-top: 16px; font-weight: bold; }
        .enlace-panel { display: block; margin-top: 24px; color: #0066cc; }
    </style>
</head>
<body>
    <h1>Digitalizar factura</h1>
    <p>Toma una foto de tu factura o recibo:</p>
    <input type="file" id="archivo" accept="image/*" capture="environment">
    <button onclick="subirFactura()">Subir factura</button>
    <div id="estado"></div>
    <a class="enlace-panel" href="/facturas">Ver facturas procesadas &rarr;</a>
    <script>
        // Redimensiona la imagen a un ancho maximo de 1600px y la reexporta
        // como JPEG de calidad 0.8, para no subir fotos de varios MB desde
        // el celular y acelerar tanto la subida como el procesamiento.
        function comprimirImagen(archivo) {
            return new Promise((resolve) => {
                const img = new Image();
                const lector = new FileReader();
                lector.onload = (e) => {
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
                lector.readAsDataURL(archivo);
            });
        }
        // Flujo completo de subida: comprime la imagen, pide una URL
        // prefirmada de S3 al backend, y sube el archivo directo a S3 con
        // esa URL -- el archivo nunca pasa por este servidor Flask.
        async function subirFactura() {
            const input = document.getElementById('archivo');
            const estado = document.getElementById('estado');
            if (!input.files.length) {
                estado.textContent = 'Primero selecciona o toma una foto.';
                return;
            }
            const archivoOriginal = input.files[0];
            estado.textContent = 'Optimizando imagen...';
            const archivo = await comprimirImagen(archivoOriginal);
            estado.textContent = 'Preparando subida...';
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

# HTML del panel de consulta: recorre las facturas guardadas en RDS (mas
# recientes primero) y, para cada una, su tabla de items.
PANEL_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Facturas procesadas</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 700px; margin: 30px auto; padding: 0 16px; }
        h1 { font-size: 22px; }
        .factura { border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
        .factura h3 { margin: 0 0 6px 0; }
        .factura .total { color: #FF9900; font-weight: bold; font-size: 18px; }
        .factura table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
        .factura th, .factura td { text-align: left; padding: 4px 6px; border-bottom: 1px solid #eee; }
        .vacio { color: #777; }
        .volver { display: inline-block; margin-bottom: 20px; }
    </style>
</head>
<body>
    <a class="volver" href="/">&larr; Volver a subir factura</a>
    <h1>Facturas procesadas ({{ facturas|length }})</h1>
    {% if not facturas %}
        <p class="vacio">Todavia no hay facturas procesadas.</p>
    {% endif %}
    {% for factura in facturas %}
    <div class="factura">
        <h3>{{ factura.proveedor_nombre or 'Proveedor no detectado' }}</h3>
        <p>Procesada: {{ factura.fecha_procesado }}</p>
        <p class="total">Total: {{ factura.total }}</p>
        <table>
            <tr><th>Descripcion</th><th>Cant.</th><th>Precio</th><th>Subtotal</th></tr>
            {% for item in factura.lineas %}
            <tr>
                <td>{{ item.descripcion }}</td>
                <td>{{ item.cantidad }}</td>
                <td>{{ item.precio_unitario }}</td>
                <td>{{ item.subtotal }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    {% endfor %}
</body>
</html>
"""

# Pagina principal: sirve el formulario de subida.
@app.route('/')
def index():
    return render_template_string(PAGINA_HTML)

# Genera una URL prefirmada de S3 (valida 5 minutos) para que el navegador
# suba el archivo directamente a S3, sin pasar por este servidor -- este
# endpoint solo firma la URL, nunca recibe el archivo en si.
@app.route('/get-upload-url')
def get_upload_url():
    nombre_original = request.args.get('filename', 'factura.jpg')
    extension = nombre_original.split('.')[-1]
    # Genera un nombre unico (UUID) para no sobreescribir archivos si dos
    # usuarios suben una foto con el mismo nombre original.
    key = f"entrada/{uuid.uuid4()}.{extension}"
    upload_url = s3.generate_presigned_url(
        ClientMethod='put_object',
        Params={'Bucket': BUCKET_NAME, 'Key': key},
        ExpiresIn=300
    )
    return jsonify({'upload_url': upload_url, 'key': key})

# Panel de consulta: lee de RDS todas las facturas ya procesadas por la
# Lambda, junto con sus items, y las muestra en una pagina HTML simple.
@app.route('/facturas')
def ver_facturas():
    conexion = pg8000.native.Connection(
        user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT, database=DB_NAME
    )
    try:
        filas_factura = conexion.run(
            "SELECT factura_id, proveedor_nombre, total, fecha_procesado FROM factura ORDER BY fecha_procesado DESC"
        )
        facturas = []
        for fila in filas_factura:
            factura_id, proveedor, total, fecha_procesado = fila
            filas_item = conexion.run(
                "SELECT descripcion, cantidad, precio_unitario, subtotal FROM item_factura WHERE factura_id = :fid",
                fid=factura_id
            )
            lineas = [
                {'descripcion': i[0], 'cantidad': i[1], 'precio_unitario': i[2], 'subtotal': i[3]}
                for i in filas_item
            ]
            facturas.append({
                'proveedor_nombre': proveedor,
                'total': total,
                'fecha_procesado': fecha_procesado,
                'lineas': lineas
            })
        return render_template_string(PANEL_HTML, facturas=facturas)
    finally:
        conexion.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
