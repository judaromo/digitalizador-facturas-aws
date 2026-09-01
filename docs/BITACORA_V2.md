# Bitácora de construcción — Versión 2

Registro técnico, paso a paso, de la construcción de la versión 2 del
proyecto (panel de control `/panel` y asistente conversacional `/asistente`
sobre Amazon Bedrock), incluyendo los bugs reales encontrados y su
corrección. Es la continuación de la v1 -- ver el [README](../README.md)
para la arquitectura general y el [diagrama de arquitectura v2]
(diagrama_arquitectura_v2.png).

## 5. Bitácora de construcción — paso a paso

### 5.1 Planeación de la versión 2: investigación y decisiones de diseño

Con la v1 cerrada, verificada y publicada, se abrió la planeación de la v2 a partir de una petición deliberadamente amplia: una plataforma de gestión de negocio que ayudara a los microempresarios a diferenciarse en el mercado colombiano. Antes de proponer funcionalidades, se investigó el contexto real en el que operaría esa plataforma (ver detalle completo en la sección 1): el umbral normativo de facturación electrónica de la DIAN, el panorama competitivo (Alegra, Siigo, Bold), los dolores reales reportados por pymes colombianas en una encuesta reciente, el crecimiento de WhatsApp como canal de mensajería empresarial, y el modelo de precios de Amazon QuickSight como alternativa de panel.

Con esa base, se presentaron tres opciones (sección 1: ampliar captura de datos, agregar una capa de inteligencia sobre los datos ya capturados, o construir una plataforma integral competidora) junto con la objeción de que la tercera opción equivale a reconstruir, desde cero, lo que actores ya establecidos resuelven de forma madura -- una decisión de escala de startup, no de arquitectura técnica de portafolio. El usuario aceptó esa objeción y eligió la segunda opción, precisándola con dos requisitos propios: un panel simple de ventas/facturas con gráficas, y un asistente conversacional, idealmente con posibilidad de integrarse con WhatsApp.

Se resolvieron dos decisiones técnicas concretas antes de empezar a construir:

### Decisión: tecnología del panel

Se evaluó Amazon QuickSight frente a construir el panel a mano con Flask y Chart.js. QuickSight tiene costo por sesión, y embeberlo para usuarios finales anónimos requeriría una conexión VPC adicional hacia RDS y generación programática de URLs de incrustación -- complejidad real para las pocas gráficas simples que se necesitan en esta etapa. Se eligió Flask + Chart.js: reutiliza directamente la aplicación y el acceso a RDS que ya existen, sin costo por sesión ni infraestructura adicional.

### Decisión: canal del asistente conversacional

El usuario preguntó directamente si valía la pena integrar WhatsApp o si era mejor un chatbot dentro de la propia aplicación, y qué impacto real tendría cada uno. La respuesta no es una disyuntiva: la lógica del asistente (interpretación de la pregunta con Bedrock, ejecución de una consulta SQL predefinida, redacción de la respuesta a partir del resultado real) es idéntica sin importar el canal; solo cambia la "puerta de entrada" por la que llega el mensaje. Construir primero el chatbot interno permite demostrar toda esa lógica sin depender de la verificación de Meta Business, cuyo tiempo no está bajo control del proyecto. WhatsApp queda como una segunda puerta de entrada opcional, a agregar más adelante usando el WhatsApp Sandbox de Twilio -- que permite probar en minutos sin esperar esa verificación, aunque no es apto para producción.

### Principio de diseño no negociable: uso de herramientas, no generación libre

El riesgo central de cualquier asistente conversacional sobre datos financieros es la alucinación: que el modelo de lenguaje invente o calcule mal una cifra a partir de su propio razonamiento, en vez de basarse en el dato real. Por eso el diseño de esta v2 establece, desde ahora, una regla fija: Amazon Bedrock nunca genera un número financiero por sí mismo. El patrón correcto es tool use / function calling: Bedrock interpreta la pregunta en lenguaje natural y decide cuál de un conjunto pequeño y predefinido de consultas SQL de agregación ejecutar, y con qué parámetros (por ejemplo, un rango de fechas); una función real del backend ejecuta esa consulta exacta contra RDS; Bedrock solo redacta la respuesta final en lenguaje natural a partir del resultado real de esa consulta. Este es, además, exactamente el mismo concepto (tool use frente a generación libre / RAG) que evalúa la certificación AWS AI Practitioner, por lo que documentarlo con cuidado en esta bitácora tiene valor doble: para el proyecto y para el estudio de la certificación.

Con estas decisiones tomadas, el siguiente paso (no iniciado todavía) es empezar la construcción: definir el conjunto concreto de consultas SQL permitidas, construir las rutas del panel, e integrar Bedrock con esas herramientas.

### 5.2 Aclaración del modelo de datos: gasto vs. venta, y la tabla venta_diaria

Antes de escribir cualquier consulta, surgió una ambigüedad importante en el modelo de datos: las facturas de prueba digitalizadas en la v1 son facturas de proveedores -- es decir, representan el gasto del microempresario, no su venta. El plan inicial de preguntas para el panel y el asistente asumía, sin decirlo explícitamente, que existía algún dato de venta ya capturado. No existía.

El usuario resolvió esta ambigüedad con una observación clave sobre el público objetivo: un microempresario tipo tienda de barrio no factura electrónicamente cada venta individual (ver sección 1, umbral de la DIAN), pero sí sabe, al cerrar el día, cuánto vendió en total. En vez de intentar capturar facturas de venta -- que en la práctica no existen para este público --, se diseñó un campo mucho más simple: un formulario donde el usuario ingresa manualmente el total vendido de un día. Esto es, además, exactamente el tipo de dato mínimo viable que la encuesta de pymes citada en la sección 1 sugiere que este público sí está dispuesto y en capacidad de registrar.

Con esa aclaración, se diseñó la tabla venta_diaria, verificada en RDS mediante psql (\d venta_diaria):

- fecha DATE, con restricción UNIQUE: un solo registro de venta por día.
- monto NUMERIC: el total vendido ese día, ingresado a mano.
- fecha_registrado TIMESTAMP: momento en que se guardó o actualizó el registro (no la fecha de la venta, sino la fecha de captura del dato).
La restricción UNIQUE sobre fecha existe para permitir un patrón de "upsert" (INSERT ... ON CONFLICT (fecha) DO UPDATE): si el usuario se equivoca y vuelve a registrar la venta del mismo día, el sistema corrige el valor en vez de duplicarlo o sumarlo por error.

### 5.3 Diseño de las diez consultas ("herramientas") compartidas entre el panel y el asistente

Se le pidió al usuario aprobar no solo el mínimo de preguntas necesarias para el panel, sino una evaluación más amplia de qué otras preguntas serían realmente útiles para un microempresario. De esa evaluación conjunta surgieron diez funciones de consulta, cada una con una sola responsabilidad, diseñadas desde el inicio para ser reutilizadas tanto por las rutas del panel visual como por las "herramientas" que más adelante invocaría Amazon Bedrock -- así la lógica de negocio vive en un solo lugar, no duplicada entre el panel y el asistente.

### Lado gasto (datos ya capturados por Textract en la v1)

- **obtener_gasto_por_dia. **Gasto total agrupado por día dentro de un rango de fechas -- pensada para graficar una tendencia.
- **obtener_facturas_de_un_dia. **Detalle de las facturas procesadas en una fecha específica.
- **obtener_resumen_gasto. **Cantidad de facturas y gasto total en un rango -- la forma más directa de responder "cuánto he gastado".
- **obtener_top_proveedores. **Proveedores a los que más se les ha comprado, por monto, dentro de un rango.
- **obtener_item_mas_frecuente. **Productos o servicios comprados con más frecuencia -- útil para detectar gasto recurrente.
- **obtener_facturas_incompletas. **Facturas a las que Textract no les detectó proveedor o total -- una pregunta de calidad de datos, no de negocio: le indica al usuario cuáles facturas conviene revisar.
- **obtener_comparacion_periodos. **Compara el gasto de los últimos 30 días contra los 30 días anteriores a esos.
### Lado venta (dato nuevo, ingresado a mano)

- **registrar_venta_diaria. **Guarda o actualiza (upsert) la venta total de un día.
- **obtener_venta_por_dia. **Venta registrada por día dentro de un rango -- misma forma que obtener_gasto_por_dia, para poder graficar ambas series juntas.
- **obtener_comparacion_gasto_venta. **Compara gasto total contra venta total de un rango y calcula un margen aproximado (venta menos gasto).
Decisión de diseño: las comparaciones de periodos (5.3, obtener_comparacion_periodos, y más adelante el resumen de 30 días del asistente) usan ventanas móviles de 30 días -- los últimos 30 días contra los 30 días inmediatamente anteriores -- en vez de meses calendario. Un mes calendario tiene una cantidad variable de días (28 a 31) y el mes en curso casi nunca está completo cuando se consulta, lo que distorsionaría cualquier comparación. Una ventana móvil de tamaño fijo evita ese problema por construcción.

registrar_venta_diaria se excluyó deliberadamente del conjunto de herramientas que más adelante se le expondría a Bedrock (sección 5.9): el asistente conversacional se diseñó de solo lectura a propósito, para no darle a un modelo de lenguaje la capacidad de modificar datos del negocio a partir de una instrucción en lenguaje natural.

### 5.4 Construcción del panel visual (/panel) con Chart.js

El panel de gestión (ruta /panel) muestra cuatro tarjetas de indicadores (gasto de hoy, venta de hoy, margen aproximado de los últimos 30 días, variación del gasto contra los 30 días anteriores), una gráfica de líneas de Chart.js con gasto y venta día a día, y tablas de proveedores principales, ítems más frecuentes y facturas incompletas -- todo dentro de los últimos 30 días. Para que las dos series de la gráfica (gasto y venta) queden alineadas en el mismo eje de tiempo, se construye un diccionario día-total para cada serie y se recorren los 30 días rellenando con 0 los que no tengan dato; de lo contrario, un día sin venta registrada desplazaría los puntos de esa serie respecto a la de gasto.

Antes de construir la plantilla visual, se agregó una ruta temporal /panel-debug que devolvía el resultado crudo en JSON de todas las consultas nuevas, para verificar con datos reales que la lógica SQL era correcta antes de invertir tiempo en el HTML/CSS. Una vez confirmado, se retiró -- el mismo patrón ya usado en la v1 (sección 5.45 de ese documento) para el log temporal de la Lambda.

