# Almacén de series temporales para el VPS

Informe de investigación para el ticket [#3](https://github.com/bisite/FertLoops/issues/3) del mapa de wayfinding ([#1](https://github.com/bisite/FertLoops/issues/1)). Fuentes consultadas el 30 de julio de 2026. Se distingue explícitamente entre lo que afirma la documentación primaria y lo que es razonamiento o cálculo propio.

## Recomendación

**PostgreSQL, sin extensiones de series temporales, con TimescaleDB como camino de mejora documentado y no tomado de entrada.**

El razonamiento en una frase: a la cadencia de este proyecto los datos son pequeños, y las dos cosas que sí necesitamos de verdad —**cruzar medidas con eventos discretos de riego y fertilización**, y **que un estudiante de TFG pueda consultarlo**— las da SQL. Las bases de datos especializadas en series temporales compran capacidades que aquí no hacen falta, y las pagan con un repositorio de terceros, una licencia que hay que leer con cuidado, o un modelo de datos que no admite las consultas que el proyecto necesita.

## Estimación de volumen (cálculo propio, no dato documentado)

Sin esta cifra la comparación no se puede hacer, así que la calculo con supuestos explícitos:

- Series por zona a partir de `docs/trama-de-datos-riego.md`: suelo (T, H, C), aire (T, H), pH, CE, radiación solar y volumen acumulado ≈ **9 magnitudes**, más 5 códigos de error y estado ≈ **14 series**.
- Cadencia: **1 muestra por minuto** (el `Sample_per_minute: 6` del ejemplo sugiere que podría ser más; queda pendiente de [#2](https://github.com/bisite/FertLoops/issues/2)).
- Zonas: **4 mesas de drenaje** (pendiente de confirmar en [#2](https://github.com/bisite/FertLoops/issues/2), y puede que haya un ESP32 por mesa o uno por módulo).

Resultado: 14 × 1440 = **20.160 filas por zona y día**; con 4 zonas, **~80.000 filas al día** ≈ **29 millones de filas al año**. En un esquema estrecho (instante, serie, valor) son del orden de **1,5–3 GB al año** contando índices.

Eso es un volumen que PostgreSQL maneja sin esfuerzo en 4 GB de RAM. **Ninguna de las funcionalidades avanzadas de las bases especializadas —compresión, agregados continuos, downsampling automático— es necesaria a esta escala.** Es el hecho que más peso tiene en la recomendación.

## PostgreSQL a secas

- **Empaquetado:** está en los repositorios de Ubuntu, no en un repositorio de terceros. Es el único candidato del que esto es cierto sin matices.
- **Consultas:** SQL estándar, con lo que el cruce de series con eventos discretos (riegos, dosificaciones, cambios de modo de autoridad, comandos auditados en [#10](https://github.com/bisite/FertLoops/issues/10)) es un `JOIN` normal. Ventanas móviles con funciones de ventana. Para el análisis científico posterior, cualquier herramienta —R, Python, QGIS, una hoja de cálculo— habla SQL.
- **Retención y agregación:** con particionado declarativo por rango sobre el instante, la retención es `DROP TABLE` de la partición antigua, que es instantáneo y no genera hinchazón. Los agregados se resuelven con vistas materializadas refrescadas por un temporizador de systemd (o `pg_cron`). **Esto es trabajo manual que asumimos conscientemente**: es exactamente lo que TimescaleDB automatiza.
- **Copias de seguridad:** `pg_dump`/`pg_restore` y `pg_basebackup` son herramientas de primera línea, documentadas y conocidas. El procedimiento de restauración verificable se detalla en el ticket de operación ([#6](https://github.com/bisite/FertLoops/issues/6)).
- **Soporte en Grafana:** hay origen de datos PostgreSQL de serie (se confirma en el informe de [#5](https://github.com/bisite/FertLoops/issues/5)).
- **Curva de aprendizaje:** la más baja de las cuatro para alguien que llega a un TFG, porque SQL se enseña en el grado.

## TimescaleDB

Extensión de PostgreSQL, así que conserva todo lo anterior y añade hipertablas, agregados continuos, compresión y políticas de retención. Es el candidato técnicamente más adecuado *si* el volumen lo exigiera. Tres cosas hay que saber antes de elegirlo:

### 1. Está partido en dos ediciones y lo que queremos está en la de pago-por-licencia

El fichero `LICENSE` del proyecto lo dice sin ambigüedad: «Outside of the 'tsl' directory, source code in a given file is licensed under the Apache License Version 2.0», y «Within the 'tsl' folder, source code in a given file is licensed under the Timescale License». La comparativa de ediciones sitúa en la **Community Edition** (licencia TSL, no Apache) prácticamente todo lo que motivaría instalarlo:

| Funcionalidad | Edición |
| --- | --- |
| Hipertablas (básico) | Apache 2 |
| **Agregados continuos** (crear, refrescar, políticas) | **Community (TSL)** |
| **Compresión / columnstore** | **Community (TSL)** |
| **Políticas de retención** (`add_retention_policy`) | **Community (TSL)** |
| **Planificación de trabajos** (`add_job`, `alter_job`) | **Community (TSL)** |
| `reorder_chunk`, `move_chunk`, `SkipScan` | Community (TSL) |

Es decir: **con la edición Apache 2 te quedas con hipertablas y poco más**, y las políticas automáticas —el motivo real para instalarlo— requieren TSL.

### 2. La TSL permite autoalojarlo gratis, pero prohíbe venderlo como servicio

Textualmente: se puede «install [it] in your own on-premises or cloud infrastructure and run it for free», y es «completely free if you manage your own service». Pero «You cannot sell [Community Edition] as a service, even if you are the main contributor», ni «make modifications to the [Community Edition] source code and offer it as a service».

Para el piloto del CIALE esto es irrelevante: lo autoalojamos y es gratis. **Pero la sección de comercialización de la propuesta plantea explícitamente un modelo SaaS**, y ese modelo chocaría con la TSL. El SaaS está fuera del alcance de este mapa, así que no cambia la decisión de hoy; sí conviene que quede escrito para que nadie se lo encuentre por sorpresa dentro de dos años.

### 3. Hay paquete en Ubuntu, pero no sabemos con qué edición se compila

El archivo de paquetes de Ubuntu sí contiene `postgresql-17-timescaledb` (questing 25.10) y `postgresql-18-timescaledb` (resolute 26.04 LTS), ambos en la sección **universe**. Lo que **no** he podido determinar es si esos paquetes se compilan con `APACHE_ONLY=1`, lo que dejaría fuera compresión, agregados continuos y políticas de retención. Se sabe que otros empaquetadores lo hacen así (el Dockerfile de Spilo usa `APACHE_ONLY=1`), y hay incidencias abiertas en el propio proyecto sobre confusión entre ediciones según cómo se haya instalado. **Comprobación pendiente y trivial:** instalar y ejecutar `SHOW timescaledb.license;`, o revisar `debian/rules` del paquete fuente.

## InfluxDB 3 Core — descartado

Motivo: **no puede consultar el rango temporal que este proyecto necesita.**

La documentación de Core lista como funcionalidades exclusivas de Enterprise «Historical query capability and single series indexing», junto con alta disponibilidad y réplicas de lectura. El detalle está en la configuración: con el valor por defecto de 432 ficheros por consulta y una `gen1-duration` de 10 minutos, **una consulta alcanza del orden de 72 horas de datos**. La restricción de *escritura* histórica se levantó, pero el rango que una sola consulta puede cubrir sigue estando limitado en horas por razones de implementación; Enterprise lo resuelve reordenando datos por serie y escribiendo un índice aparte.

Un proyecto cuyo objetivo declarado (O6) es comparar campañas de cultivo completas no puede construirse sobre un motor cuyas consultas abarcan tres días. Existe una edición Enterprise gratuita «for at-home use», pero un invernadero universitario de investigación no es «at-home use», y apoyar la arquitectura en una excepción de licencia que no nos aplica claramente es mala idea. Soporta SQL e InfluxQL, lo cual es un punto a favor que no llega a compensar lo anterior.

*(Nota de trazabilidad: el detalle del límite de 72 horas y su evolución provienen de un artículo del blog de InfluxData y de hilos de su foro —**fuentes secundarias**—, corroborados con la página de opciones de configuración y la de documentación de Core, que sí son primarias.)*

## VictoriaMetrics — descartado

Motivo: **el modelo de datos no admite las consultas que el proyecto necesita.**

Implementa **MetricsQL**, «a PromQL-like query language», y **no soporta SQL**. Su modelo es métricas con etiquetas y muestras (valor, instante), no filas relacionales, de modo que **no se pueden hacer joins arbitrarios entre series temporales y tablas de eventos discretos**. La retención se configura con `-retentionPeriod`, con un valor por defecto de **1 mes (31 días)** y un mínimo de 24 h. Es Apache 2.0 en su versión abierta, tiene `vmbackup`/`vmrestore` basados en instantáneas, y es muy eficiente en RAM (afirma «10x less RAM than InfluxDB»).

Es una herramienta excelente para lo que está pensada —monitorización de infraestructura— y puede ser un candidato razonable para vigilar la *salud del sistema* en [#15](https://github.com/bisite/FertLoops/issues/15). Pero el dato de este proyecto es un **conjunto de datos científico** que hay que cruzar, anotar y exportar, no una métrica operativa.

## Comparativa

| | PostgreSQL | TimescaleDB | InfluxDB 3 Core | VictoriaMetrics |
| --- | --- | --- | --- | --- |
| Consultas SQL | Sí | Sí | Sí (+ InfluxQL) | **No** (MetricsQL) |
| Join series ↔ eventos | Sí | Sí | Limitado por el rango | **No** |
| Rango de consulta | Sin límite | Sin límite | **~72 h por consulta** | Sin límite |
| En repositorios de Ubuntu | **Sí (main)** | Sí (universe, edición sin verificar) | No (repo propio) | No (releases propias) |
| Retención automática | Manual (particiones) | Sí, pero **TSL** | Sí | Sí |
| Agregados automáticos | Manual (matviews) | Sí, pero **TSL** | Sí | Sí |
| Licencia | PostgreSQL (permisiva) | Apache 2 + **TSL** por partes | MIT/Apache 2 + Enterprise | Apache 2 + Enterprise |
| Curva para un TFG | La más baja | Baja (es Postgres) | Media | Media-alta |

## Consecuencias para otros tickets

- **[#4](https://github.com/bisite/FertLoops/issues/4) (transporte):** compatible. La recomendación de allí (Mosquitto con bridge) escribe a la base de datos mediante un consumidor MQTT en el VPS; con PostgreSQL hay consumidores existentes, sin código propio.
- **[#7](https://github.com/bisite/FertLoops/issues/7) (modelo de medidas):** elegir SQL deja abierta —y hace relevante— la decisión entre una fila por trama y filas estrechas por serie. Este informe supone el esquema estrecho para estimar volumen, pero **no prejuzga esa decisión**.
- **[#14](https://github.com/bisite/FertLoops/issues/14) (retención):** si se elige PostgreSQL a secas, la política de retención es diseño de particiones, y hay que decidirla explícitamente en lugar de heredarla del producto.

## Lo que no se ha podido determinar con fuentes primarias

- **Con qué edición se compilan los paquetes `postgresql-*-timescaledb` de Ubuntu universe.** Es la incógnita más relevante de este informe si se considera TimescaleDB. Comprobable en cinco minutos con `SHOW timescaledb.license;`.
- **Qué versión de Ubuntu corre el VPS**, que determina la versión de PostgreSQL disponible en repositorios (17 en 25.10, 18 en 26.04 LTS). Es un dato del equipo de infraestructura de USAL, no una decisión nuestra, pero conviene confirmarlo.
- **Cifras oficiales de huella de memoria** de ninguno de los cuatro candidatos: ninguno publica requisitos mínimos concretos y verificables para una carga como esta. Las afirmaciones comparativas de VictoriaMetrics («10x less RAM than InfluxDB») son de su propia documentación y no están contrastadas por un tercero.
- **El límite exacto de rango de consulta de InfluxDB 3 Core** en función de `gen1-duration` y del número de ficheros por consulta: la relación está documentada, pero el valor efectivo depende de cómo se ingirieron los datos, y no hay una cifra única publicada.
- **La cadencia real de muestreo y el número de zonas**, que gobiernan la estimación de volumen. Dependen de [#2](https://github.com/bisite/FertLoops/issues/2). Si la cadencia real fuese mucho mayor (por ejemplo 6 muestras por minuto en varias zonas), habría que rehacer el cálculo, aunque haría falta más de un orden de magnitud para que PostgreSQL a secas dejase de ser suficiente.

## Fuentes

- PostgreSQL, documentación oficial (particionado, vistas materializadas, copias de seguridad): <https://www.postgresql.org/docs/current/>
- TimescaleDB, fichero `LICENSE`: <https://github.com/timescale/timescaledb/blob/main/LICENSE>
- TimescaleDB, comparativa de ediciones: <https://github.com/timescale/docs/blob/latest/about/timescaledb-editions.md> y <https://docs.timescale.com/about/latest/timescaledb-editions/>
- Ubuntu, archivo de paquetes (búsqueda `timescaledb`): <https://packages.ubuntu.com/search?keywords=timescaledb&searchon=names&suite=all&section=all>
- InfluxDB 3 Core, documentación: <https://docs.influxdata.com/influxdb3/core/>
- InfluxDB 3 Core, opciones de configuración: <https://docs.influxdata.com/influxdb3/core/reference/config-options/>
- InfluxData, anuncio sobre la limitación de 72 horas (**fuente secundaria**): <https://www.influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27/>
- VictoriaMetrics, single-server: <https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/>
- Spilo, uso de `APACHE_ONLY=1` como precedente de empaquetado (**fuente secundaria**): <https://github.com/zalando/spilo/issues/403>
