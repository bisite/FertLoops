---
status: accepted
---

# `reading` es de solo añadido; las correcciones son filas nuevas

Las filas de `reading` **nunca** se actualizan ni se borran. Una medida equivocada —calibrado malo, fallo de decodificación— se corrige insertando una fila nueva con `corrects_reading_id`, una autorreferencia anulable que apunta a la que corrige. La original queda intacta para siempre.

Es la garantía más fuerte disponible para durabilidad y trazabilidad: **no hay nada que registrar en un log de auditoría, porque nada muta.**

## Deliberadamente mínimo

No hay estado de corrección, ni flujo de aprobación, ni motivo: solo la autorreferencia. Cómo resuelve un consumidor «cuál es la verdad actual para este instante» se deja a quien consulte.

## Cómo lo refuerza el esquema

- `uq_reading_sensor_observed_at_original` — índice único **parcial**: una medida original es única por sensor e instante. Las correcciones están exentas a propósito, porque comparten `sensor_id` y `observed_at` con la que corrigen. Es también lo que hace idempotente la reinyección de una cola tras un corte ([ADR-0013](0013-clock-policy-and-timestamping.md)).
- `idx_reading_corrects_reading_id` — parcial sobre las filas que corrigen algo, que son la minoría.

## Consecuencias

- **Coincide con lo que ya exigía la compresión columnar.** [ADR-0001](0001-timescaledb-container-as-measurement-store.md) observó que sobre *chunks* comprimidos un `UPDATE` en su sitio no es una operación normal, y que las correcciones deben modelarse como filas nuevas. La restricción operativa y el diseño del esquema apuntan en la misma dirección.
- **Que el índice sea parcial obliga a filtrar en los agregados.** Original y corrección conviven en `reading` para siempre, así que sumarlas cuenta el instante dos veces. Ver [ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md).
- **La capa TimescaleDB elimina la clave ajena autorreferenciada.** La columna y su intención no cambian; lo que desaparece es la comprobación en la base de datos. Ver [ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md).
