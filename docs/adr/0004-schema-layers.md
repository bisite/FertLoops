---
status: accepted
---

# Capas del esquema: núcleo, extensiones, semilla

El esquema de la base de datos se divide en **capas independientes**, cada una con su propio directorio de migraciones. La regla es una sola: **el núcleo no depende de nada que pueda faltar.**

| Capa | Directorio | Contenido |
| --- | --- | --- |
| Núcleo | `db/migrations/core/` | Las 8 tablas del dominio, en PostgreSQL estándar y sin extensiones |
| Extensiones | `db/migrations/extensions/<nombre>/` | Todo lo que dependa de los tipos o funciones de una extensión |
| Semilla | `db/migrations/seed/` | El vocabulario cerrado del proyecto: contextos, unidades y tipos de sensor |
| Aplicación | `db/migrations/app/` (prevista, aún no existe) | El esquema propio: riego, fertilización, control |

## Por qué el núcleo se mantiene en SQL estándar

`TIMESTAMPTZ`, `NUMERIC`, `CHECK`, `FOREIGN KEY` y nada más. `gen_random_uuid()` es nativo desde PostgreSQL 13, así que ni eso hay que declarar.

Las extensiones son más frágiles y de ciclo más rápido que PostgreSQL, y aquí hay dos en juego ([ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md), [ADR-0010](0010-toolkit-layer-and-tiering-exclusion.md)). Mantener el núcleo libre de ellas tiene tres efectos concretos:

- **Se puede inspeccionar y aplicar sin levantar nada más.** Un `psql` contra un PostgreSQL a secas basta para leer y probar el modelo entero.
- **Lo que depende de una extensión está señalizado.** Si algún día una de ellas cambia o se abandona, se sabe exactamente qué ficheros hay que revisar: los de su directorio.
- **El análisis estático funciona.** El DDL de TimescaleDB no lo parsea una gramática de postgres a secas, así que las capas de extensión quedan fuera del linter (`.sqruffignore`). El núcleo y la semilla, que son SQL plano, sí se analizan.

La contrapartida: **la capa de extensión modifica lo que el núcleo creó.** Convertir `reading` en hipertabla obliga a cambiar su clave primaria y a eliminar una clave ajena. Está documentado en [ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md), pero implica que leer solo el núcleo da una imagen incompleta de lo que hay desplegado.

## Procedencia

El núcleo no se diseñó para FertLoops: se hereda de una plantilla interna del grupo, adaptada aquí. Eso explica dos cosas que de otro modo desconciertan al leer el SQL:

- **Los comentarios del SQL están en inglés** mientras la documentación va en español. Es la convención de `AGENTS.md` —código e identificadores en inglés, prosa de equipo en español— y los comentarios heredados se conservan tal cual.
- **Hay generalidad que este proyecto no usa.** `location` y `device_placement` son el caso más visible: existen en el núcleo pero la adscripción real de Device a Mesa de drenaje **no pasa por ellas** ([ADR-0011](0011-canonical-measurement-model.md)). No se eliminan —vacías no cuestan nada y `location` servirá si algún día hay un segundo emplazamiento— pero conviene saberlo antes de usarlas por error.

Lo que se hereda es **el SQL y su razonamiento**, ambos recogidos en los ADR 0005 a 0010. No hay dependencia de código, ni de compilación, ni de red: este repositorio contiene el esquema completo y se aplica solo.

## Consecuencias

- **Aplicar el esquema son cuatro órdenes, no una**, una por capa, con opciones distintas. Ver [`db/migrations/README.md`](../../db/migrations/README.md) y [ADR-0008](0008-golang-migrate-one-source-per-layer.md).
- **Una capa de extensión futura** —PostGIS, por ejemplo— sigue el mismo patrón: su directorio y su propia tabla de seguimiento de versiones.
- **El esquema todavía no cubre el dominio completo.** Solo sabe de medidas: no hay tablas de eventos de riego ni de fertilización, ni nada que represente el Modo de control. Ver [ADR-0006](0006-what-core-deliberately-does-not-model.md).