### Error encontrado: 502 Bad Gateway por una condición de carrera en el despliegue

La primera vez que se probó /panel-debug tras desplegar el código nuevo, la aplicación devolvió 502 Bad Gateway. El diagnóstico, vía journalctl -u facturas-app -n 50 sobre la instancia (conectada por EC2 Instance Connect), reveló una condición de carrera: el comando systemctl restart se ejecutó antes de que aws s3 cp terminara de escribir el nuevo app.py en /opt/app/app.py, por lo que systemd intentó arrancar un archivo a medio escribir, falló repetidamente y activó su límite de reinicios rápidos, dejando el servicio detenido. Para cuando se revisaron los logs, un reinicio manual posterior ya lo había recuperado.

**Corrección de proceso: **en todo despliegue manual posterior, se exige confirmar el mensaje de éxito de aws s3 cp antes de ejecutar systemctl restart, en vez de encadenar ambos comandos sin verificar el primero.

### 5.5 Bug de datos reales: nombres de proveedor fragmentados por saltos de línea

Probando obtener_top_proveedores con datos reales vía /panel-debug, el mismo proveedor aparecía dos veces en la lista: una vez como "SMALL HE RO" y otra como "SMALL\nHE\nRO" (con saltos de línea literales). La causa es que Textract, al extraer bloques de texto de varias líneas, a veces conserva los saltos de línea originales del recibo dentro del campo de nombre del proveedor -- si se agrupa por el texto exacto, el mismo proveedor queda partido en variantes distintas solo por esos saltos de línea.

**Corrección: **agrupar por una versión normalizada del nombre -- TRIM(REGEXP_REPLACE(proveedor_nombre, '\s+', ' ', 'g')) -- que convierte cualquier secuencia de espacios o saltos de línea en un solo espacio y recorta los sobrantes al inicio y al final.

Esta corrección NO resuelve un problema distinto y más difícil, documentado deliberadamente como limitación conocida: cuando Textract extrae el mismo proveedor real como dos textos genuinamente diferentes (por ejemplo, "La Esquina del Real" en una factura y "del Real Lit Esquina Restaurant" en otra -- visible más adelante en la sección 5.14). Resolver eso requeriría una comparación difusa (fuzzy matching) entre nombres, lo cual queda fuera de alcance por ahora, igual que las limitaciones de Textract ya documentadas en la v1.

### 5.6 Bug latente encontrado por revisión de código: suma nula de totales

Al revisar obtener_top_proveedores antes de darla por terminada (no a partir de un fallo real observado, sino de una revisión proactiva del código), se identificó que SUM(total) podía devolver NULL si todas las facturas de un proveedor tuvieran el total sin detectar -- y ese NULL, al pasar por float(None) en Python, habría causado una excepción no controlada. Se corrigió envolviendo la suma en COALESCE(SUM(total), 0), el mismo patrón ya usado en obtener_resumen_gasto.

### 5.7 Lección operativa: instancias del Auto Scaling Group desincronizadas

Probando el panel ya desplegado, el usuario notó un comportamiento extraño y lo diagnosticó correctamente por su cuenta: al recargar la página, el nombre de un proveedor aparecía a veces normalizado ("SMALL HE RO") y a veces con los saltos de línea originales ("SMALL\nHE\nRO"), alternando entre una recarga y otra, y preguntó si tenía que ver con el autoescalado.

La causa confirmada: el despliegue de la corrección de la sección 5.5 se había hecho parchando manualmente una sola instancia del Auto Scaling Group (vía EC2 Instance Connect), no las dos. El Application Load Balancer reparte las solicitudes entre ambas instancias por turno (round-robin), así que cada recarga podía caer en la instancia ya corregida o en la que todavía corría el código viejo.

**Corrección de proceso: **todo parche manual debe aplicarse (y verificarse) en las dos instancias del grupo, nunca en una sola -- esta misma lección reapareció más adelante, de forma más severa, en la sección 5.13, con el Instance Refresh.

### 5.8 Formato de cifras en pesos colombianos

Con el panel ya mostrando datos reales, el usuario pidió que las cifras se mostraran en el formato numérico usado en Colombia (punto como separador de miles, coma como separador decimal -- ej. $1.500.000,50), en vez del formato estadounidense por defecto de Python.

Python no incluye un formato de configuración regional ("locale") para esto sin depender de que el sistema operativo del servidor tenga instalado el locale es_CO -- una dependencia frágil de garantizar. En su lugar, se implementó formatear_numero(): primero se genera el número en formato estadounidense (coma para miles, punto para decimales) y luego se intercambian los dos símbolos usando un marcador temporal ('§'), para que uno no pise al otro a mitad de camino. La función se registró como filtro de Jinja (cop), usable directamente en las plantillas como {{ valor | cop }}.

El ejemplo que dio el usuario incluía un punto también antes de los decimales ($1.500.000.00); se le señaló esa inconsistencia con el estándar colombiano real (que usa coma para decimales) y se implementó el estándar correcto -- el usuario confirmó que era lo que en realidad buscaba.

### 5.9 Integración con Amazon Bedrock: elección de modelo, costo y perfiles de inferencia entre regiones

Con el panel visual completo, el usuario pidió explícitamente dejar pendiente la documentación y avanzar directo a Bedrock. Antes de escribir código, preguntó por el costo de usar el modelo, dejando una restricción explícita para toda la etapa de desarrollo: costo cero o el más bajo posible.

Se eligió Claude Haiku 4.5 por ser el modelo de menor costo de la familia Claude disponible en Bedrock. Precio verificado directamente en la documentación de Anthropic (no asumido): $1 USD por millón de tokens de entrada y $5 USD por millón de tokens de salida -- Bedrock generalmente sigue este mismo precio de lista, con un posible recargo de hasta 10% en endpoints "regionales" multi-región (nota que podría no aplicar a los endpoints "globales" verdaderos). El usuario confirmó, de forma independiente, haber creado ya una alerta de presupuesto en su cuenta de AWS.

En la consola de Bedrock, la ficha del modelo Claude Haiku 4.5 indica "Inference type: Cross-region inference": este modelo no se puede invocar directamente con su ID plano de modelo, sino que exige usar un perfil de inferencia entre regiones (inference profile). La consola ofrece dos variantes: un perfil "US" (geográfico, limitado a regiones de Estados Unidos) y un perfil "Global" (enruta la solicitud a cualquier región comercial disponible). El proyecto usa el perfil Global:

ID del perfil: global.anthropic.claude-haiku-4-5-20251001-v1:0
ARN: arn:aws:bedrock:us-east-1:<TU-CUENTA-AWS>:inference-profile/     global.anthropic.claude-haiku-4-5-20251001-v1:0

### 5.10 Política de IAM para invocar el perfil de inferencia

Consultando directamente la documentación oficial de AWS (no por prueba y error) se confirmó que los permisos de IAM para invocar un perfil de inferencia "global." siguen un patrón distinto al de un perfil "us.": un perfil "us." geográfico puede autorizarse con un comodín (*) de región sobre el recurso foundation-model, pero un perfil "global." exige dos entradas de recurso específicas -- una con una región concreta y otra sin ninguna región en el ARN -- condicionadas a que la invocación venga exactamente a través de ese perfil de inferencia.

Se creó una política nueva y separada (politica-bedrock-invoke), deliberadamente distinta de politica-subir-facturas-s3, para mantener los permisos organizados por capacidad y facilitar una auditoría futura, en vez de acumular todo en una sola política cada vez más mal nombrada:

{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "InvocarPerfilDeInferenciaHaiku",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream"
            ],
            "Resource": [
                "arn:aws:bedrock:us-east-1:<TU-CUENTA-AWS>:inference-profile/
global.anthropic.claude-haiku-4-5-20251001-v1:0"
            ]
        },
        {
            "Sid": "InvocarModeloBaseSoloViaEsePerfil",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream"
            ],
            "Resource": [
                "arn:aws:bedrock:us-east-1::foundation-model/
anthropic.claude-haiku-4-5-20251001-v1:0",
                "arn:aws:bedrock:::foundation-model/
anthropic.claude-haiku-4-5-20251001-v1:0"
            ],
            "Condition": {
                "StringLike": {
                    "bedrock:InferenceProfileArn":
"arn:aws:bedrock:us-east-1:<TU-CUENTA-AWS>:inference-profile/
global.anthropic.claude-haiku-4-5-20251001-v1:0"
                }
            }
        }
    ]}

Se adjuntó a ec2-app-role, confirmado con list-attached-role-policies mostrando las tres políticas del rol: politica-subir-facturas-s3, politica-bedrock-invoke y politica-leer-password-ssm.

### 5.11 Habilitación de acceso al modelo: formulario de Anthropic y suscripción de AWS Marketplace

Con la política de IAM adjuntada, la primera prueba real del asistente devolvió ResourceNotFoundException. El usuario pegó, en ese momento, un aviso real de la consola de Bedrock: "Model access page has been retired" -- un cambio reciente de AWS que simplificó el acceso a modelos: los modelos serverless ahora se habilitan automáticamente en su primer uso, a nivel de cuenta completa, sin necesidad de solicitarlo manualmente por adelantado.

Ese mismo aviso deja dos excepciones relevantes para este proyecto: los modelos de Anthropic todavía exigen completar una única vez un formulario de "Submit use case details" (nombre del proyecto, sitio web -- se usó el repositorio de GitHub de la v1 --, industria, tipo de usuarios previstos y una descripción del caso de uso); y los modelos distribuidos vía AWS Marketplace exigen que un usuario con permisos de Marketplace los invoque una vez para activarlos a nivel de toda la cuenta.

El usuario completó el formulario de casos de uso él mismo (no se fabricó ni se envió información de la cuenta en su nombre). El error cambió de ResourceNotFoundException a AccessDeniedException -- señal de progreso real (el modelo ya se reconocía como existente), pero con un permiso genuino faltante. El mensaje de AWS fue explícito y verificable, no una suposición:

AccessDeniedException: Model access is denied due to IAM user
or service role is not authorized to perform the required AWS
Marketplace actions (aws-marketplace:ViewSubscriptions,aws-marketplace:Subscribe) to enable access to this model.

