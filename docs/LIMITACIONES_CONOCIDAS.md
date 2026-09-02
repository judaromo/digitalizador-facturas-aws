# Limitaciones conocidas

Este documento resume tres hallazgos del pipeline de extracción con Amazon
Textract que se investigaron con evidencia real (logs de CloudWatch) durante
el desarrollo, en vez de suponer la causa a partir del síntoma visible.

## 1. Recibos con formato de dos líneas por ítem

Algunos recibos fotografiados imprimen cada ítem en dos líneas físicas: una
línea con la descripción, la cantidad y el precio, y una segunda línea
debajo con un descuento asociado (por ejemplo "Dcto. 50.00%"). En ese
formato, Textract mezcla texto y números entre ambas líneas al construir los
campos `QUANTITY` y `UNIT_PRICE`, devolviendo fragmentos que no son números
completos (por ejemplo `'.'`, `','`, `'18.05-'` o `'29: 00'`).

**Esto no es un defecto del código de este proyecto.** La función
`limpiar_numero()` recibe esos fragmentos, no encuentra ningún número válido
dentro de ellos, y devuelve `None` — el comportamiento correcto es no
inventar un número a partir de un texto sin sentido. El problema ocurre un
paso antes, en la lectura óptica de Textract: el dato crudo ya llega
incompleto o mezclado desde el origen.

**Por qué no se corrige aquí:** la alternativa real sería dejar de usar
Textract `AnalyzeExpense` (que interpreta la imagen como un documento de
gasto genérico) y migrar a Textract `AnalyzeDocument` con detección de
tablas, agregando lógica propia para agrupar cada fila de ítem con su
subfila de descuento antes de extraer los valores. Es un cambio
significativamente más complejo, y para un proyecto de portafolio donde el
resto de los formatos de factura probados ya se procesan correctamente, el
costo no se justifica frente al beneficio. Se documenta como limitación
conocida y aceptada, no como un pendiente a resolver.

**Alcance:** afecta únicamente a documentos con ese formato específico de
dos líneas por ítem. El resultado visible para el usuario es un campo en
blanco (`None`) en el panel de consulta para esos ítems puntuales, en vez de
un dato incorrecto — el sistema falla de forma segura.

**Mejora futura opcional**, solo si en el futuro aparecen muchas facturas
reales con este formato: migrar a Textract `AnalyzeDocument` más detección
de tablas, con agrupación de filas propia para unir cada ítem con su subfila
de descuento antes de extraer los valores numéricos.

## 2. Símbolos de moneda y unidades en campos numéricos (corregido)

La primera versión de `limpiar_numero()` solo quitaba el símbolo de euro
(`€`), pero no el de dólar (`$`) — usado tanto en facturas en inglés como,
más relevante para este proyecto, en pesos colombianos. Un valor como
`'$10.00'` nunca lograba convertirse a número, y tampoco se quitaban
palabras pegadas a la cantidad, como en `'500 units'`. A diferencia del caso
anterior, aquí el texto crudo entregado por Textract era perfectamente
legible — el problema estaba enteramente en la función de limpieza.

**Corrección aplicada:** se reemplazó el reemplazo literal de símbolos
específicos por una expresión regular que extrae el primer número válido
dentro del texto, sin importar qué símbolo de moneda o palabra lo rodee (ver
`lambda/lambda_procesar_factura.py`, función `limpiar_numero`).

