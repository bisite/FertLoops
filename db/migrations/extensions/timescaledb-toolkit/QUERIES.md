# Recetas de consulta: hiperfunciones de TimescaleDB Toolkit sobre `reading`

Patrones de consulta, no esquema: nada de lo que hay aquí necesita una
migración más allá de `000001_enable_toolkit`. Para el relleno de huecos y
el primero/último, que son del TimescaleDB base y no necesitan el Toolkit,
ver `../timescaledb/QUERIES.md`.

## Media ponderada en el tiempo

Un `AVG(value)` a secas da por supuesto que cada medida representa la
misma porción de tiempo, y eso deja de ser cierto en cuanto el intervalo
de muestreo varía. En FertLoops varía por dos motivos: el Gateway publica
al VPS cada 60 s aunque el Device muestree cada 10 s, y tras un corte del
enlace llegan lotes con huecos que no están repartidos de forma uniforme
(docs/adr/0005). `time_weight` tiene en cuenta cuánto tiempo estuvo
vigente cada valor.

```sql
SELECT
    sensor_id,
    time_bucket(INTERVAL '1 day', observed_at) AS bucket,
    average(time_weight('Linear', observed_at, value)) AS time_weighted_avg
FROM reading
WHERE sensor_id = $1
GROUP BY sensor_id, bucket;
```

`integral(...)` sobre el mismo agregado `time_weight(...)` da el área bajo
la curva en lugar de la media, que es lo que se quiere para un «cuánto X
en total a lo largo del día».

## Submuestreo para gráficas

Devolver todos los puntos crudos a un panel sobre un rango largo es
derrochar, y por encima de la resolución de la pantalla no aporta nada.
`lttb` (*Largest-Triangle-Three-Buckets*) elige un subconjunto
representativo que conserva la forma visual de la serie.

```sql
SELECT time, value
FROM unnest((
    SELECT lttb(observed_at, value, 800)  -- 800 ~= puntos objetivo en pantalla
    FROM reading
    WHERE sensor_id = $1
      AND observed_at >= $2 AND observed_at < $3
));
```

## Vivacidad de un sensor («¿se ha quedado callado?»)

Deliberadamente distinto de la adscripción de un Device a una Mesa de
drenaje (docs/adr/0011): la adscripción dice **dónde** está desplegado un
Device; esto dice si un **sensor** sigue reportando de verdad. El
razonamiento es el mismo que en docs/adr/0006 para el estado de un Device:
es una consulta sobre marcas de tiempo que ya existen, no una columna de
estado almacenada que pueda desincronizarse de la realidad.

```sql
SELECT
    sensor_id,
    live_at(heartbeat_agg(observed_at, INTERVAL '15 minutes'), now()) AS reporting
FROM reading
WHERE observed_at >= now() - INTERVAL '7 days'
GROUP BY sensor_id;
```

El `INTERVAL '15 minutes'` es la tolerancia de «sigue vivo»: se elige en
relación con cada cuánto se espera que reporte el sensor. Un sensor sin
ninguna medida dentro de esa ventana anterior a `now()` sale con
`reporting = false`.