Esto confirmó la hipótesis del aviso de AWS: ec2-app-role, construido deliberadamente como un rol acotado sin ningún permiso de Marketplace, no podía ser el primer invocador del modelo en la cuenta. Se le adjuntó a el usuario de IAM con el que el usuario tiene la sesión abierta en la consola (no a ec2-app-role) la política administrada por AWS AWSMarketplaceManageSubscriptions, que incluye exactamente los permisos citados en el mensaje de error.

Siguiendo la preferencia explícita del usuario de aprender primero la consola gráfica de AWS antes de enfocarse en la CLI, la prueba de activación se hizo en el Playground de Bedrock en vez de por CloudShell. En el primer intento, el diálogo de selección de modelo tenía marcado "US Anthropic Claude Haiku 4.5" -- un perfil de inferencia distinto al configurado en el proyecto -- y se corrigió a "Global Anthropic Claude Haiku 4.5" antes de la prueba, para que la activación correspondiera exactamente al perfil que usa la aplicación.

Con el perfil correcto seleccionado, el mensaje de prueba en el Playground respondió con éxito, y llegó al correo la confirmación formal de AWS Marketplace: una oferta aceptada para "Claude Haiku 4.5 (Amazon Bedrock Edition)" de Anthropic, PBC, con un monto de compra de $0.00 USD (la suscripción en sí no tiene costo; el costo real es por uso, según los precios de la sección 5.9). Con la suscripción activa a nivel de cuenta, la misma pregunta que antes fallaba se probó directamente en /asistente y respondió correctamente con datos reales de la base de datos.

### 5.12 Bug de integración: ValidationException en resultados de herramientas tipo lista

Con el primer tipo de pregunta funcionando, se probaron preguntas que ejercitan las demás herramientas. El patrón de resultados fue revelador: la pregunta que compara gasto contra venta (obtener_comparacion_gasto_venta, que devuelve un diccionario) funcionó de inmediato; las preguntas sobre proveedores principales, facturas incompletas y venta de un mes sin datos (obtener_top_proveedores, obtener_facturas_incompletas, obtener_venta_por_dia -- las tres devuelven una lista de Python) fallaron de forma consistente con ValidationException.

La hipótesis, formada a partir de ese patrón y no de una suposición aislada, es que el campo json de un toolResult en la Converse API de Bedrock debe ser un objeto JSON en su nivel superior, no un arreglo -- cuando el resultado de Python era una lista, boto3 la serializaba como un arreglo JSON puro, y Bedrock rechazaba la solicitud completa antes de que el modelo la viera.

**Corrección: **envolver siempre el resultado de cualquier herramienta en un diccionario ({'resultado': resultado}) antes de enviarlo como toolResult, sin importar si el valor original era una lista, un diccionario o incluso None -- así el nivel superior siempre es un objeto válido.

Nota de honestidad metodológica para esta bitácora: la causa no se confirmó leyendo el mensaje de error textual y detallado de AWS (error.response['Error']['Message']), porque ese mensaje se descartaba en el código original y solo se exponía el código corto del error (ValidationException). Se agregó temporalmente una versión de diagnóstico que sí mostraba el mensaje completo, pero para cuando esa versión llegó a ejecutarse correctamente (ver 5.13, el problema de despliegue retrasó varias pruebas), la corrección de la envoltura ya estaba incluida en el mismo despliegue y la pregunta respondió sin error -- por lo que nunca se llegó a ver el texto exacto del error original. La corrección se dio por confirmada de forma empírica (el error desapareció, consistentemente, en las tres herramientas que antes fallaban) y no por lectura directa del mensaje de AWS. Se documenta esta distinción para no sobreafirmar una certeza que no se verificó de la forma más directa posible.

El mensaje de diagnóstico detallado se revirtió a la versión corta y genérica una vez terminada la investigación, para no exponerle al usuario final del asistente detalles técnicos internos de AWS.

### 5.13 Lección operativa: la casilla "Skip matching" del Instance Refresh

Al intentar desplegar las correcciones de la sección 5.12, varias pruebas consecutivas mostraron exactamente el mismo error de antes, sin cambio alguno, a pesar de confirmar en la consola de S3 que el archivo se había subido con una marca de tiempo reciente y de ver un Instance Refresh marcado como "Successful" al 100% en la consola de EC2. El propio usuario notó el dato clave: ese Instance Refresh terminaba sospechosamente rápido, a diferencia de refrescos anteriores.

La causa: el formulario de Instance Refresh de AWS incluye una opción llamada "Skip matching", que omite reemplazar instancias que ya "coinciden" con la plantilla de lanzamiento (Launch Template) vigente. El mecanismo de despliegue de este proyecto, deliberadamente, nunca modifica la plantilla de lanzamiento -- el código nuevo se descarga de S3 en cada arranque vía user data (ver v1, sección 5.46) -- así que, para efectos de esa comparación, las instancias siempre "coinciden" con la plantilla, y con Skip matching activado el refresh las omitía por completo sin relanzarlas, aunque terminara mostrando "Successful".

Mientras se diagnosticaba esto, se usó como solución temporal el mismo parche manual instancia por instancia de la sección 5.7 (vía EC2 Instance Connect, en las dos instancias). Una vez identificada la casilla, se repitió el Instance Refresh con Skip matching desmarcado: esta vez tardó un tiempo normal y el código nuevo sí quedó activo en ambas instancias, confirmado con una nueva prueba exitosa del asistente.

**Conclusión operativa: **el Instance Refresh es confiable para este proyecto siempre que Skip matching quede desmarcado en cada ejecución -- de lo contrario, reproduce exactamente el mismo riesgo de inconsistencia entre instancias que motivó dejar de parchar manualmente en primer lugar.

### 5.14 Verificación funcional del asistente con preguntas reales

Con el despliegue corregido, se probaron preguntas reales cubriendo distintas herramientas y casos límite, no solo el camino feliz:

- **"¿cuánto he gastado esta semana?" (obtener_resumen_gasto). **El asistente resolvió correctamente la fecha relativa ("esta semana") a un rango exacto y respondió con el total real y el número de facturas.
- **"Compárame lo que gasté con lo que vendí en los últimos 30 días" (obtener_comparacion_gasto_venta). **Respondió con venta total, gasto total, margen y porcentaje de gasto sobre venta, con una interpretación cualitativa razonable del resultado.
- **"¿Quiénes son mis proveedores principales este mes?" (obtener_top_proveedores). **Confirmó, con datos reales, la limitación conocida ya documentada en la sección 5.5: "del Real Lit Esquina Restaurant" y "La Esquina del Real" aparecen como proveedores distintos en la respuesta, siendo probablemente el mismo negocio real -- evidencia adicional de que resolver eso exigiría comparación difusa de nombres, fuera de alcance por ahora.
- **"¿Tengo facturas incompletas?" (obtener_facturas_incompletas). **Listó correctamente las facturas con proveedor o total sin detectar, identificándolas por ID.
- **"¿cuánto vendí en marzo de 2019?" (obtener_venta_por_dia, caso sin datos). **Sin resultados para ese rango, el asistente no inventó una cifra ni falló: explicó que no había registros y preguntó si se quería consultar un período distinto -- el comportamiento exacto que exige la regla anti-alucinación del diseño (sección 5.1).
Con estas cinco pruebas cubriendo ambos lados del modelo de datos (gasto y venta), resultados en forma de lista y de diccionario, y un caso explícito sin datos, el patrón de tool use del asistente queda validado como funcional y resistente a los casos límite más importantes, aunque no se ejercitaron una por una las diez herramientas definidas en la sección 5.3.

### 5.15 Memoria conversacional multi-turno del asistente

Hasta este punto, cada pregunta al asistente se procesaba de forma completamente independiente -- una pregunta como "¿y la semana pasada?" no tenía forma de saber a qué se refería "la semana pasada" sin repetir el contexto completo. Se agregó memoria conversacional para que el asistente recuerde las preguntas y respuestas anteriores dentro de la misma conversación.

### Decisión de diseño: el historial vive en el navegador, no en el servidor

La opción instintiva -- guardar el historial en memoria del servidor, indexado por sesión -- se descartó deliberadamente. La aplicación corre en dos instancias EC2 detrás de un ALB que reparte peticiones por turnos (el mismo hecho que ya causó los problemas de las secciones 5.7 y 5.13): si el historial viviera en la memoria de una sola instancia, la siguiente pregunta del mismo usuario podría caer en la otra instancia y "olvidar" la conversación. En vez de introducir sesiones pegajosas (sticky sessions) en el ALB o una tabla nueva en la base de datos, el historial se guarda en una variable de JavaScript en el navegador y se manda completo en el cuerpo de cada petición a /chat. El servidor queda completamente sin estado (stateless): usa el historial recibido solo para esa petición y no recuerda nada entre una llamada y la siguiente, sin importar cuál de las dos instancias responda.

### Control de costo: ventana de historial acotada

Cada mensaje de historial que se manda a Bedrock es texto de entrada adicional, y los tokens de entrada tienen costo (sección 5.9). Sin límite, una conversación larga iría encareciendo cada pregunta nueva progresivamente. Se fijó una constante (HISTORIAL_MAXIMO_MENSAJES = 10) que limita el contexto enviado a las últimas 5 preguntas con sus 5 respuestas, sin importar cuánto más larga sea la conversación completa que el navegador tiene almacenada. Se agregó también un botón "Nueva conversación" en el widget de chat, que borra el historial en el navegador -- útil tanto para cambiar de tema como para controlar el costo de una conversación que ya no hace falta seguir arrastrando.

Se agregó, además, una sexta regla al mensaje de sistema del asistente: puede usar el historial para entender a qué se refiere una pregunta de seguimiento, pero cualquier cifra que mencione debe seguir viniendo de una herramienta invocada en esa misma respuesta -- nunca copiada o recalculada a mano de un mensaje anterior. Esto extiende la regla anti-alucinación original (sección 5.1) al nuevo escenario de conversaciones de varias vueltas.

### Verificación antes de desplegar

