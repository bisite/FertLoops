---
status: accepted
---

# golang-migrate, una fuente independiente por capa

Las migraciones se aplican con [golang-migrate](https://github.com/golang-migrate/migrate), que ejecuta ficheros `.sql` versionados tal cual, sin lenguaje de migración propio. Los ficheros siguen siendo SQL legible por `psql`, así que adoptarla no ata el esquema a ninguna herramienta. Versiones como entero secuencial, no marcas de tiempo.

## Una fuente por capa

golang-migrate **no puede mezclar varias fuentes en una ejecución**, y cada fuente lleva un historial lineal en su propia tabla de seguimiento. Como las capas son independientes ([ADR-0004](0004-schema-layers.md)), cada una es su propia fuente, aplicada como una invocación aparte:

| Fuente | Obligatoria | Tabla de seguimiento |
| --- | --- | --- |
| `db/migrations/core/` | sí | `schema_migrations` |
| `db/migrations/extensions/timescaledb/` | sí en la práctica | `schema_migrations_ext_timescaledb` |
| `db/migrations/extensions/timescaledb-toolkit/` | no | `schema_migrations_ext_timescaledb_toolkit` |
| `db/migrations/seed/` | sí para operar | `schema_migrations_seed` |

`seed` es la excepción conceptual: **no crea nada, solo puebla.** Sin ella el esquema no admite ni una medida. Está aparte porque sus filas van en tablas del núcleo pero sus valores son del proyecto.

Las órdenes exactas están en [`db/migrations/README.md`](../../db/migrations/README.md).

## `x-multi-statement` no significa lo que parece

Es la trampa operativa de esta herramienta. **No** envuelve el fichero en una transacción: lo parte por `;` y ejecuta cada sentencia con su propio autocommit. Con el separador siendo ingenuo, el valor correcto depende de la fuente. Ambas reglas están comprobadas contra la imagen real:

- **`core` y `seed` — NO poner la opción.** `core` define la función pl/pgsql `set_updated_at()`, cuyo cuerpo contiene puntos y coma; el separador la cortaría por la mitad y falla con `unterminated dollar-quoted string`. Sin la opción, el fichero entero va en una llamada y PostgreSQL lo interpreta bien. En `seed` además interesa que toda la siembra sea una sola transacción.
- **`timescaledb` — hay que poner `x-multi-statement=true`.** No por el motivo habitual: que `CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)` no pueda ir en una transacción **ya no es cierto** en la versión que usamos, y la dirección `up` se aplica bien de las dos formas. La opción es necesaria por la dirección **`down`**: el fichero de reversión de los agregados quita las políticas de refresco y de columnstore y después tira las vistas, y en modo fichero-completo eso es una sola transacción, así que las bajas nunca se confirman antes del `DROP`. El planificador de trabajos no las ve, un trabajo a medio ejecutar compite con el `DROP` y falla con `tuple concurrently deleted`. Medido: **2 de 3 reversiones fallaron sin la opción, 7 de 7 salieron bien con ella.**

  Por eso los ficheros de esa capa se mantienen «seguros para trocear»: sin funciones pl/pgsql y **sin `;` en medio de una línea de comentario**.
- **`timescaledb-toolkit`** es un único `CREATE EXTENSION`, así que da igual.

## Enmendar en el sitio, hasta el primer despliegue

Estas migraciones se pueden **enmendar en el sitio** —editar `000001` en lugar de añadir un `000002`— mientras ningún despliegue las haya aplicado. Deja una base limpia en vez de un rastro de correcciones que alteran y vuelven a alterar, y golang-migrate nunca reejecuta una versión ya aplicada, así que la edición solo alcanza a bases creadas después.

**En cuanto el esquema se aplique a un despliegue real, se termina: a partir de ahí es estrictamente de solo añadido.** El cambio de régimen se anota en el README de las migraciones con su fecha, porque después una edición en el sitio desvía en silencio la base desplegada respecto a los ficheros.

## Consecuencias

- **Aplicar el esquema completo son cuatro órdenes**, cada una con su cadena de conexión y sus opciones. Equivocarse con `x-multi-statement` no avisa bien: en `core` corta el fichero, y en `timescaledb` da un fallo **intermitente** al revertir, que es peor porque pasa las veces suficientes para parecer correcto.
- **El orden importa y la herramienta no lo impone.** `core`, después `timescaledb`, después `timescaledb-toolkit`; `seed` en cualquier momento tras `core`.
- **Revertir `seed` falla a propósito si hay datos que dependan de él**, porque las claves ajenas hacia `sensor_type` son `ON DELETE RESTRICT`. Comprobado. Y deja la versión en **estado sucio**, así que hay que `force` antes de reintentar.
- **No hay ninguna prueba automática que vigile nada de esto.** Todo lo comprobado es puntual y hay que repetirlo a mano al cambiar una migración; está listado en el README de las migraciones. Montar CI es parte de [#16](https://github.com/bisite/FertLoops/issues/16).
