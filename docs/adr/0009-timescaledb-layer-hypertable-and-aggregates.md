---
status: accepted
---

# Capa TimescaleDB: hipertabla, compresión y agregados

`db/migrations/extensions/timescaledb/` convierte `reading` en hipertabla comprimida (versión 1) y le añade agregados continuos horario y diario (versión 2). Materializa en el esquema lo que [ADR-0001](0001-timescaledb-container-as-measurement-store.md) decidió a nivel de infraestructura.

`reading` es la candidata evidente: la tabla de más volumen, más intensiva en inserciones y de solo añadido, con clave temporal, insertada en orden cronológico y ya consultada por `(sensor_id, observed_at)`.

## Dos cambios que TimescaleDB impone

Se aplican **solo en esta capa**; el núcleo no los conoce.

- **La clave primaria pasa de `id` a `(id, observed_at)`.** TimescaleDB exige que toda restricción única de una hipertabla incluya la columna de partición. `id` sigue siendo único en la práctica —sale de una única secuencia— pero deja de estar comprobado.
- **Se elimina la clave ajena autorreferenciada de `reading`.** TimescaleDB no soporta claves ajenas hacia una hipertabla, incluida una tabla hacia sí misma. La columna `corrects_reading_id` y su intención no cambian ([ADR-0007](0007-append-only-readings-corrections.md)); la integridad referencial de las correcciones pasa a ser responsabilidad de la aplicación. **Es la consecuencia más importante de esta capa y la más fácil de olvidar.**

## Particionado y compresión

**Columna de partición: `observed_at`**, el tiempo del evento. Es la columna por la que el esquema ya consulta, aunque implique que una subida retrasada tras un corte inserte en un *chunk* viejo — que aquí es el caso esperado, no el excepcional.

**Intervalo de *chunk*: 7 días.** Con el volumen conocido son unos cientos de miles de filas por *chunk*, holgadamente por debajo del criterio de que los índices de los *chunks* recientes quepan en menos del 25 % de la RAM.

**`segmentby = 'sensor_id'`**, la dimensión de filtrado primaria; **`orderby = 'observed_at DESC'`**; **`sparse_index = 'minmax(value)'`** para que las consultas de rango no descompriman.

**Compresión a los 14 días, y hay que pedirla explícitamente.** Activar el columnstore solo la hace *posible*, no la programa: comprobado, tras `SET (timescaledb.enable_columnstore = true)` la hipertabla se queda con **cero** trabajos. Dejar la llamada comentada significaba que las medidas **no se comprimían nunca**. Va activa, y a 14 días en lugar de 7 porque un Gateway que se reconecta reinyecta medidas con `observed_at` viejo, y dejar un intervalo de *chunk* de margen evita que cada reconexión fuerce descomprimir y recomprimir.

## Agregados continuos

`reading_hourly` se construye sobre `reading`; `reading_daily` **sobre `reading_hourly`**, no sobre las crudas, así que el diario re-agrega unas 24 filas ya resumidas por sensor y día en lugar de reescanear.

**Solo originales.** Los dos filtran `WHERE corrects_reading_id IS NULL` —el diario lo hereda del horario—. No es preferencia, es corrección de error: el índice único es **parcial**, así que original y corrección conviven en `reading` para siempre y sin filtrar se cuenta el instante dos veces. Comprobado: una corrección de valor 42,0 llevaba el máximo diario a 42,0, un valor que ninguna medida válida alcanzó, y con filtro vuelve al 23,149 real.

Lo que estos agregados **no** hacen es *resolver* las correcciones: un instante corregido no aporta nada en lugar de aportar el valor corregido. Y la escapatoria obvia no existe: **un agregado continuo solo puede definirse sobre una hipertabla o sobre otro agregado continuo, nunca sobre una vista normal.** Para resolución exacta hay que consultar `reading` directamente.

**El día es el día local.** `reading_daily` agrupa con `timezone => 'Europe/Madrid'`. Sin ese argumento, `time_bucket` ancla los límites en las 00:00 UTC —su parámetro vale `UTC+0` por defecto y, a diferencia de `date_trunc`, **ignora la zona horaria de la sesión**, así que ninguna configuración del servidor lo corrige—. En España eso archiva la primera hora de cada día local en el día anterior: **el agua consumida tras la medianoche local se acredita a la jornada anterior, todos los días.** Medido sobre el cambio de hora, una consulta por UTC devolvía 1440 minutos donde el día local tiene 1380. La zona va como nombre IANA, nunca como desplazamiento fijo, que es justo lo que se rompe al cambiar la hora. `reading_hourly` no la lleva ni le hace falta: una hora es una hora.

**`sum_value` en los dos rollups.** Para la mayoría de magnitudes una suma no significa nada, pero para las de tipo incremento es la métrica que se quiere: el volumen de riego llega como litros del intervalo y su total del día es una suma. Guardarla simplifica además la media diaria, que sale de `SUM(sum_value) / NULLIF(SUM(reading_count), 0)` en vez de multiplicar medias por conteos. Ninguna columna vale para todas las series: la función correcta depende de la magnitud y la fija [ADR-0011](0011-canonical-measurement-model.md).

**Agregación en tiempo real solo en el horario**, para que «la hora en curso» se vea de inmediato, aceptando escanear filas crudas recientes. En el diario no aplica: un día está incompleto para «hoy» de todas formas.

**Refresco sin límite** (`start_offset => NULL`) en los dos, precisamente para que lo que llegue tarde tras un corte se reagregue solo.

## Consecuencias

- **La retención se deja sin fijar, y la recomendación es no ponerla nunca.** Medido sobre el modelo definitivo: 8,91 M filas al año por mesa de drenaje, **2171 MB → 42 MB comprimidos (51,3×)**, unos **508 MB/año** para las 12 mesas. A ese tamaño, tirar las crudas ahorra decenas de megabytes y destruye el único registro sin corregir y a resolución completa que existe. Conservarlas es además lo que hace asequible el refresco sin límite. Ratificarlo es [#14](https://github.com/bisite/FertLoops/issues/14).
- **Un bucket diario con zona es de ancho variable** (23/24/25 h), y TimescaleDB **rechaza** apilar un agregado de ancho fijo encima de uno variable. Un futuro semanal o mensual tendrá que llevar zona también; variable sobre variable sí se permite. Comprobado.
- **El agregado diario queda específico del emplazamiento.** Un segundo módulo en otra zona horaria necesitaría el suyo.
- **Reinyectar en un *chunk* ya comprimido funciona y no lo descomprime.** Importa porque la cola local está diseñada para 15 días de corte y aquí se comprime a los 14. Medido: 28,7 ms frente a 20,8 ms para un día de una serie, y el *chunk* sigue comprimido. Y lo que más importaba: **el índice único parcial sigue rechazando duplicados dentro de un *chunk* comprimido**, así que la idempotencia de la ingesta sobrevive a la compresión.
- **La reversión reconstruye la tabla, no la «des-hipertabiliza».** TimescaleDB no tiene operación inversa, así que el `.down.sql` copia los datos a una tabla ordinaria y recrea a mano clave primaria, claves ajenas e índices con sus nombres (`CREATE TABLE ... LIKE` no copia ninguna de esas cosas). Funciona, pero a volumen de producción no es un rollback barato.
- **Revertir los agregados exige `x-multi-statement=true`** o compite con los trabajos en segundo plano. Ver [ADR-0008](0008-golang-migrate-one-source-per-layer.md).
- **Esta capa no tiene análisis estático.** Su DDL no lo parsea una gramática de postgres a secas, así que está excluida del linter: solo revisión humana.
