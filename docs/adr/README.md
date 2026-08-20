# Decisiones de arquitectura

Un ADR por decisión grande, en orden cronológico. Cada uno registra qué se
decidió, por qué, y qué se descartó — no cómo se implementa.

Para orientarse rápido, están agrupados por tema. **Si solo vas a leer
tres**: [0011](0011-canonical-measurement-model.md) (qué es una medida),
[0004](0004-schema-layers.md) (cómo está partido el esquema) y
[0001](0001-timescaledb-container-as-measurement-store.md) (dónde se
guardan las medidas).

## Infraestructura y despliegue

| ADR | Decisión |
| --- | --- |
| [0001](0001-timescaledb-container-as-measurement-store.md) | PostgreSQL con TimescaleDB en contenedor como almacén de medidas |
| [0002](0002-mqtt-mosquitto-bridge-as-pi-vps-transport.md) | MQTT con Mosquitto como transporte entre el Gateway y el VPS |
| [0003](0003-docker-compose-with-native-caddy-as-vps-deployment-model.md) | Docker Compose para todo salvo el proxy inverso, que va nativo |
| [0012](0012-bento-as-ingestion-pipeline.md) | Bento como tubería de ingesta entre el broker y la base de datos |

## Forma del esquema

| ADR | Decisión |
| --- | --- |
| [0004](0004-schema-layers.md) | Capas del esquema: núcleo, extensiones, semilla |
| [0005](0005-core-schema-keys-timestamps-vocabularies.md) | Claves, marcas de tiempo y vocabularios del núcleo |
| [0006](0006-what-core-deliberately-does-not-model.md) | Lo que el esquema deliberadamente no modela |
| [0007](0007-append-only-readings-corrections.md) | `reading` es de solo añadido; las correcciones son filas nuevas |
| [0008](0008-golang-migrate-one-source-per-layer.md) | golang-migrate, una fuente independiente por capa |

## Series temporales

| ADR | Decisión |
| --- | --- |
| [0009](0009-timescaledb-layer-hypertable-and-aggregates.md) | Capa TimescaleDB: hipertabla, compresión y agregados |
| [0010](0010-toolkit-layer-and-tiering-exclusion.md) | TimescaleDB Toolkit como capa aparte |

## Modelo de dominio

| ADR | Decisión |
| --- | --- |
| [0011](0011-canonical-measurement-model.md) | Modelo canónico de una medida |
| [0013](0013-clock-policy-and-timestamping.md) | Política de relojes, sellado de tiempo y frescura |

## Cómo escribir uno nuevo

Numeración secuencial, nombre de fichero en inglés y kebab-case, cuerpo en
español. Frontmatter con `status: accepted`. Un ADR merece existir cuando
la decisión es **difícil de revertir**, **sorprendente sin contexto** y
**resultado de un compromiso real** — si falta alguna de las tres, basta un
comentario en el código o en la issue.
