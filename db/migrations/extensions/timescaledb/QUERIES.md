# Recetas de consulta: hiperfunciones de TimescaleDB sobre `reading`

Son patrones de consulta, no esquema: nada de lo que hay aquí necesita
una migración. Usan solo funciones del propio TimescaleDB, sin extensión
adicional; para las que necesitan TimescaleDB Toolkit —medias ponderadas
en el tiempo, submuestreo, vivacidad de sensores— ver
`../timescaledb-toolkit/QUERIES.md`.

Todos los ejemplos dan por aplicadas `000001_reading_hypertable` y
`000002_reading_continuous_aggregates`.

## Rellenar huecos de medidas ausentes

Un sensor que se pierde una ventana de reporte aparece como fila
**ausente**, no como fila con `NULL`. `time_bucket_gapfill` fabrica los
buckets que faltan; `locf` e `interpolate` deciden qué valor poner en
ellos.

```sql
-- Arrastra el último valor conocido — apropiado para magnitudes que
-- cambian despacio (una consigna, por ejemplo); engañoso para las que
-- varían de verdad.
SELECT
    time_bucket_gapfill(INTERVAL '1 hour', observed_at) AS bucket,
    sensor_id,
    locf(AVG(value)) AS avg_value
FROM reading
WHERE observed_at >= now() - INTERVAL '1 day'
  AND observed_at < now()
  AND sensor_id = $1
GROUP BY bucket, sensor_id;

-- Interpolación lineal entre los valores reales de alrededor.
SELECT
    time_bucket_gapfill(INTERVAL '1 hour', observed_at) AS bucket,
    sensor_id,
    interpolate(AVG(value)) AS avg_value
FROM reading
WHERE observed_at >= now() - INTERVAL '1 day'
  AND observed_at < now()
  AND sensor_id = $1
GROUP BY bucket, sensor_id;
```

`time_bucket_gapfill` **exige un rango temporal explícito** en la consulta
(`WHERE observed_at >= ... AND observed_at < ...`): sin él no tiene forma
de saber qué buckets deberían existir.

## Primera y última medida de cada bucket

`reading_hourly` y `reading_daily` ya exponen `last_value` por bucket.
Para consultas puntuales que no pasen por los agregados continuos:

```sql
SELECT
    time_bucket(INTERVAL '1 hour', observed_at) AS bucket,
    sensor_id,
    first(value, observed_at) AS first_value,
    last(value, observed_at) AS last_value
FROM reading
WHERE sensor_id = $1
GROUP BY bucket, sensor_id;
```

## Aviso: estas consultas sí ven las correcciones

Ojo, porque aquí el comportamiento es **el contrario** al de los agregados
continuos. Los agregados filtran `WHERE corrects_reading_id IS NULL` y
resumen solo originales (docs/adr/0009), pero **ninguna de las consultas
de arriba lo hace**: agregan todas las filas que existan para ese sensor y
ese rango, incluida una medida original y la corrección que la supera,
contando el instante dos veces.

Si se quiere el mismo criterio que los agregados, hay que añadir el filtro
a mano. Consultar `reading` sin filtrar es lo correcto solo cuando se
quiere ver el histórico completo, correcciones incluidas.
