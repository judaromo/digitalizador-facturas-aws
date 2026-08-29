# Limitaciones conocidas

Este documento resume dos hallazgos del pipeline de extracción con Amazon
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

## Lección general

Un mismo síntoma visible (`None` en el panel) tenía dos causas de naturaleza
opuesta: una corregible en el código propio, y otra una limitación real del
servicio de OCR, ajena al código. Separar ambas causas con evidencia cruda
— agregando un log temporal que expone el dato tal como lo devuelve
Textract, antes de cualquier transformación — evitó tanto dejar sin corregir
un defecto real como perder tiempo intentando "arreglar" algo que, en el
fondo, la fuente de datos nunca entregó de forma legible.
