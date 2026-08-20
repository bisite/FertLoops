# Aplicar estas migraciones con golang-migrate

Ver `docs/adr/0008-golang-migrate-one-source-per-layer.md` para el por qué
de esta herramienta y de esta disposición. Lo primero es instalar la CLI:
en [cmd/migrate](https://github.com/golang-migrate/migrate/tree/master/cmd/migrate)
están las opciones por plataforma (Homebrew, Scoop, `.deb`, `go install` o
un binario ya compilado).

Si se instala con `go install`, **hay que pasar el controlador de base de
datos como etiqueta de compilación** o todas las órdenes fallan con
`unknown driver postgres`:

```sh
go install -tags 'postgres' github.com/golang-migrate/migrate/v4/cmd/migrate@v4.19.0
```

## Levantar una base de datos para trabajar

Para desarrollo o para probar una migración, un contenedor desechable basta.
La imagen es la que fija docs/adr/0001, y **la etiqueta importa**: una
variante `-oss` no trae la edición Community y estas migraciones fallan a
medio aplicar (ver «Cómo se comprobó esto» al final).

```sh
docker run -d --name fertloops-db \
  -e POSTGRES_PASSWORD=... -e POSTGRES_DB=fertloops -p 5432:5432 \
  timescale/timescaledb-ha:pg18
```

El despliegue real no se hace así, sino con Docker Compose y escuchando solo
en `127.0.0.1` (docs/adr/0003).

## Disposición

- `db/migrations/core/` — obligatoria. Las tablas del dominio: dispositivos,
  sensores, tipos y contextos de sensor, unidades, emplazamientos,
  historial de colocación y medidas. PostgreSQL a secas, sin extensiones.
- `db/migrations/extensions/<nombre>/` — las capas de mejora
  (docs/adr/0004). Todo lo que dependa de los tipos o funciones de una
  extensión vive aquí, nunca en `core`.
- `db/migrations/seed/` — el vocabulario cerrado del proyecto: contextos,
  unidades y los 17 `sensor_type` del contrato UART. **No crea nada, solo
  puebla.** Sin ella el esquema no admite ni una medida, así que en la
  práctica es obligatoria para operar.

Cada directorio es una fuente de golang-migrate independiente con su propia
secuencia de versiones: golang-migrate no puede mezclar fuentes en una sola
ejecución, y cada una necesita su propia `x-migrations-table` para que sus
versionados no choquen en la misma base de datos.

Se aplican en este orden: `core`, después `timescaledb`, después
`timescaledb-toolkit`, y `seed` en cualquier momento tras `core` (solo
depende de las tablas de vocabulario). **Nada en golang-migrate lo impone**
— cada fuente ignora que las demás existen.

## Ficheros con varias sentencias: la opción cambia según la fuente

`x-multi-statement` de golang-migrate **no** envuelve el fichero en una
transacción: lo parte por `;` y ejecuta cada sentencia por separado, cada
una con su propio autocommit. Que eso convenga o no depende de la fuente.
Las dos reglas de abajo se comprobaron empíricamente contra
`timescale/timescaledb-ha:pg18` (PostgreSQL 18.4, TimescaleDB 2.27.2,
golang-migrate 4.19.0) — ver «Cómo se comprobó esto» al final.

- **`core` — NO poner `x-multi-statement`.** `000001_init_core_schema`
  define la función pl/pgsql `set_updated_at()`, cuyo cuerpo contiene
  puntos y coma; el separador de golang-migrate es ingenuo y la cortaría
  por la mitad. Sin la opción, golang-migrate envía el fichero entero en
  una sola llamada y PostgreSQL lo interpreta bien. Todas las sentencias
  del núcleo son compatibles con transacción, así que es seguro.
- **`timescaledb` — hay que poner `x-multi-statement=true`, y no por el
  motivo habitual.** La razón que suele darse es que
  `CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)` no puede
  ejecutarse dentro de un bloque de transacción. Sobre TimescaleDB 2.27.2
  eso **ya no es cierto**: se comprobó que funciona sin problema dentro de
  un `BEGIN`/`COMMIT` explícito, y la dirección `up` se aplica bien con la
  opción y sin ella.

  La opción sigue siendo necesaria por la dirección **`down`**.
  `000002_reading_continuous_aggregates.down.sql` quita las políticas de
  refresco y de columnstore y después tira las vistas. En modo
  fichero-completo todo eso es una sola transacción, así que las bajas de
  políticas nunca se confirman antes del `DROP`: el planificador de
  trabajos en segundo plano no puede verlas, un trabajo a medio ejecutar
  compite con el `DROP` y falla con `tuple concurrently deleted` o
  `tuple concurrently updated`. Medido: **2 de 3 reversiones fallaron sin
  la opción, 7 de 7 salieron bien con ella.** Con la opción cada sentencia
  hace autocommit, así que la baja de la política es visible para el
  planificador antes de tirar las vistas.

  Por lo ingenuo del separador, estos ficheros se mantienen a propósito
  «seguros para trocear»: sin funciones pl/pgsql y **sin `;` en medio de
  una línea de comentario** (un `;` al final del comentario sí vale).
- **`timescaledb-toolkit`** es un único `CREATE EXTENSION`, así que la
  opción da igual; los ejemplos de abajo la omiten.

Equivocarse con la opción en `core` no da un error claro: da un fichero
cortado por la mitad (`unterminated dollar-quoted string`). Equivocarse en
`timescaledb` da un fallo **intermitente** al revertir, que es peor —
funciona las veces suficientes para parecer correcto.

Se usa la cadena de conexión `postgres://` estándar (el controlador
lib/pq), que es lo que usan estos ejemplos. La base de datos corre en un
contenedor que escucha solo en `127.0.0.1` (docs/adr/0001), así que
`migrate` se invoca desde el anfitrión contra el puerto publicado.

## Núcleo

```sh
# sin x-multi-statement (fichero completo; la función pl/pgsql lo necesita)
DB_URL="postgres://user:pass@localhost:5432/fertloops?sslmode=disable"

migrate -source file://db/migrations/core -database "$DB_URL" up
```

## Capa de extensión TimescaleDB

Necesita la extensión `timescaledb` disponible en la instancia destino y
`core` ya aplicada. La versión 1 convierte `reading` en hipertabla con
compresión; la versión 2 añade encima agregados continuos horario y diario
(docs/adr/0009). En el `QUERIES.md` de ese directorio hay patrones de
consulta listos para relleno de huecos y primero/último.

La versión 1 además **elimina la clave ajena autorreferenciada** de
`reading` y amplía su clave primaria a `(id, observed_at)` — conviene leer
docs/adr/0009 **antes** de aplicarla, no después.

```sh
TSDB_URL="postgres://user:pass@localhost:5432/fertloops?sslmode=disable&x-multi-statement=true&x-migrations-table=schema_migrations_ext_timescaledb"

migrate -source file://db/migrations/extensions/timescaledb -database "$TSDB_URL" up
```

La retención se deja deliberadamente sin fijar, y a este volumen la
recomendación es no ponerla nunca: tirar las medidas crudas ahorra una
fracción de gigabyte al año y destruye el único registro sin corregir y a
resolución completa que existe. Conservar las crudas para siempre es
además lo que hace asequible el refresco sin límite de los agregados.
Ratificarlo es [#14](https://github.com/bisite/FertLoops/issues/14); ver
docs/adr/0009.

## Capa de extensión TimescaleDB Toolkit

Es una extensión distinta de la capa `timescaledb` de arriba
(docs/adr/0010): se puede aplicar una sin la otra. Necesita la extensión
`timescaledb_toolkit` instalada en la instancia destino (un paso de
instalación aparte del TimescaleDB base, incluso autoalojado) y `core` ya
aplicada. En el `QUERIES.md` de ese directorio están las recetas de media
ponderada en el tiempo, submuestreo y vivacidad de sensores.

```sh
TOOLKIT_URL="postgres://user:pass@localhost:5432/fertloops?sslmode=disable&x-migrations-table=schema_migrations_ext_timescaledb_toolkit"

migrate -source file://db/migrations/extensions/timescaledb-toolkit -database "$TOOLKIT_URL" up
```

Añadir una capa de mejora futura (PostGIS, por ejemplo) sigue el mismo
patrón: un directorio `db/migrations/extensions/<nombre>/` nuevo con su
`x-migrations-table=schema_migrations_ext_<nombre>`.

## Semilla del vocabulario

Necesita solo `core` aplicada. Siembra 4 contextos de sensor, 10 unidades
y los 17 `sensor_type` derivados del contrato UART congelado
(docs/trama-de-datos-riego.md, acordado en
[#2](https://github.com/bisite/FertLoops/issues/2)) según el modelo de
docs/adr/0011. Es idempotente —`ON CONFLICT DO NOTHING` en todo—, así que
reaplicarla no duplica nada.

Va sin `x-multi-statement`, para que la semilla entera sea una sola
transacción.

```sh
SEED_URL="postgres://user:pass@localhost:5432/fertloops?sslmode=disable&x-migrations-table=schema_migrations_seed"

migrate -source file://db/migrations/seed -database "$SEED_URL" up
```

El vocabulario es **cerrado a propósito**: la tubería de ingesta rechaza
una terna `(contexto, magnitud, unidad)` que no esté sembrada, en lugar de
crearla, y eso es lo que impide la deriva de unidades (docs/adr/0005).
Añadir una magnitud es por tanto una migración nueva, no una edición de
configuración.

**Revertir esta fuente falla si hay datos que dependan de ella**, porque
las claves ajenas hacia `sensor_type` son `ON DELETE RESTRICT`. Es
deliberado — borrar el vocabulario por debajo de las medidas que lo usan
sería peor. Ojo con el efecto secundario: **una reversión fallida deja la
versión en estado sucio**, así que hay que arreglar la dependencia y
después `migrate ... force 1` antes de reintentar.

## Enmendar en el sitio frente a añadir

Estas migraciones se pueden **enmendar en el sitio** —editar `000001` en
lugar de añadir un `000002`— exactamente mientras ningún despliegue las
haya aplicado. Es deliberado: deja una base limpia para quien arranque de
este esquema más adelante, en vez de un `000001` prístino seguido de un
rastro de correcciones que alteran y vuelven a alterar. golang-migrate
nunca reejecuta una versión ya aplicada, así que una edición en el sitio
solo alcanza a las bases de datos creadas después.

**En cuanto este esquema se aplique a un despliegue real, eso se termina:
a partir de ahí es estrictamente de solo añadido, para siempre.** Cuando
ocurra, anotarlo aquí con su fecha y el despliegue, porque después una
edición en el sitio desvía en silencio la base de datos desplegada
respecto a los ficheros de migración.

Todavía no hay nada desplegado, así que las correcciones sobre el esquema
heredado —la columna `sum_value`, la política de columnstore a 14 días, el
filtro de correcciones y la agrupación por días locales— se aplicaron
enmendando `000001` y `000002` directamente, y no como migraciones
posteriores.

## Crear una migración nueva

```sh
migrate create -ext sql -dir db/migrations/core -seq add_something
```

(cambiando `-dir` por el directorio de la extensión que toque, si es para
una de ellas). `-seq` mantiene las versiones como enteros secuenciales,
en coherencia con lo que ya hay, en lugar de pasar a marcas de tiempo.

## Otras órdenes útiles

```sh
migrate -source file://db/migrations/core -database "$DB_URL" down 1   # revertir un paso
migrate -source file://db/migrations/core -database "$DB_URL" version  # versión actual
migrate -source file://db/migrations/core -database "$DB_URL" force N  # tras arreglar un estado sucio
```

## Análisis estático del SQL

[sqruff](https://playground.quary.dev/) se ejecuta sobre `core`; las capas
de extensión quedan excluidas porque su DDL no lo parsea una gramática de
postgres a secas (ver `.sqruff` y `.sqruffignore` en la raíz del
repositorio). Se lanza desde la raíz. **Todavía no hay barrera en CI**,
eso es parte de [#16](https://github.com/bisite/FertLoops/issues/16):

```sh
uvx sqruff lint .    # necesita uv: https://docs.astral.sh/uv/getting-started/installation/
```

## Cómo se comprobó esto

Todo lo documentado arriba se ejercitó contra un contenedor desechable de
la imagen que este proyecto despliega de verdad
(`timescale/timescaledb-ha:pg18` — PostgreSQL 18.4, TimescaleDB 2.27.2,
TimescaleDB Toolkit 1.23.0), con golang-migrate 4.19.0. **No hay ninguna
prueba automática que lo vigile**: esto es una comprobación
puntual y no una barrera contra regresiones. Hay que repetirla a mano al
cambiar una migración. Montar CI es parte de
[#16](https://github.com/bisite/FertLoops/issues/16).

Lo confirmado:

- Las tres fuentes se aplican en orden y se revierten en orden inverso.
- `core` **falla** con `x-multi-statement=true`
  (`unterminated dollar-quoted string`), tal como se documenta.
- La reversión de `timescaledb` es inestable **sin** `x-multi-statement=true`
  (2 de 3 fallos) y fiable con ella (7 de 7).
- Revertir `000001_reading_hypertable` conserva todas las filas con su `id`
  original, restaura la clave ajena autorreferenciada, la clave primaria de
  una sola columna y los nombres de los cuatro índices, y avanza la
  secuencia de identidad para que la siguiente inserción no colisione.
- Cada restricción del núcleo rechaza lo que debe: medidas originales
  duplicadas, una segunda colocación activa del mismo dispositivo,
  `removed_at` anterior a `placed_at`, latitud fuera de rango, y borrar un
  sensor que tiene medidas (`ON DELETE RESTRICT` lanza SQLSTATE **23001**
  `restrict_violation`, no 23503 — conviene saberlo al escribir la capa de
  aplicación).
- Una corrección que comparte `sensor_id` y `observed_at` con la medida que
  corrige **sí** se acepta, tal como está diseñado.
- Los agregados **excluyen** las correcciones. Una corrección de valor 42,0
  llevaba el `max_value` diario a 42,0 antes de añadir el filtro
  `WHERE corrects_reading_id IS NULL`, y vuelve al 23,149 real después: es
  el doble conteo que ese filtro existe para arreglar (docs/adr/0009).
- `reading_daily` reproduce `avg_value`, `sum_value` y `max_value` de las
  filas crudas con diferencia nula, mientras que un `AVG(avg_value)`
  ingenuo sobre `reading_hourly` se desviaba 0,39 en un día parcial.
- Activar el columnstore **no** programa la compresión por sí solo: la
  hipertabla se quedaba con cero trabajos hasta llamar explícitamente a
  `add_columnstore_policy`. La política de 14 días está aplicada y
  verificada.
- **La deduplicación sobrevive a la compresión.** Un duplicado exacto se
  rechaza también dentro de un *chunk* ya comprimido, y
  `ON CONFLICT (sensor_id, observed_at) WHERE corrects_reading_id IS NULL
  DO NOTHING` infiere el índice parcial y lo salta en silencio, que es
  como escribirá la tubería de ingesta. Reinyectar un día de una serie en
  zona comprimida cuesta 28,7 ms frente a 20,8 ms sin comprimir, y el
  *chunk* **sigue comprimido** después: un vaciado de cola tras un corte
  largo no descomprime nada (docs/adr/0013).
- **`reading_daily` agrupa por días locales de `Europe/Madrid`, no por días
  UTC.** `time_bucket` tiene `timezone` con valor por defecto `UTC+0` e
  **ignora la zona horaria de la sesión** (a diferencia de `date_trunc`),
  así que sin el argumento explícito ninguna configuración del servidor lo
  corrige. Comprobado sobre el cambio de hora del 29 de marzo de 2026: con
  zona, los buckets empiezan a las 00:00 locales y el día sale con 1380
  minutos (23 h), cuadrando con las filas crudas con error 0 en los cinco
  días medidos; sin zona, ese día devolvía 1440. El coste: un bucket con
  zona es de ancho **variable**, y TimescaleDB rechaza apilar un agregado
  de ancho fijo encima de uno variable — un futuro semanal o mensual
  tendrá que llevar zona también. El sobrecoste medido es de 3 ms en un
  refresco completo de 200 días, con exclusión de *chunks* idéntica.
- La semilla del vocabulario es idempotente (reaplicarla deja 4 contextos,
  10 unidades y 17 tipos, sin duplicar), su reversión **falla** mientras un
  `sensor` referencie un tipo sembrado, y funciona en cuanto se quita la
  dependencia.
- Sobre el modelo definitivo de 17 series por Mesa de drenaje
  (docs/adr/0011) y un año de cadencia real: **8,91 M filas, 2171 MB sin
  comprimir → 42 MB comprimidos, una razón de 51,3×**, o 4,75 MB por millón
  de filas. Extrapolado a las 12 Mesas del piloto: ~107 M filas/año y
  ~508 MB/año. Con solo las 9 magnitudes medidas la razón era 26,5×, así
  que **añadir las series de errores y de eco de control mejora la
  compresión** en lugar de empeorarla: son casi constantes.
- **Estas migraciones exigen la edición Community y fallan sin ella.** En
  `timescale/timescaledb-ha:pg18`, `SHOW timescaledb.license` devuelve
  `timescale` y todo funciona. En la variante `-oss` devuelve `apache`, y
  la compresión, los agregados continuos y las políticas de retención
  fallan con «functionality not supported under the current apache
  license», mientras que `timescaledb_toolkit` no está ni presente. Las
  hipertablas sí siguen funcionando, así que una imagen `-oss` falla en el
  paso del columnstore de `000001`, no en `create_hypertable`. **Nunca usar
  una etiqueta `-oss`.**