**Costo aceptado:** la nueva versión es más permisiva. En el caso del recibo
con OCR ya corrompido (limitación #1), un fragmento como `'18.05-'` ahora sí
se convierte en `18.05` en vez de quedar en `None`, aunque ese número
provenía de una lectura ya mezclada entre dos filas. Se decidió aceptar este
costo porque resuelve el caso común y relevante (símbolos de moneda,
unidades) a cambio de un caso raro que de todas formas ya está documentado
por separado como limitación de Textract.

## 3. Fecha no detectada en facturas manuscritas con casillas separadas (DÍA/MES/AÑO)

Algunas facturas físicas escritas a mano imprimen la fecha en tres casillas
separadas con su propio encabezado impreso ("DÍA", "MES", "AÑO"), en vez de
como una sola cadena de texto legible de corrido (el formato que sí cubren
las facturas electrónicas o los recibos con la fecha impresa en una línea).
En ese formato, Textract `AnalyzeExpense` no devuelve ningún campo
`INVOICE_RECEIPT_DATE` — no es que el valor llegue mal formado, es que el
campo dedicado nunca se genera.

**Diagnóstico con evidencia real** (mismo método que la investigación del
NIT, sección 5.34 de la bitácora): se agregó temporalmente un log que
imprime todos los `SummaryFields` de Textract sin ningún filtro, y se
reprocesó una factura real con este formato (Hotel Prado del Huila /
Natalia Andrea Díaz Céspedes). La fecha sí quedó parcialmente detectada por
Textract, pero fragmentada entre varios campos genéricos `OTHER` de
confianza dispar y mezclada con el propio texto de los encabezados
impresos: `'MES AÑO\n2026.\noy'` (confianza 69.7%), `'04'` (confianza
82.4%), `'2026'` (confianza 90.5%) y `'15 2026.\noy'` (confianza 97.1%) —
cuatro fragmentos distintos para un solo dato, y ninguno es una fecha
completa y legible por sí solo.

**Por qué no se aplica aquí la misma heurística usada para el NIT:** la
heurística de la sección 5.34 funciona porque un NIT colombiano tiene una
forma reconocible casi sin ambigüedad (una tira de 9 o 10 dígitos), lo que
permite descartar el resto de los campos `OTHER` con confianza razonable.
Los fragmentos de fecha de este caso no tienen una señal distintiva
equivalente: `'04'` y `'15'` son igual de válidos como candidatos a día, no
hay ningún fragmento identificable con certeza como el mes (el propio
encabezado impreso "MES" quedó mezclado con el valor en vez de aparecer
separado), y el año aparece repetido en dos fragmentos distintos junto con
ruido no numérico (`'oy'`, probablemente un trazo o mancha mal leída).
Armar una fecha a partir de esto exigiría adivinar cuál de los dos
candidatos es el día y de dónde sale el mes — exactamente el tipo de
inferencia sin base que el proyecto evita deliberadamente en otros lugares
(reglas 9 y 10 del asistente, impuesto guardado como `NULL` en vez de 0). A
diferencia del NIT, aquí no hay una decisión razonable que priorice
cobertura sobre precisión: cualquier fecha armada a partir de estos
fragmentos tendría una probabilidad real de estar mal, sin ninguna forma de
detectarlo después.

**Alcance:** afecta únicamente a facturas físicas con la fecha escrita en
casillas separadas con encabezado impreso, no a fechas escritas o impresas
como una sola cadena (los formatos que sí cubre `parsear_fecha()`,
incluyendo mes en letras). El resultado visible para el usuario es un campo
de fecha en blanco (`NULL`) para esas facturas puntuales — mismo criterio
que la limitación #1: el sistema falla de forma segura en vez de mostrar
una fecha inventada.

**Mejora futura opcional**, solo si aparecen muchas facturas reales con
este formato: usar la geometría (posición en la página) que Textract sí
devuelve para cada campo, agrupando los fragmentos por su cercanía a los
encabezados impresos "DÍA", "MES" y "AÑO" en vez de solo por su forma de
texto — la misma idea de geometría que quedó evaluada y descartada por
ahora para el caso del NIT (sección 5.34), y por la misma razón: sin
evidencia todavía de que valga la inversión de complejidad frente a lo poco
frecuente del formato.

## Lección general

Un mismo síntoma visible (`None` en el panel) puede tener causas de
naturaleza distinta: una corregible en el código propio (limitación #2), y
dos limitaciones reales del servicio de OCR, ajenas al código, pero
distintas entre sí (limitaciones #1 y #3) — en un caso Textract mezcla el
dato entre dos líneas físicas del documento; en el otro, lo fragmenta entre
varios campos genéricos de baja confianza sin una señal que permita
recomponerlo con certeza. Separar estas causas con evidencia cruda —
agregando un log temporal que expone el dato tal como lo devuelve Textract,
antes de cualquier transformación — evitó tanto dejar sin corregir un
defecto real como perder tiempo intentando "arreglar" algo que, en el
fondo, la fuente de datos nunca entregó de forma recuperable con
confianza.
