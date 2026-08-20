---
status: accepted
---

# Lo que el esquema deliberadamente no modela

Quien venga de cualquier esquema IoT esperará campos que aquí no están. Se enumeran para que se lean como omisiones deliberadas y no como despistes, y para que nadie los «arregle» añadiéndolos.

## Sin propiedad ni control de acceso

`device` no tiene referencia a usuario, propietario ni tenant. El núcleo modela que un Device existe y produce medidas, nunca quién puede verlo o gestionarlo.

El control de acceso es política, y varía por proyecto. Vive en la capa de aplicación, unida a este esquema por la convención de identificadores que se elija. Es asunto de [#13](https://github.com/bisite/FertLoops/issues/13).

## Sin separación entre valor crudo y valor derivado

`reading` tiene una sola columna `value`: la medida limpia. Se consideró guardar también el valor crudo del Device para protegerse de que las fórmulas de calibrado cambien, y se descartó: este esquema es el sumidero de datos **ya validados**, y la validación ocurre aguas arriba, en la tubería de ingesta ([ADR-0012](0012-bento-as-ingestion-pipeline.md)).

Quien necesite conservar las tramas crudas y recalcular desde ellas lo hace en la ingesta, fuera de este esquema.

## Sin campo de estado en `device`

Se consideró un `status` o `decommissioned_at` y se descartó: sería estado redundante que puede desincronizarse del historial que ya lo implica. Que un Device esté o no en servicio se infiere de si tiene una adscripción activa a una Mesa de drenaje ([ADR-0011](0011-canonical-measurement-model.md)).

## Sin geometría ni polígonos

`location` guarda `latitude`/`longitude` como `NUMERIC(9, 6)` con `CHECK` de rango, asumiendo WGS84 por convención y sin metadatos de SRID. Los polígonos y el indexado espacial son trabajo de PostGIS, que viene incluido en la imagen elegida pero **alrededor del cual no se diseña nada** ([ADR-0001](0001-timescaledb-container-as-measurement-store.md)): queda disponible como capa de extensión si algún día hace falta.

## Sin calidad del dato, sin errores y sin estado de control *en el núcleo*

Tres huecos del esquema heredado que **sí están resueltos**, pero fuera del núcleo:

- **No distingue «no medido» de «fuera de rango» de «sensor en fallo».** `reading.value` es `NOT NULL`: o hay número, o no hay fila.
- **No tiene sitio para los códigos de error del Device**, porque su vocabulario solo contempla medios medidos —suelo, agua, aire— y un error no lo es.
- **No distingue un actuador de un sensor**, así que el estado de válvula e inversor no tiene dónde ir.

Los tres se resuelven en [ADR-0011](0011-canonical-measurement-model.md) sin tocar `reading`: los errores y el Eco de control pasan a ser series con un contexto `equipment` nuevo, «sensor en fallo» se deriva de esas series, «no medido» es la ausencia de fila, y «fuera de rango» va a Cuarentena.

## Consecuencias

- **El esquema solo sabe de medidas.** No hay tablas de eventos discretos de riego ni de fertilización, ni nada que represente el Modo de control o las decisiones del motor de decisión. Pero [ADR-0001](0001-timescaledb-container-as-measurement-store.md) eligió PostgreSQL precisamente por poder «cruzar series de medidas con eventos discretos de riego y fertilización»: **ese cruce todavía no tiene con qué cruzarse.** Modelarlo es trabajo pendiente, y depende de [#9](https://github.com/bisite/FertLoops/issues/9) y [#10](https://github.com/bisite/FertLoops/issues/10); irá a `db/migrations/app/` ([ADR-0004](0004-schema-layers.md)).
- **La Mesa de drenaje no es una entidad del núcleo**, y se decidió que sea de la capa de aplicación: ver [ADR-0011](0011-canonical-measurement-model.md). Eso deja `location` y `device_placement` del núcleo **sin consumidor real**.