Siguiendo la misma disciplina de pruebas con mocks ya establecida para la integración de Bedrock (sección 5.9), se probó la función que arma los mensajes a partir del historial -- sin gastar en llamadas reales a Bedrock -- cubriendo cuatro casos: sin historial (primera pregunta), con un historial corto que se incluye completo y en orden, con un historial más largo que el límite (confirmando que el recorte respeta los pares pregunta-respuesta y no rompe la alternancia de roles que exige la Converse API), y un historial vacío explícito. Los cuatro casos pasaron antes de desplegar el cambio.

Verificado en producción con una conversación real de dos vueltas: "¿cuánto he gastado esta semana?" seguido de "¿y la semana pasada?" (sin repetir la palabra "gasto"). El asistente resolvió correctamente, a partir del contexto de la primera pregunta, que la segunda seguía preguntando por gasto, calculó el rango de fechas correcto de la semana anterior, y respondió con honestidad que no había facturas registradas en ese período. El botón "Nueva conversación" también se verificó: borra la conversación visible correctamente.

### 5.16 Cobertura completa de las diez herramientas: prueba de las cinco restantes

Continuando el trabajo de la sección 5.14, se ejercitaron por lenguaje natural las cinco herramientas que aún no se habían probado directamente: obtener_gasto_por_dia, obtener_facturas_de_un_dia, obtener_resumen_gasto sobre un período distinto al semanal, obtener_item_mas_frecuente, y la comparación de gasto entre dos ventanas de 30 días. Con esto, las diez herramientas definidas en la sección 5.3 quedaron ejercitadas al menos una vez con una pregunta real, no solo revisadas por lectura de código.

obtener_item_mas_frecuente devolvió, para algunas facturas, nombres de ítem con texto mezclado o poco legible. Se diagnosticó como una consecuencia esperada de las imágenes de prueba usadas (facturas de ejemplo con ruido de OCR y texto en más de un idioma), consistente con la limitación de Textract ya documentada en la sección 5.5 -- no una falla de código nueva, y no se propuso corrección alguna.

La prueba de obtener_facturas_de_un_dia, con la pregunta "¿qué facturas se procesaron el 29 de agosto de 2026?", fue la que resultó más reveladora: expuso un problema real de confiabilidad en cómo el asistente resume listas de datos, documentado en detalle en las secciones 5.18 y 5.19 a continuación.

### 5.17 Verificación de manejo de impuestos en una factura real

Se planteó una duda concreta a partir de una factura real de un restaurante (Popeyes) usada como caso de prueba: dos ítems detectados por Textract no sumaban el total final de la factura, lo que en principio parecía un bug de extracción relacionado con impuestos no considerados.

La verificación aritmética directa mostró que no hay ningún bug: el subtotal impreso en la factura ($66,49 + $9,96 = $76,45) coincide exactamente con la suma de los ítems detectados, y ese subtotal más el impuesto impreso por separado ($9,94) coincide exactamente con el total general ($86,39). Es decir, los ítems se extraen correctamente en su valor antes de impuestos, y el campo TOTAL que devuelve Textract sí incluye el impuesto -- que es como funciona normalmente una factura impresa. Adicionalmente, un renglón sin precio propio impreso (un ítem incluido dentro de otro) no se extrae como línea aparte con precio, lo cual es correcto y coincide con la limitación de Textract ya conocida, no un caso nuevo.

**Conclusión: **no se recomienda ningún cambio de código a partir de este caso -- es una verificación positiva del comportamiento esperado, no un bug.

A partir de esta revisión surgió una pregunta separada, aún sin decidir: si conviene agregar a la vista de facturas (a) el valor del impuesto como campo propio, y (b) la posibilidad de ver la imagen real de la factura procesada. Ambas mejoras requerirían cambios en la función Lambda procesar-factura y en el esquema de la tabla de facturas en RDS, y dejan abierta la pregunta de si conviene rellenar (backfill) el dato para las facturas ya procesadas. Entre las dos, se evaluó que la visualización de la imagen real aporta más valor práctico al microempresario que el campo de impuesto por separado. Ninguna de las dos se ha implementado; queda como decisión pendiente en la sección 7.

### 5.18 Confiabilidad de las respuestas agregadas del modelo: reglas 7 y 8

Al revisar la respuesta de la prueba de la sección 5.16 sobre las facturas del 29 de agosto, se encontró una inconsistencia: el asistente afirmó un total de 20 facturas, pero su propio desglose por proveedor, sumado a mano sobre la respuesta pegada, daba 18. No era una cifra inventada de la nada -- los datos de cada proveedor eran reales, provenientes de la herramienta -- sino un error de conteo del modelo al agrupar y resumir esos datos en prosa, un riesgo distinto pero igualmente engañoso para quien lee la respuesta.

Se agregó una séptima regla al mensaje de sistema: si una herramienta devuelve una lista de registros y la respuesta los agrupa o resume, el modelo debe verificar antes de responder que la suma de las cantidades de cada grupo sea igual al total de registros recibidos, y volver a contar con cuidado si no cuadra, en vez de reportar un resumen agrupado mal contado.

Al volver a probar la misma pregunta con la regla 7 desplegada, el total principal ya cuadraba correctamente contra el desglose por proveedor (4+4+3+2+4+2+1 = 20). Sin embargo, una nota secundaria de la misma respuesta -- "3 facturas con problemas: 2 sin proveedor, 2 sin total" -- seguía siendo internamente inconsistente: la aritmética de la propia lista mostraba que debían ser 3 facturas sin proveedor y 4 facturas con algún problema en total, no 3 y 2 como decía la nota.

La causa fue la misma familia de problema en un lugar distinto: el modelo estaba infiriendo cuáles facturas tenían datos incompletos revisando a ojo la lista cruda de otra herramienta, en vez de usar obtener_facturas_incompletas, que ya calcula exactamente ese conteo con SQL. Se agregó una octava regla exigiendo el uso de esa herramienta específica para cualquier pregunta sobre facturas con datos incompletos o sin detectar, en vez de inferirlo revisando otra lista.

### 5.19 Una regresión más seria: cifra fabricada para un proveedor, y las reglas 9 y 10

Con las reglas 7 y 8 desplegadas, se volvió a probar la misma pregunta sobre las facturas del 29 de agosto. El conteo secundario de la sección anterior ya estaba resuelto, pero apareció un problema nuevo y más grave: el modelo presentó el subtotal de ABC Service como $70.480 (equivalente a $17.620 × 4), cuando en realidad solo 3 de las 4 facturas de ese proveedor tenían un total detectado -- el subtotal correcto, sumando solo las facturas con dato real, es $52.860. En la práctica, el modelo le había asignado a una factura sin total detectado el mismo valor que sus facturas hermanas, en vez de dejarla fuera de la suma: una violación indirecta de la regla 1 (nunca inventar una cifra), a pesar de que ninguna herramienta le devolvió ese valor de $70.480 -- lo fabricó él mismo al sumar en su propia respuesta.

Ese error arrastró el total general del día, que el asistente reportó en aproximadamente $400.750. Se verificó de forma independiente el valor correcto, tanto recalculando el subtotal de cada proveedor a partir de la lista completa como cruzando el resultado contra la prueba de obtener_gasto_por_dia ya realizada en la sección 5.16: el total real del 29 de agosto es $383.130. El conteo de facturas también retrocedió -- la tabla que armó el modelo esa vez sumaba 19, no 20, con un registro que se perdió silenciosamente en el camino.

La causa raíz es de fondo, no un caso aislado más: el modelo estaba haciendo su propia agregación en prosa libre sobre una pregunta que ni siquiera pedía un resumen, solo el detalle de las facturas del día. Las reglas 7 y 8 habían corregido cada síntoma puntual que las motivó, pero no atacaban la causa general: dejar en manos del modelo una suma que ya existe, calculada de forma confiable, en una herramienta SQL.

Se agregaron dos reglas más para atacar la causa de raíz en vez del síntoma. La regla 9 indica que, si la pregunta pide el detalle o la lista de facturas de un día o período (no un resumen), la respuesta debe presentarlas como una lista simple, sin agruparlas ni calcular subtotales por cuenta propia. La regla 10 indica que, si la pregunta sí pide un total o un desglose agrupado por proveedor o ítem, el modelo debe usar la herramienta que ya calcula ese agregado con SQL (obtener_top_proveedores, obtener_item_mas_frecuente) en vez de sumar él mismo los montos de una lista cruda.

Se volvió a probar la misma pregunta con las reglas 9 y 10 desplegadas. El resultado corrigió el problema de fondo: la respuesta presentó las 20 facturas como una lista simple, sin agrupar por proveedor ni calcular ningún subtotal; las dos facturas de ABC Service sin total detectado (posiciones 12 y 17 de la lista) se marcaron explícitamente como "sin total detectado" en vez de recibir un valor inventado; y el conteo de 20 facturas quedó verificable directamente contra la lista (veinte líneas, sin huecos ni duplicados). No volvió a aparecer ninguna cifra fabricada.

### 5.20 Legibilidad de las respuestas: texto plano y formato de cifras (reglas 11 y 12)

Con el contenido de las respuestas ya corregido, la misma prueba de las facturas del 29 de agosto expuso un problema distinto, de presentación: la respuesta se veía como un solo párrafo corrido, con los símbolos de una tabla en formato markdown ("|", "---", "**") mostrados literalmente en pantalla en vez de una tabla.

Se identificaron dos causas independientes. La primera es un bug real de frontend, presente desde antes aunque no se había notado: el widget de chat asigna la respuesta con parrafo.textContent, sobre un párrafo cuyo CSS por defecto (white-space: normal) colapsa cualquier salto de línea en un espacio -- por lo que cualquier respuesta de varias líneas, no solo tablas, se aplastaba visualmente en un párrafo corrido. Se corrigió agregando white-space: pre-wrap al estilo de los párrafos de la conversación, para que los saltos de línea se respeten sin necesidad de tocar el HTML generado.

La segunda causa es un desajuste de formato: el modelo estaba generando sintaxis de tabla markdown, pero el widget de chat no interpreta markdown, solo muestra texto plano. Se evaluaron dos caminos: enseñarle al frontend a renderizar markdown como HTML real, o instruir al modelo para que nunca use esa sintaxis. Se descartó el primero por ser una inversión de ingeniería mayor con una superficie de riesgo nueva (pasar de textContent a innerHTML exige escapar con cuidado cualquier texto proveniente de OCR antes de insertarlo, para no abrir una puerta de inyección de HTML) y por encajar peor con un contenedor angosto pensado para celular, donde una tabla de varias columnas con montos largos es difícil de leer de todas formas. Se optó por agregar una undécima regla al prompt de sistema que le prohíbe al modelo usar sintaxis markdown y le pide presentar listas como una línea por elemento, con un guion al inicio.

