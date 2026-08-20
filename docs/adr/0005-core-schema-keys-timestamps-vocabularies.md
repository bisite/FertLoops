---
status: accepted
---

# Claves, marcas de tiempo y vocabularios del núcleo

Tres decisiones que atraviesan todas las tablas de `db/migrations/core/`.

## Claves primarias: UUID, salvo en `reading`

**UUID (`gen_random_uuid()`) en las siete tablas de entidad.** Se consideraron claves enteras —más pequeñas y rápidas de indexar— y se descartaron porque colisionan al combinar datos de despliegues creados por separado: `device` n.º 42 significa algo distinto en cada uno, y el fallo solo aparece cuando alguien intenta cruzarlos, que es el peor momento para descubrir un error de clave primaria. A este volumen la diferencia de tamaño es irrelevante.

**`reading` usa `BIGINT GENERATED ALWAYS AS IDENTITY`.** Es la excepción y refina la regla, no la contradice: nada fuera de `reading` referencia `reading.id` salvo su propia autorreferencia `corrects_reading_id` ([ADR-0007](0007-append-only-readings-corrections.md)), que siempre es interna a la misma base de datos. No hay requisito de identidad global que justifique el coste, y aquí sí se paga: es la tabla de más volumen y más intensiva en inserciones —del orden de 294.000 filas al día con las 12 mesas de drenaje ([ADR-0011](0011-canonical-measurement-model.md))— y una clave UUID aleatoria dispersa las inserciones por el árbol B, empeorando la localidad y engordando el índice. Ocho bytes secuenciales en lugar de dieciséis aleatorios, justo donde importa.

## Marcas de tiempo

Todas las tablas llevan `created_at` y `updated_at` con `DEFAULT now()`, y un disparador `BEFORE UPDATE` que mantiene `updated_at` mediante la función compartida `set_updated_at()`.

**`reading` es la excepción: solo `created_at`.** Sus filas no se mutan nunca ([ADR-0007](0007-append-only-readings-corrections.md)), así que `updated_at` sería una columna que no puede llevar información.

En cambio `reading` sí lleva **dos** marcas con significados distintos, y la distinción es load-bearing:

- **`observed_at`** — cuándo se tomó la medida, sellado por el Gateway. Es el tiempo del evento y la columna de partición de la hipertabla.
- **`created_at`** — cuándo se insertó la fila. Sirve además como señal de «cuándo nos enteramos» para las subidas retrasadas tras un corte del enlace, que aquí son la norma.

Qué reloj sella `observed_at`, qué margen de error arrastra y qué instantes se rechazan por implausibles está en [ADR-0013](0013-clock-policy-and-timestamping.md). La zona horaria de agregación, en [ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md).

## Vocabularios: tablas, no enumerados

`sensor_context` (el medio al que aplica un tipo de sensor), `sensor_type` (qué magnitud, en qué unidad, en qué contexto) y `unit` (símbolo y nombre) son **tablas**. Añadir un contexto, un tipo o una unidad es insertar una fila, no migrar el esquema.

Para `unit` se descartaron dos alternativas:

- **Texto libre en `sensor_type`.** A lo largo de años y proyectos deriva —«C», «Celsius», «°C» significando lo mismo— y fragmenta en silencio cualquier consulta que agrupe por unidad.
- **Normalización forzada a SI al escribir.** Reintroduce lógica de conversión dentro del esquema, que es lo que [ADR-0006](0006-what-core-deliberately-does-not-model.md) excluye: si la conversión está mal, corrompe en silencio el valor que este almacén existe para guardar.

## Consecuencias

- **`set_updated_at()` obliga a aplicar `core` sin `x-multi-statement`.** Su cuerpo pl/pgsql contiene puntos y coma y el separador de golang-migrate es ingenuo. Ver [ADR-0008](0008-golang-migrate-one-source-per-layer.md).
- **La unicidad de `sensor` es estructural, no ordinal.** Dos sensores del mismo tipo en el mismo Device son dos filas con UUID distintos; no hay número de secuencia que mantener.
- **El vocabulario hay que poblarlo antes de insertar una sola medida**, y vive en `db/migrations/seed/` ([ADR-0011](0011-canonical-measurement-model.md)). Dos unidades ya las fija el contrato UART y no se vuelven a decidir: la conductividad del **agua** llega en **ppm** y la del **suelo** en **mS/cm**. Son magnitudes distintas, no la misma en dos unidades, y por eso la clave única de `sensor_type` es `(contexto, magnitud, unidad)`.
