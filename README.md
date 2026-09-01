# Digitalización de facturas para microempresarios (AWS)

Proyecto de portafolio construido como parte del estudio para las
certificaciones AWS Cloud Practitioner / AI Practitioner. Resuelve un
problema real de microempresarios en Colombia: digitalizar facturas y
recibos en papel tomando solo una foto desde el celular, sin digitar nada a
mano, y ofrecerles después un panel de control y un asistente conversacional
sobre esos datos ya digitalizados.

El proyecto se construyó en dos etapas. La **versión 1** (pipeline de
captura y digitalización) quedó cerrada y verificada de punta a punta. La
**versión 2**, construida sobre esa misma base, agregó un panel visual de
indicadores y un asistente conversacional con IA generativa (Amazon
Bedrock) para consultar los datos en lenguaje natural.

## Qué hace

1. El usuario toma una foto de una factura desde una página web simple.
2. La foto se comprime en el navegador y se sube directo a Amazon S3
   mediante una URL prefirmada (nunca pasa por el servidor de la app).
3. La subida a S3 dispara automáticamente una función AWS Lambda.
4. La Lambda envía la imagen a Amazon Textract (`AnalyzeExpense`), que
   extrae el proveedor, la fecha, el total y cada ítem de la factura, con
   validaciones propias (no bloqueantes) de consistencia numérica.
5. Los datos ya estructurados se guardan en una base de datos PostgreSQL
   (Amazon RDS).
6. Un panel de consulta (`/facturas`) muestra todas las facturas procesadas,
   con sus ítems, directamente desde la base de datos — incluyendo una
   alerta visible cuando la suma de los ítems no reconcilia con el total
   de la factura (con o sin impuesto), calculada al momento de mostrar el
   panel, así que aplica también a facturas ya procesadas.
7. Un panel visual (`/panel`) resume el gasto (a partir de las facturas) y
   la venta diaria (registrada manualmente) con indicadores y una gráfica
   de tendencia (Chart.js).
8. Un asistente conversacional (`/asistente`), sobre Amazon Bedrock (Claude
   Haiku 4.5 vía un perfil de inferencia global), responde preguntas en
   lenguaje natural sobre el gasto, la venta y las facturas — siempre
   ejecutando una de un conjunto fijo de consultas SQL predefinidas contra
   RDS, nunca inventando una cifra por sí mismo.
9. El mismo asistente también responde por WhatsApp (`/whatsapp-webhook`),
   como prueba de concepto sobre el Sandbox de Twilio, con su propio
   historial de conversación por número de teléfono guardado en RDS.

## Arquitectura

![Diagrama de arquitectura (v1)](docs/diagrama_arquitectura.png)
![Diagrama de arquitectura (v2)](docs/diagrama_arquitectura_v2.png)

- **Red:** VPC con subredes públicas y privadas en dos zonas de
  disponibilidad, NAT instance para salida a internet desde la subred
  privada, Application Load Balancer público.
- **Cómputo:** Auto Scaling Group con Launch Template versionado; la
  aplicación Flask corre en un entorno virtual de Python aislado de las
  dependencias del sistema.
- **Almacenamiento y datos:** S3 para las imágenes originales, RDS
  (PostgreSQL) para los datos estructurados (facturas, ítems y venta diaria).
- **IA de extracción:** Amazon Textract (`AnalyzeExpense`) para leer la
  factura.
- **IA generativa:** Amazon Bedrock (Claude Haiku 4.5, vía perfil de
  inferencia global cross-region) para el asistente conversacional, con el
  patrón *tool use* de la Converse API — el modelo solo redacta texto a
  partir del resultado real de una consulta SQL, nunca calcula ni inventa
  una cifra financiera por su cuenta.
- **Credenciales:** la contraseña de RDS se guarda en SSM Parameter Store
  (SecureString) y se consulta en tiempo de ejecución — no está escrita en
  ningún archivo de código.
- **IAM:** los roles de la aplicación y de la Lambda están acotados a las
  acciones y recursos exactos que necesitan (sin políticas administradas
  genéricas de AWS), incluyendo el permiso puntual para invocar el perfil
  de inferencia de Bedrock.

## Estructura del repositorio

```
app/                            Aplicación Flask (sube fotos, panel de consulta, panel visual y asistente conversacional)
lambda/                         Función Lambda que llama a Textract, valida y guarda los datos en RDS
docs/                           Diagramas de arquitectura, bitácora de la v2 y limitaciones conocidas
```

## Documentación

- [`docs/BITACORA_V2.md`](docs/BITACORA_V2.md): bitácora técnica paso a
  paso de la construcción de la v2, incluyendo los bugs reales encontrados
  y su corrección.
- [`docs/LIMITACIONES_CONOCIDAS.md`](docs/LIMITACIONES_CONOCIDAS.md): ver
  sección siguiente.

## Cómo desplegarlo en tu propia cuenta

Este repositorio no incluye datos ni credenciales de ninguna cuenta de AWS.
Antes de desplegar, necesitas:

1. Una VPC con subredes públicas/privadas, un bucket S3, una base de datos
   RDS (PostgreSQL) con las tablas `factura`, `item_factura` y
   `venta_diaria`, y un parámetro SecureString en SSM Parameter Store con
   la contraseña de RDS (`/facturas-app/rds-password`).
   Opcional, solo si vas a habilitar el canal de WhatsApp: la tabla
   `whatsapp_historial` (ver `docs/BITACORA_V2.md`, sección 5.31) y un
   segundo parámetro SecureString con el Auth Token de una cuenta de
   Twilio (`/facturas-app/twilio-auth-token`) — en ambos casos, el rol de
   la instancia necesita permiso `ssm:GetParameter` sobre el parámetro
   nuevo, no solo sobre el de la contraseña de RDS.
2. En `app/app.py` y en `lambda/lambda_procesar_factura.py`, reemplazar
   `BUCKET_NAME`, `DB_HOST` y (solo en `app/app.py`) `ID_MODELO_ASISTENTE`
   con los valores reales de tu cuenta (están marcados en el código con
   comentarios indicando dónde encontrarlos en la consola de AWS).
3. Empaquetar `pg8000` (ver `lambda/requirements-layer.txt`) como una Lambda
   Layer, ya que no viene incluido en el runtime de Lambda por defecto.
4. Configurar el bucket S3 para disparar la Lambda ante eventos
   `s3:ObjectCreated` en el prefijo `entrada/`.
5. Solicitar acceso al modelo Claude Haiku 4.5 en la consola de Amazon
   Bedrock (Model access) y crear/usar un perfil de inferencia
   cross-region, para poder invocarlo desde la región donde despliegues.
6. Desplegar `app/app.py` (con las dependencias de
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
Textract, Bedrock, RDS (PostgreSQL), SSM Parameter Store, IAM.
Backend: Python, Flask, boto3, pg8000, Twilio (canal de WhatsApp).
Frontend: Chart.js (panel visual).