Verificado en producción con la misma pregunta: la respuesta se mostró como una lista legible, una factura por línea, sin ningún símbolo de markdown.

La misma prueba dejó ver un problema adicional, más de fondo: las cifras en pesos en el texto del asistente mezclaban dos convenciones distintas en la misma respuesta (por ejemplo $199.65 con el punto como separador decimal, junto a $32.761.00 con el punto como separador de miles y un ".00" agregado al final) -- ambiguo para un lector que no conoce de antemano los datos. Se agregó una duodécima regla que exige el formato colombiano en cualquier cifra en pesos dentro del texto: punto como separador de miles, sin decimales ni centavos (el peso colombiano no se usa en centavos en la práctica).

Verificado en producción: las cifras de la misma prueba aparecieron consistentemente en formato colombiano ($17.620, $131.553, $32.761, etc.), sin mezcla de convenciones.

Queda una observación pendiente, de menor prioridad: al aplicar la regla 12, el modelo redondea él mismo cada cifra al convertirla a texto (por ejemplo $108,68 pasó a mostrarse como $109) -- una operación mecánica que hoy resuelve el modelo de forma no determinística en vez de una función de código. El impacto actual es bajo porque, gracias a las reglas 9 y 10, cualquier suma o comparación real sigue calculándose en SQL sobre el valor exacto, no sobre el texto redondeado que se muestra. Una alternativa más robusta -- que las propias herramientas de consulta devuelvan la cifra ya formateada en pesos colombianos, reutilizando la función formatear_numero ya usada en el panel (sección 5.8), para que el modelo solo la copie en vez de decidir cómo redondearla -- queda anotada como mejora futura, sin implementar por ahora.

### 5.21 Pregunta abierta: datos de prueba con facturas no colombianas

Varias de las facturas usadas hasta ahora para probar el sistema (incluida la del recibo de Popeyes de la sección 5.17, y varios de los proveedores que aparecen en los ejemplos de esta bitácora) son facturas de ejemplo de otros países, no facturas colombianas reales. Surgió la pregunta de si conviene borrar esos datos de prueba y reiniciar solo con facturas locales.

El análisis distingue dos cosas. Los bugs encontrados y corregidos en esta etapa (conteo mal agrupado, cifras fabricadas, formato de markdown, redondeo inconsistente) son fallas de razonamiento del modelo y de extracción de Textract que ocurrirían igual con una factura colombiana real -- los datos de prueba actuales no invalidan nada de lo ya verificado. Lo que sí es cierto es que hay categorías de bugs específicas del contexto colombiano que datos extranjeros no pueden revelar: manejo del IVA como renglón separado, fechas en formato DD/MM/AAAA en vez de MM/DD/AAAA, extracción correcta del NIT o la razón social.

**Decisión (pendiente, no tomada aún): **no se ha borrado ningún dato de prueba. La recomendación evaluada es no borrar todavía -- los datos actuales siguen siendo útiles para encontrar bugs de lógica -- sino agregar un lote pequeño de facturas colombianas reales como prueba dirigida a los casos específicos que los datos actuales no cubren, y dejar la limpieza completa de datos de prueba como un paso final, justo antes de documentar o mostrar la versión definitiva del proyecto.

### 5.22 Implementación: visualización de la imagen real de la factura

De los dos asuntos abiertos en la sección 5.17, se decidió avanzar primero con la visualización de la imagen real de la factura en /facturas, dejando el campo de impuesto por separado sin implementar por ahora.

Antes de escribir código se revisó el código real de la función Lambda que procesa cada factura, en vez de asumir el alcance del cambio. El hallazgo cambió la estimación inicial: la tabla factura ya tenía una columna s3_key, y la Lambda ya la guarda desde la versión 1 en cada factura procesada -- no hacía falta ningún cambio en la Lambda ni en el esquema de RDS, y tampoco backfill, porque las facturas ya procesadas ya tenían ese dato guardado. El trabajo real se redujo a leer esa columna en la ruta /facturas y generar un enlace a la imagen.

La implementación agrega s3_key a la consulta SQL de /facturas, genera una URL firmada temporal (presigned URL) de S3 por cada factura con el metodo get_object del cliente de S3 ya usado en el proyecto (ExpiresIn de una hora), y agrega en la plantilla un enlace "Ver imagen original de la factura" que abre esa URL en una pestaña nueva. Se optó por un enlace simple en vez de miniaturas incrustadas en la lista, para no cargar hasta 27 imágenes de una vez en una página pensada para celular.

### Bug encontrado: permisos de IAM insuficientes para leer el objeto

La primera prueba en producción devolvió un error XML de S3 (AccessDenied) al abrir el enlace, en vez de la imagen. La causa: generar una URL firmada no otorga ningún permiso por si sola -- solo firma la solicitud en nombre de la identidad que la generó (el rol ec2-app-role de la instancia EC2), y S3 verifica en el momento en que se usa la URL si esa identidad tiene permiso real para la operación. El rol ya tenía s3:PutObject sobre el prefijo entrada/ (por eso la subida de facturas funciona) y s3:GetObject, pero acotado únicamente al archivo despliegue/app.py (necesario para que la instancia descargue el código en el arranque, sección 5.4) -- nunca se le había dado permiso de lectura sobre las imágenes de facturas en entrada/, porque nadie lo había necesitado hasta ahora.

Se corrigió agregando un tercer statement a la política de IAM del rol, separado de los dos existentes para no arriesgar modificarlos (mismo criterio de la sección 5.10):

