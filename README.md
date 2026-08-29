# Digitalización de facturas para microempresarios (AWS)

Proyecto de portafolio construido como parte del estudio para las
certificaciones AWS Cloud Practitioner / AI Practitioner. Resuelve un
problema real de microempresarios en Colombia: digitalizar facturas y
recibos en papel tomando solo una foto desde el celular, sin digitar nada a
mano.

## Qué hace

1. El usuario toma una foto de una factura desde una página web simple.
2. La foto se comprime en el navegador y se sube directo a Amazon S3
   mediante una URL prefirmada (nunca pasa por el servidor de la app).
3. La subida a S3 dispara automáticamente una función AWS Lambda.
4. La Lambda envía la imagen a Amazon Textract (`AnalyzeExpense`), que
   extrae el proveedor, la fecha, el total y cada ítem de la factura.
5. Los datos ya estructurados se guardan en una base de datos PostgreSQL
   (Amazon RDS).
6. Un panel web (`/facturas`) muestra todas las facturas procesadas, con sus
   ítems, directamente desde la base de datos.

## Arquitectura

![Diagrama de arquitectura](docs/diagrama_arquitectura.png)

- **Red:** VPC con subredes públicas y privadas en dos zonas de
  disponibilidad, NAT instance para salida a internet desde la subred
  privada, Application Load Balancer público.
- **Cómputo:** Auto Scaling Group con Launch Template versionado; la
  aplicación Flask corre en un entorno virtual de Python aislado de las
  dependencias del sistema.
- **Almacenamiento y datos:** S3 para las imágenes originales, RDS
  (PostgreSQL) para los datos estructurados.
- **IA:** Amazon Textract (`AnalyzeExpense`) para leer la factura.
- **Credenciales:** la contraseña de RDS se guarda en SSM Parameter Store
  (SecureString) y se consulta en tiempo de ejecución — no está escrita en
  ningún archivo de código.
- **IAM:** los roles de la aplicación y de la Lambda están acotados a las
  acciones y recursos exactos que necesitan (sin políticas administradas
  genéricas de AWS).

## Estructura del repositorio

```
app/                            Aplicación Flask (sube fotos, muestra el panel de consulta)
lambda/                         Función Lambda que llama a Textract y guarda los datos en RDS
docs/                           Diagrama de arquitectura y limitaciones conocidas
```

## Cómo desplegarlo en tu propia cuenta

Este repositorio no incluye datos ni credenciales de ninguna cuenta de AWS.
Antes de desplegar, necesitas:

1. Una VPC con subredes públicas/privadas, un bucket S3, una base de datos
   RDS (PostgreSQL) con las tablas `factura` e `item_factura`, y un
   parámetro SecureString en SSM Parameter Store con la contraseña de RDS
   (`/facturas-app/rds-password`).
2. En `app/app.py` y en `lambda/lambda_procesar_factura.py`, reemplazar
   `BUCKET_NAME` y `DB_HOST` con los valores reales de tu cuenta (están
   marcados en el código con comentarios).
3. Empaquetar `pg8000` (ver `lambda/requirements-layer.txt`) como una Lambda
   Layer, ya que no viene incluido en el runtime de Lambda por defecto.
4. Configurar el bucket S3 para disparar la Lambda ante eventos
   `s3:ObjectCreated` en el prefijo `entrada/`.
5. Desplegar `app/app.py` (con las dependencias de
   `app/requirements.txt`) en las instancias del Auto Scaling Group, detrás
   del Application Load Balancer.

## Limitaciones conocidas

Ver [`docs/LIMITACIONES_CONOCIDAS.md`](docs/LIMITACIONES_CONOCIDAS.md) para
el detalle de dos hallazgos de la etapa de extracción con Textract: un
defecto real ya corregido (símbolos de moneda y unidades en campos
numéricos) y una limitación del servicio de OCR con recibos de formato de
dos líneas por ítem, documentada y aceptada conscientemente en vez de
resuelta.

## Tecnologías

AWS: VPC, EC2, Auto Scaling Group, Application Load Balancer, S3, Lambda,
Textract, RDS (PostgreSQL), SSM Parameter Store, IAM.
Backend: Python, Flask, boto3, pg8000.