{
    "Sid": "LeerImagenesFacturasS3",
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::facturas-microempresarios-
                 <TU-CUENTA-AWS>/entrada/*"}

En el primer intento de aplicar este cambio, el statement nuevo se pegó fuera del arreglo Statement del documento de política (como una clave suelta de nivel superior), lo cual no es JSON válido y tampoco es una estructura que IAM acepte -- un documento de política solo admite Version y Statement en su nivel superior. Se corrigió moviendo el statement nuevo dentro del arreglo, como tercer elemento de la lista. Con la política corregida y guardada, el enlace mostró la imagen real correctamente, verificado en producción.

### Bug encontrado, por separado: valores nulos mostrados como "None"

Al revisar facturas con ítems sin cantidad, precio unitario o subtotal detectado por Textract, la plantilla mostraba el texto literal "None" (la representación de Python de un valor nulo), en vez de dejar la celda vacía o indicar de forma clara que el dato no se detectó. Se corrigió mostrando un guion ("-") en cualquier campo de ítem que llegue nulo desde la base de datos. Es una corrección de presentación únicamente -- no cambia ni recalcula ningún dato.

Aparte de estos dos bugs, algunas facturas de prueba muestran cifras de ítems que no cuadran entre si (cantidad por precio distinto del subtotal mostrado). Se evaluó explícitamente no corregir esto: no es un bug de código, sino ruido de OCR propio de las facturas de prueba no colombianas ya señaladas en la sección 5.21 (algunas con nombres de ítem visiblemente corrompidos). Recalcular esas cifras a mano para que cuadren equivaldría a fabricar un dato que Textract nunca extrajo -- el mismo riesgo que se ha evitado deliberadamente en el asistente conversacional (secciones 5.18 y 5.19) -- así que se deja tal como Textract lo detectó, y la corrección real queda en manos de probar con facturas colombianas reales de mejor calidad.

### 5.23 Formato de moneda en /facturas

Al empezar a agregar facturas reales escritas a mano, se notó que /facturas nunca había recibido el mismo tratamiento de formato colombiano que ya tenía /panel desde la sección 5.8 -- es una plantilla distinta, heredada tal cual de la v1, que quedó fuera de ese cambio. Los montos se mostraban como el número crudo de la base de datos (por ejemplo "100000.00"), sin separador de miles. Se aplicó el mismo filtro cop ya usado en /panel (signo $, punto de miles, coma decimal) al total de la factura, al precio unitario y al subtotal de cada ítem. De paso se corrigió otro "None" que no se había atrapado en la sección 5.22: cuando el total de la factura no se detecta, ahora se muestra "sin total detectado" en vez del texto literal "None".

### 5.24 Primera factura electrónica real: hallazgos y una corrección propia

El usuario compartió una factura electrónica real (formato DIAN, FE 550, con CUFE y todos los campos de una factura electrónica colombiana) para evaluar si el pipeline actual -- pensado y probado hasta ahora solo con fotos de facturas físicas -- también sirve para este tipo de documento, que llega como PDF en vez de foto.

La primera prueba se hizo tomando una captura de pantalla del PDF y subiéndola como imagen -- funcionó correctamente, pero no probaba nada sobre PDFs en si. Se afirmó, sin verificarlo, que el pipeline ya soportaba PDF directamente; esa afirmación resultó incorrecta y tuvo que corregirse cuando el usuario aclaró que la prueba real había sido con una imagen, no con el PDF. La lección: no dar por buena una inferencia sobre lo que el usuario hizo sin confirmarla, incluso cuando el resultado observado parece encajar.

Antes de intentar la prueba real con el PDF, se consultó la documentación oficial de Amazon Textract (no la memoria del modelo, dado que son límites de servicio que cambian) para entender que tan lejos llega el soporte de PDF. El hallazgo clave: la API síncrona que usa la Lambda actual (analyze_expense) solo admite PDF de una sola página -- un documento de varias páginas requeriría la API asíncrona (StartExpenseAnalysis / GetExpenseAnalysis), una arquitectura distinta con un tema de SNS y una segunda Lambda para recibir el resultado cuando el trabajo en segundo plano termine. Se decidió no construir esa ruta asíncrona de forma preventiva -- es una inversión de arquitectura real para un caso (factura electrónica de varias páginas) que todavía no se ha visto fallar. Si en el futuro una factura electrónica de varias páginas falla, la Lambda actual lo hará con una excepción reconocible en CloudWatch, y esa sería la señal real para construir el flujo asíncrono, en vez de adivinar ahora si hace falta.

### 5.25 Bug encontrado: el formulario de subida se colgaba con un PDF

Al intentar subir el PDF real (no una captura de pantalla), el formulario se quedó indefinidamente en el mensaje "Optimizando imagen...", sin llegar nunca a pedir la URL de subida, sin llegar a S3, y sin ningún registro en CloudWatch (la Lambda nunca se disparó porque nunca hubo un archivo nuevo en el bucket).

La causa, confirmada leyendo el código del formulario (PAGINA_HTML): el paso de "optimizar" la imagen carga el archivo dentro de un elemento <img> del navegador para poder redimensionarlo con un <canvas> -- un mecanismo que funciona con fotos, pero un navegador nunca puede decodificar un PDF como si fuera una imagen. El evento que avisa que la imagen ya cargó (img.onload) nunca se dispara con un PDF, y como el código tampoco manejaba el caso contrario (img.onerror), la promesa de JavaScript quedaba esperando ese evento para siempre, sin ningún aviso de error visible.

Se corrigió el formulario para que, si el archivo seleccionado es un PDF, se suba directamente sin pasar por ese paso de compresión (que de todas formas solo tiene sentido para fotos). De paso se agregó manejo explícito de error para el caso de una imagen corrupta, que tenía el mismo problema de fondo (colgarse sin avisar). Verificado en producción: el PDF real se subió correctamente y quedó registrado en S3 y procesado por la Lambda.

### 5.26 Bug de interpretación de números: convención colombiana vs. estadounidense

Con el PDF ya procesado, apareció un problema más serio y más de fondo: la cantidad del ítem "HOSPEDAJE" se guardó como 100, cuando la factura real dice "Cantidad: 1,00" -- es decir, uno. No tendría sentido que cantidad 100 por precio 100 diera un subtotal de 100, y el usuario lo notó de inmediato.

La causa: la función limpiar_numero de la Lambda (que convierte el texto que devuelve Textract a un número) asumía siempre la convención estadounidense -- coma como separador de miles, punto como decimal -- y simplemente le quitaba cualquier coma al texto. Eso funciona para "$5,000.00" (estadounidense) pero interpreta mal "1,00" en convención colombiana (coma como decimal): al quitarle la coma queda "100", cuando el valor real es 1.00. Es exactamente el tipo de bug específico del contexto colombiano que se anticipó, sin poder confirmarlo todavía, en la sección 5.21 -- y que solo una factura colombiana real podía revelar.

Se corrigió la función para decidir la convención según el propio texto, en vez de asumir siempre una: si el texto tiene punto y coma a la vez, el separador decimal es el que aparece más a la derecha (el otro símbolo, sea cual sea, es separador de miles) -- así "100.000,00" se interpreta correctamente como 100000.0 y "5,000.00" como 5000.0. Si el texto solo tiene uno de los dos símbolos, se decide por la cantidad de dígitos después de ese símbolo: 2 dígitos es casi siempre un decimal ("1,00" -> 1.0), 3 dígitos es casi siempre una agrupación de miles sin decimales ("5,000" -> 5000.0, "5.000" -> 5000.0). La función se probó con 13 casos antes de desplegarla, incluyendo todos los formatos ya usados por las facturas de prueba en inglés (para confirmar que ninguno cambiara de resultado) y el caso nuevo que expuso el bug -- los 13 pasaron.

Además, atendiendo un pedido explícito del usuario, se agregó una validación (no correctiva) en la Lambda: por cada ítem donde Textract detectó cantidad, precio unitario y subtotal, se verifica que cantidad × precio sea aproximadamente igual al subtotal, y si no cuadra, se deja un aviso en los logs de CloudWatch identificando la factura y el ítem. Deliberadamente no se corrige ni se recalcula ningún valor a partir de esa comparación -- eso equivaldría a fabricar un dato que Textract no reportó, el mismo riesgo que ya se evitó en el asistente conversacional con las reglas 9 y 10 (sección 5.19). Verificado en producción, reprocesando la misma factura: cantidad, precio, subtotal y total ya cuadran entre sí ($100.000 × 1 = $100.000).

### 5.27 Incidente de despliegue: un app.py truncado en S3

Al desplegar los cambios de esta sesión (formato de moneda y arreglo del formulario de PDF), el sitio empezó a responder 502 Bad Gateway de forma consistente en cada intento -- a diferencia de la sección 5.7, donde el síntoma era intermitente (una instancia si y otra no). Repetir el mismo procedimiento de despliegue (subir a S3 y hacer Instance Refresh) no lo resolvió las primeras dos veces.

Siguiendo la misma disciplina de pedir evidencia real antes de diagnosticar (sección 5.4), se revisó directamente el archivo en una de las instancias vía EC2 Instance Connect. El primer intento de verificar la sintaxis del archivo dio un resultado engañoso: un error de permisos al intentar crear una carpeta __pycache__ dentro de /opt/app/ (que pertenece a root), no relacionado con el contenido real del archivo. La evidencia decisiva fue otra: el archivo en la instancia pesaba 13.187 bytes, cuando el app.py correcto pesa 65.117 bytes -- apenas el 20% del tamaño real, claramente cortado a media subida.

Se descartó la hipótesis original (condición de carrera entre la copia y el reinicio, sección 5.4) a favor de una más específica: el objeto que quedó guardado en S3 ya estaba incompleto desde el origen, probablemente por una subida interrumpida -- ambas instancias mostraban el mismo tamaño truncado de forma consistente, lo que no encaja con una carrera entre copia y reinicio (eso daría resultados distintos entre instancias, no el mismo archivo cortado en ambas). Se verificó también, por si acaso, que el archivo que se venía entregando al usuario no estuviera corrupto en el origen -- no lo estaba (tamaño y sintaxis correctos) -- antes de descartar esa posibilidad y insistir en revisar el tamaño del objeto en la consola de S3 en cada paso de la cadena. Con el archivo reenviado y confirmado su tamaño antes de subirlo, el despliegue funcionó.

### 5.28 Implementación: campo de impuesto en la vista de facturas

Con el bug de números ya corregido (sección 5.26) y la primera factura electrónica real funcionando de punta a punta, se retomó la decisión pendiente de la sección 5.17: agregar el impuesto como campo propio en /facturas, aprovechando que las facturas electrónicas reales lo traen como un dato limpio y separado (a diferencia de las facturas informales, donde solo se pudo verificar por aritmética -- sección 5.17, caso Popeyes).

La implementación siguió las tres piezas ya previstas. Primero, en RDS: ALTER TABLE factura ADD COLUMN impuesto NUMERIC, ejecutado por el usuario vía psql (EC2 Instance Connect), permitiendo nulos y sin valor por defecto -- igual que total, para poder distinguir "no se detectó impuesto" de "el impuesto es cero". Segundo, en la Lambda: se agregó la extracción del campo TAX, que Textract ya devuelve en SummaryFields junto con VENDOR_NAME y TOTAL, usando la misma función limpiar_numero ya corregida, y guardándolo en la columna nueva (NULL cuando Textract no lo detecta, nunca 0 por defecto). Tercero, en /facturas: se agregó una línea "Impuesto: $X" justo debajo del total, visible solo cuando la factura tiene un valor de impuesto detectado -- las facturas procesadas antes de este cambio simplemente no muestran la línea, en vez de decir "sin detectar" en las 37 facturas por igual.

Verificado en producción reprocesando la factura de Lilia Gasca: la línea nueva muestra "Impuesto: $0,00", que coincide exactamente con la factura real -- esa vendedora no es responsable de IVA, y el documento lo indica explícitamente en su encabezado ("No Somos Responsables de IVA"), así que el cero detectado es el valor correcto, no un dato faltante.

Queda pendiente, sin fecha de cierre fija, el backfill opcional del impuesto para las facturas procesadas antes de este cambio, aprovechando que la Lambda guarda el JSON crudo de Textract en datos_textract_raw (tipo jsonb) desde el principio -- en teoría permitiría rellenar ese dato sin volver a subir las imágenes, para las facturas donde Textract sí detectó el campo TAX en su momento.

### 5.29 Validación de factura completa (suma de subtotales vs. total) y una factura descartada por baja resolución

Se intentó procesar una factura de Dotaciones Gamero, pero las dos capturas compartidas mostraban baja resolución -- texto borroso, especialmente visible en la columna de descripción. Se descartó sin ningún cambio de código, a la espera de conseguir una captura más nítida de la misma factura.

Se agregó en guardar_en_rds (lambda_procesar_factura_final.py) una segunda validación, esta vez a nivel de factura completa y no por ítem: la suma de los subtotales de todos los ítems debería aproximarse al total detectado por Textract. Mismo criterio que la validación por ítem ya existente (sección 5.26): no bloqueante y no correctiva -- solo deja un aviso en los logs de CloudWatch, nunca corrige ni recalcula ningún valor.

**Decisión: **comparar contra dos candidatos, no contra uno solo. El total de una factura puede o no incluir el impuesto por encima de la suma de subtotales, según cómo cada proveedor lo imprima. El usuario planteó que, por convención contable, el total "debería" incluir el impuesto en promedio, pero reconoció haber visto excepciones reales entre las facturas ya procesadas. Con esa combinación -- una regla general más excepciones ya confirmadas, sin dato medido de qué tan frecuentes son -- fijar un solo candidato habría generado avisos falsos sistemáticos en las facturas que legítimamente siguen la convención contraria. Por eso se compara contra la suma de subtotales sola y, cuando Textract detectó impuesto, también contra esa suma más el impuesto; solo se avisa si NINGUNA de las dos cuadra.

Para no quedarse solo con la impresión de "en promedio", se agregó un tercer camino de log, distinto del aviso de "no cuadra con nada": cuando el total coincide con la suma de subtotales SIN el impuesto, mientras Textract sí detectó un impuesto mayor a cero, se registra un mensaje INFO en vez de AVISO -- no se trata de un error sino de la excepción ya conocida. La intención es acumular evidencia real en los logs durante un tiempo, para poder confirmar o corregir después la suposición de que el total "en promedio" incluye el impuesto, en vez de dejarla como una impresión sin medir.

Al implementar la comparación se encontró y corrigió un bug propio, antes de desplegarlo: la primera versión usaba el operador walrus (total_factura := limpiar_numero(...)) directamente como condición del if, lo cual habría saltado toda la validación en el caso -- improbable pero posible -- de que el total detectado fuera exactamente 0.0, ya que ese valor se evalúa como falso en Python. Se corrigió a una comparación explícita (is not None) antes de que llegara a producción.

Verificado con seis casos simulados por separado (total que incluye impuesto y cuadra en silencio, total sin impuesto que genera el INFO nuevo, un caso que no cuadra con ningún candidato y genera el AVISO, dos variantes sin impuesto detectado por Textract, e impuesto detectado en cero) -- los seis se comportaron como se esperaba. El archivo compila sin errores de sintaxis.

### 5.30 Lección de despliegue y verificación de la sección 5.29 con datos reales

Al conseguir una captura de mejor resolución de la factura de Dotaciones Gamero (pendiente de la sección 5.29) y procesarla, el aviso esperado en CloudWatch no apareció. La causa no fue un error de lógica: el código de la sección 5.29 se había editado, probado con simulaciones y sincronizado con GitHub, pero nunca se había desplegado a la función Lambda real -- subir código a GitHub no equivale a desplegarlo, y ese paso final se había quedado pendiente sin quedar marcado como tal. Se corrigió desplegando el código actualizado desde la consola de Lambda.

**Verificación con datos reales: **al reprocesar la factura de Gamero con la Lambda ya actualizada, apareció el aviso esperado: "la suma de los subtotales de los items (6000000.0) no cuadra con el total de la factura (7912000.0), ni sumandole el impuesto detectado (912000.0) para dar 6912000.0". Es la primera vez que esta validación tiene un caso real que la dispara: la factura de Gamero tiene un error propio del proveedor al emitirla (su descuento y su IVA sí están bien calculados entre sí, pero el total impreso no coincide con esa aritmética), no un error de extracción.

Adicionalmente, se agregó una segunda pieza pedida por el usuario: la misma reconciliación (suma de subtotales contra total, con o sin impuesto) ahora también se calcula y se muestra directamente en /facturas, no solo en los logs de CloudWatch. Se implementó como una función nueva (validar_total_factura) que se ejecuta al momento de renderizar el panel, leyendo lo que ya está guardado en RDS -- deliberadamente no como un valor que se calcula y se guarda una sola vez al procesar la factura, para que el chequeo aplique de una vez a las facturas ya procesadas anteriormente (incluida la propia Gamero) sin necesidad de reprocesarlas ni de una migración de datos. Cuando el total no cuadra con ningún candidato, se muestra una alerta visible junto al total y el impuesto de esa factura; cuando cuadra solo sin el impuesto, una nota informativa más discreta; cuando todo cuadra, el panel no muestra nada adicional, igual que el criterio ya usado en los logs. Verificado en el ambiente de pruebas: la factura de Gamero muestra la alerta correcta con el monto correcto de la suma de ítems.

Efecto colateral encontrado al verificar: al reprocesar Gamero para probar el aviso de la Lambda, quedó duplicada en la base de datos (factura_id 46 de la primera prueba, con peor resolución, y 47 de la segunda). No afecta nada funcional, pero queda como pendiente de limpieza antes de depurar los datos de prueba (sección 5.21).

### 5.31 Segundo canal del asistente: WhatsApp vía el Sandbox de Twilio

Se implementó una prueba de concepto de WhatsApp como segunda puerta de entrada al asistente, evaluada como mejora opcional en la sección 7 y ahora resuelta. Nueva ruta `/whatsapp-webhook` en `app.py`, que reutiliza tal cual `procesar_pregunta()` -- la misma lógica de herramientas y Bedrock que ya usan `/chat` y `/asistente`. Lo único genuinamente nuevo es la puerta de entrada: donde `/chat` recibe el historial de conversación desde el navegador, WhatsApp no tiene navegador que lo guarde, así que el historial se guarda ahora en una tabla nueva de RDS (`whatsapp_historial`), indexada por número de teléfono -- la misma idea de fondo (la base de datos compartida como fuente de verdad entre las dos instancias EC2, no la memoria de una sola instancia) aplicada al único lugar donde sí hace falta que el servidor recuerde algo por su cuenta.

**Decisión: **validar la firma de cada petición al webhook con la librería oficial de Twilio (`twilio.request_validator.RequestValidator`), no con una implementación propia. Se escribió primero a mano el algoritmo documentado por Twilio (HMAC-SHA1 sobre la URL más los parámetros del POST ordenados alfabéticamente) y funcionaba correctamente en pruebas, pero se descartó antes de desplegarlo: la propia documentación de Twilio pide explícitamente no reimplementar esta validación, porque pueden agregar parámetros nuevos a la firma sin aviso -- y esta ruta controla el acceso a una llamada a Bedrock, que tiene costo. El Auth Token de Twilio se guarda en SSM Parameter Store (`/facturas-app/twilio-auth-token`, SecureString), mismo patrón que la contraseña de RDS.

Es explícitamente una prueba de concepto sobre el Sandbox de Twilio, no un canal apto para usuarios finales reales todavía: cualquiera que quiera escribirle al bot primero tiene que enviar un código "join" desde su propio WhatsApp al número del sandbox. Habilitar WhatsApp para usuarios reales requeriría un proceso de verificación de WhatsApp Business con Twilio, fuera del alcance de esta prueba.

### 5.32 Despliegue del canal de WhatsApp: cuatro incidentes reales, todos resueltos

El despliegue de la sección 5.31 no salió limpio en el primer intento -- al contrario de otros cambios anteriores, esta vez dejó la aplicación caída (502 del Application Load Balancer) por un tiempo, y encontrar la causa real llevó varias rondas de diagnóstico con datos de servidor (`journalctl`, no solo el síntoma del navegador). Se documentan los cuatro problemas encontrados, en el orden real en que aparecieron, porque cada uno es una lección de infraestructura aparte, no solo un bug de código.

**Primero -- Instance Refresh en vez de reinicio manual: **al intentar diagnosticar el 502 inicial, se probó un "Instance Refresh" del Auto Scaling Group como paso de troubleshooting. Instance Refresh no reinicia las instancias existentes -- las termina y lanza instancias nuevas desde cero a partir del Launch Template, que no tenía ni el `app.py` nuevo ni la librería `twilio` instalada (esos cambios solo existían aplicados a mano por SSH sobre las instancias originales). El refresh se canceló a tiempo, antes de que reemplazara ambas instancias, cuando se detectó que estaba atascado con una instancia nueva "unhealthy".

**Segundo -- permiso de IAM faltante: **con `journalctl` se encontró la causa real de la caída original, un `AccessDeniedException`: el rol `ec2-app-role` tenía permiso `ssm:GetParameter` solo sobre el parámetro de la contraseña de RDS, no sobre el nuevo `/facturas-app/twilio-auth-token` que el `app.py` nuevo lee al arrancar. Se corrigió ampliando el `Resource` de esa política para cubrir ambos parámetros.

**Tercero -- el Auto Scaling Group reemplazando instancias por su cuenta: **mientras se arreglaban las instancias a mano (instalar `twilio`, aplicar el permiso de IAM), sus IPs privadas seguían cambiando entre un intento y el siguiente -- señal de que no eran la misma instancia. La causa: el ASG tiene Health check type en "EC2, ELB", así que cualquier instancia que el Target Group marque "Unhealthy" (con solo 2 chequeos fallidos seguidos, cada 30s, el ASG la considera candidata a reemplazo) se termina y se reemplaza automáticamente por una instancia nueva -- sin avisar, y sin ninguno de los cambios aplicados a mano. El grace period de 5 minutos no protege aquí, porque solo aplica a instancias recién lanzadas, no a una instancia existente a la que se le reinicia el servicio por dentro. Se corrigió suspendiendo el proceso `ReplaceUnhealthy` del ASG mientras se terminaba el despliegue, y reactivándolo una vez confirmado que ambas instancias quedaron "healthy" en el Target Group.

**Cuarto -- URL del webhook mal configurada en Twilio: **ya con la aplicación sana, el primer mensaje de prueba por WhatsApp no obtuvo respuesta. El Debugger de Twilio (error 11200) mostró la causa exacta: la petición POST había llegado a la raíz del dominio (`/`), no a `/whatsapp-webhook` -- el campo "WHEN A MESSAGE COMES IN" del Sandbox había quedado guardado sin la ruta al final. La ruta `/` solo acepta GET (sirve la página de subir facturas), de ahí el 405 "Method Not Allowed" que Twilio recibió. Se corrigió la URL en la configuración del Sandbox.

**Verificación con datos reales: **con los cuatro problemas resueltos, una pregunta real enviada por WhatsApp ("cuánto he gastado esta semana?") obtuvo la respuesta correcta del asistente a través del número del Sandbox, calculada por las mismas herramientas SQL que ya usa `/asistente`.

Pendiente, sin relación con el funcionamiento del canal: sincronizar a GitHub el código ya desplegado (la ruta `/whatsapp-webhook`, la tabla `whatsapp_historial` y la dependencia `twilio` en `requirements.txt`) -- por ahora existe en las instancias EC2 pero no en el repositorio.

### 5.33 Implementación: NIT del proveedor como campo propio en /facturas

Al probar un lote nuevo de facturas reales (dos casos: una factura electrónica de Baterías Colombia SAS y una factura física manuscrita del Hotel Prado del Huila, esta última emitida a nombre de la persona natural titular, Natalia Andrea Díaz Céspedes, régimen no responsable de IVA), se confirmó por evidencia de CloudWatch que ni la Lambda ni /facturas capturaban el NIT del proveedor en ningún momento -- no es que el dato se perdiera en el camino, es que nunca se pidió. El resumen de log solo imprimía Proveedor, Fecha, Total, Impuesto e Items (ver `lambda_procesar_factura.py`, función `lambda_handler`); el NIT no aparecía ni ahí ni en ninguna columna de `factura`.

Antes de tocar código, se verificó contra la documentación oficial de Textract cuál es el campo estándar correcto para este dato, en vez de asumir un nombre por analogía con `VENDOR_NAME` -- un supuesto que habría sido plausible pero incorrecto: no existe un campo `VENDOR_TAX_ID`. El campo real es `TAX_PAYER_ID`, genérico para cualquier país (a diferencia de `VENDOR_VAT_NUMBER`, `VENDOR_GST_NUMBER`, `VENDOR_ABN_NUMBER` o `VENDOR_PAN_NUMBER`, que son esquemas tributarios específicos de otras regiones). Como `extraer_campos_generales()` ya recorre todos los `SummaryFields` que Textract devuelve sin filtrar por tipo, no hizo falta tocar esa función -- `campos.get('TAX_PAYER_ID')` alcanza para leerlo.

Misma implementación en tres piezas que la sección 5.28 (impuesto): primero, en RDS, `ALTER TABLE factura ADD COLUMN nit VARCHAR`, permitiendo nulos y sin valor por defecto, pendiente de que el usuario lo ejecute vía psql -- no se aplicó desde aquí porque esta sesión no tiene acceso a la base de datos ni a la cuenta de AWS. Segundo, en la Lambda: se agregó la extracción de `TAX_PAYER_ID` a `guardar_en_rds()` y al log de resumen, guardado como texto sin limpiar (a diferencia de `limpiar_numero()`, aquí no aplica ninguna transformación -- un NIT es un identificador, no una cantidad, así que los puntos y el guion se conservan tal como Textract los devuelve). Tercero, en `/facturas`: una línea "NIT: X" bajo el nombre del proveedor, visible solo cuando el dato existe -- mismo criterio que la línea de impuesto, para que las facturas procesadas antes de este cambio no muestren un campo vacío.

**Pendiente, igual que con el impuesto en su momento (sección 5.28):** el backfill del NIT para las 48 facturas ya procesadas antes de este cambio. Es parcialmente recuperable sin volver a subir las imágenes, porque `datos_textract_raw` guarda desde el principio el JSON crudo de `campos` -- para las facturas donde Textract sí detectó `TAX_PAYER_ID` en su momento, el valor ya está ahí, solo sin extraer a su propia columna.

**Pendiente de despliegue real**, no solo de código: tanto la Lambda como `app.py` necesitan desplegarse de nuevo después de este cambio (subir la función en la consola de Lambda, y actualizar `app.py` en las instancias del Auto Scaling Group) -- la sección 5.30 ya documentó que sincronizar a GitHub no equivale a desplegar, así que este paso no se puede dar por hecho.

## 6. Estado actual del proyecto

La versión 1 está completa, verificada de extremo a extremo y publicada como repositorio público en GitHub (ver el documento de la v1). No tiene pendientes técnicos abiertos.

La versión 2 tiene sus dos componentes principales construidos, desplegados en producción (las mismas instancias EC2 detrás del Auto Scaling Group y el ALB de la v1) y verificados con datos y preguntas reales: el panel de control visual (/panel) con sus cuatro tarjetas de indicadores, la gráfica de Chart.js y las tablas de proveedores, ítems e incidencias de datos; y el asistente conversacional (/asistente) sobre Amazon Bedrock (Claude Haiku 4.5, vía el perfil de inferencia global), con las diez herramientas de consulta definidas en la sección 5.3 conectadas mediante el patrón tool use de la Converse API.

En el camino se encontraron y corrigieron seis problemas reales de la primera etapa de construcción, cada uno documentado con su causa y su corrección en la bitácora anterior: dos bugs de datos (nombres de proveedor fragmentados por saltos de línea, y una suma nula latente), una condición de carrera de despliegue (502 por reiniciar el servicio antes de que terminara la copia desde S3), dos lecciones operativas de consistencia entre instancias del Auto Scaling Group (parchado manual incompleto, y la casilla Skip matching del Instance Refresh), y un bug de integración con la Converse API de Bedrock (arreglos vs. objetos en el campo json de un toolResult).

Adicionalmente, el asistente ya tiene memoria conversacional de varias vueltas (sección 5.15, con el historial viviendo en el navegador y no en el servidor, por la misma razón de consistencia entre instancias) y el diagrama de arquitectura de la v2 (sección 4.1) ya está elaborado. Las diez herramientas de consulta definidas en la sección 5.3 quedaron ejercitadas al menos una vez con una pregunta real (secciones 5.14 y 5.16).

Esa ronda final de pruebas expuso una segunda familia de problemas, distinta de los seis anteriores: la confiabilidad del propio modelo al resumir o agregar en prosa los datos que las herramientas le devuelven correctamente. En sucesivas iteraciones (secciones 5.18 y 5.19) se corrigieron un conteo mal agrupado, un sub-conteo de facturas incompletas, y una cifra fabricada al asignarle un valor a una factura sin total detectado -- esta última, la más seria, mediante dos reglas (9 y 10) que atacan la causa de raíz: no dejar que el modelo agregue en prosa datos que una herramienta SQL ya puede calcular de forma confiable. Se corrigió también, por separado, la legibilidad de las respuestas del chat (un bug real de frontend que colapsaba los saltos de línea, sintaxis markdown mostrada literalmente, y formato inconsistente de cifras en pesos), mediante las reglas 11 y 12 (sección 5.20). Cada corrección de esta ronda se verificó repitiendo la misma pregunta en producción.

De los tres asuntos que habían quedado evaluados pero sin decisión, uno ya se implementó: /facturas ahora muestra un enlace a la imagen real de cada factura en S3 (sección 5.22). El trabajo resultó más simple de lo estimado en la sección 5.17 -- la columna s3_key ya existía y se guardaba desde la v1, así que no hizo falta tocar la Lambda ni el esquema de RDS ni hacer backfill -- y en el camino se corrigieron dos bugs adicionales: permisos de IAM insuficientes para leer las imágenes, y el texto "None" mostrándose literalmente en ítems sin dato detectado.

El proceso de agregar facturas colombianas reales (sección 5.21) ya está en marcha, y en la primera factura electrónica real probada (secciones 5.24 a 5.27) encontró exactamente el tipo de bug que se anticipó que solo datos reales podrían revelar: un formulario de subida que se colgaba con archivos PDF, y un error de interpretación de números que confundía la convención colombiana (punto de miles, coma decimal) con la estadounidense -- ambos corregidos y verificados en producción, junto con un incidente de despliegue no relacionado (un archivo truncado en S3).

De los dos asuntos que quedaban sin decisión, uno ya se implementó: el campo de impuesto por separado en /facturas (sección 5.28), aprovechando que las facturas electrónicas reales traen el IVA como un campo limpio y confiable -- columna impuesto nueva en RDS, extracción del campo TAX en la Lambda, y una línea visible en /facturas solo cuando el dato existe. Verificado en producción con la factura de Lilia Gasca, cuyo IVA de $0,00 (no es responsable de IVA) se mostró correctamente.

Sigue sin decisión ni implementación si redondear las cifras del asistente de forma determinística en las propias herramientas SQL en vez de dejarlo en manos del modelo (sección 5.20).

Se agregó una segunda validación no bloqueante en la Lambda, ahora a nivel de factura completa: la suma de subtotales de los ítems contra el total detectado (sección 5.29), con dos candidatos de comparación (con y sin impuesto) para no generar avisos falsos, y un log INFO nuevo para medir con datos reales qué tan seguido el total de una factura no incluye el impuesto por encima de los subtotales.

Esa validación quedó verificada de punta a punta con un caso real (sección 5.30): la Lambda desplegada generó el aviso esperado en CloudWatch al reprocesar una factura con un total que no cuadraba con su propia aritmética, y la misma reconciliación ya se muestra también directamente en /facturas -- calculada al momento de mostrar el panel, no solo al procesar, por lo que ya aplica a las facturas procesadas anteriormente sin necesidad de reprocesarlas.

Se implementó, y ya no queda como mejora opcional pendiente, el segundo canal del asistente por WhatsApp sobre el Sandbox de Twilio (secciones 5.31 y 5.32) -- misma lógica de herramientas y Bedrock que `/asistente`, con memoria conversacional propia en RDS y validación oficial de firma de Twilio. El despliegue expuso y corrigió cuatro problemas reales: un permiso de IAM insuficiente, una dependencia faltante en una de las dos instancias, un hallazgo de infraestructura no anticipado (el Auto Scaling Group reemplaza automáticamente instancias que el Target Group marca "Unhealthy", deshaciendo cambios manuales en curso), y una URL de webhook mal configurada. Verificado con una pregunta real respondida correctamente por WhatsApp.

## 7. Próximos pasos

- Sincronizar a GitHub el código del canal de WhatsApp implementado en la sección 5.31 (ruta `/whatsapp-webhook`, tabla `whatsapp_historial`, dependencia `twilio` en `requirements.txt`) -- por ahora solo existe desplegado en las instancias EC2.
- Evaluar, sin fecha de cierre fija, si vale la pena rellenar (backfill) el impuesto de las facturas procesadas antes de la sección 5.28, a partir del JSON crudo que la Lambda ya guarda en datos_textract_raw (tipo jsonb) desde el principio, sin tener que volver a subir las imágenes (sección 5.28).
- Agregar un lote pequeño de facturas colombianas reales como prueba dirigida a casos específicos del contexto local (IVA como renglón separado, fechas DD/MM/AAAA, NIT), sin borrar todavía los datos de prueba actuales; dejar la limpieza completa de datos como paso final antes de la versión definitiva del proyecto (sección 5.21).
- Evaluar, como mejora de calidad de datos y no de esta etapa, una comparación difusa (fuzzy matching) de nombres de proveedor, para consolidar variantes del mismo proveedor real que Textract extrae como textos distintos (secciones 5.5 y 5.14).
- Evaluar, como mejora de robustez de menor prioridad, que las herramientas SQL devuelvan las cifras ya formateadas en pesos colombianos (reutilizando formatear_numero) en vez de dejar que el modelo las redondee él mismo al escribir el texto (sección 5.20).
- Evaluar, si el uso real de la memoria conversacional lo justifica, ajustar HISTORIAL_MAXIMO_MENSAJES (hoy en 10 -- 5 preguntas y 5 respuestas) según cómo se comporte el costo real de tokens de entrada en la práctica.
- Revisar en unas semanas, con datos reales de producción, los logs INFO agregados en la sección 5.29 -- qué proporción de facturas cae en "el total no incluye el impuesto" -- para confirmar o corregir la suposición de que eso es la excepción y no la regla.
- Borrar el registro duplicado de la factura de Dotaciones Gamero (factura_id 46, de la primera prueba con peor resolución) antes de depurar los datos de prueba (sección 5.21) -- se dejó ambas versiones mientras se verificaba la sección 5.30.
