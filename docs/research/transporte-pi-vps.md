# Transporte de datos entre la Raspberry Pi y el VPS

Informe de investigación para el ticket [#4](https://github.com/bisite/FertLoops/issues/4) del mapa de wayfinding ([#1](https://github.com/bisite/FertLoops/issues/1)). Fuentes consultadas el 30 de julio de 2026. Todas las afirmaciones citadas provienen de documentación primaria (documentación oficial, páginas de manual y código fuente); cuando algo es razonamiento propio y no una afirmación documentada, se dice explícitamente.

## Recomendación

**MQTT con Mosquitto: un broker local persistente en la Raspberry Pi con un `bridge` hacia el broker del VPS.** Es la única de las opciones estudiadas que ofrece store-and-forward como *configuración* en lugar de como código propio, y trae de regalo una señal de vida del enlace que el ticket de alertas ([#15](https://github.com/bisite/FertLoops/issues/15)) va a necesitar.

Con una condición que no es negociable: **hay que sobrescribir explícitamente cinco valores por defecto de Mosquitto**. Tal y como viene de fábrica, un corte de enlace en este proyecto perdería datos en silencio. El ADR que salga de este ticket tiene que incluir esa configuración, no solo el nombre del producto.

## Contexto y qué tiene que resolver el transporte

La VPN entre la Pi y el VPS ya existe y la mantiene el equipo de infraestructura de USAL, así que **el transporte no tiene que resolver el cifrado de red ni el NAT**. Lo que tiene que resolver es:

1. **Semántica de entrega:** qué se pierde y qué no cuando el enlace cae durante horas o días.
2. **Quién custodia los datos mientras el enlace está caído**, aprovechando el SSD de 1 TB de la Pi.
3. **El camino de vuelta**: los comandos de control hacia el ESP32 y la confirmación de que se aplicaron.
4. **Cuánto software propio obliga a escribir**, sabiendo que lo mantendrán estudiantes rotando.

### Una aclaración previa que cambia el marco de la decisión

En **todas** las opciones hay que escribir un lector propio en la Pi. El ESP32 habla un protocolo de petición/respuesta por línea: la Pi envía `read\r\n` y parsea un JSON de respuesta (ver `docs/trama-de-datos-riego.md`). No existe ninguna herramienta turn-key que haga eso:

- Telegraf **no tiene plugin de puerto serie**. Hay peticiones de esa funcionalidad abiertas desde 2020 y 2021 sin resolver ([issue #7218](https://github.com/influxdata/telegraf/issues/7218), [issue #10349](https://github.com/influxdata/telegraf/issues/10349)). Los plugins que sí hablan por serie lo hacen con protocolos concretos: el [plugin `modbus`](https://github.com/influxdata/telegraf/blob/master/plugins/inputs/modbus/README.md) admite línea serie en RTU o ASCII, y el [plugin `mavlink`](https://docs.influxdata.com/telegraf/v1/input-plugins/mavlink/) admite rutas del tipo `serial:///dev/ttyACM0:57600`. Ninguno sirve para un protocolo JSON propio por línea.

Por lo tanto **la decisión no es «cuánto código escribimos», porque el lector hay que escribirlo en cualquier caso: la decisión es a quién le entrega ese lector lo que lee**. Eso reduce la pregunta a qué componente se encarga de la durabilidad y del reenvío, que es precisamente la parte que no queremos escribir nosotros.

## Opción A — MQTT con Mosquitto (broker local + bridge)

### Cómo funciona

Un `bridge` de Mosquitto es una conexión que el broker local abre contra un broker remoto, comportándose como un cliente de éste. Se declara con `connection <nombre>`, `address <host[:puerto]>` y una o más líneas `topic`:

> `topic` _pattern_ [out|in|both] [qos-level] [local-prefix] [remote-prefix]: «Define a topic pattern to be shared between the two brokers […] The second parameter defines the direction […] If this parameter is not defined, the default of _out_ is used. The QoS level […] defaults to 0.»

El remapeo de prefijos está documentado en las dos direcciones, lo que permite, por ejemplo, publicar en local bajo `fertloops/...` y que llegue al VPS bajo `sitios/ciale/fertloops/...` sin tocar el código del publicador.

Dos defaults que sí van a nuestro favor:

- `cleansession` para el bridge: «Setting to _false_ (the default), means that all subscriptions on the remote broker are kept in case of the network connection dropping.»
- `restart_timeout`: reconexión con backoff con jitter, base 5 s y tope 30 s por defecto.

### Los cinco defaults que hay que cambiar

Aquí está el hallazgo importante de este informe. Todos los valores por defecto que gobiernan la durabilidad están puestos en el lado inseguro:

| Opción | Default documentado | Consecuencia si se deja así |
| --- | --- | --- |
| `persistence` | **`false`** | El broker no escribe nada a disco: un reinicio pierde todo lo encolado. |
| `topic ... [qos-level]` | **`0`** | El bridge publica en QoS 0, es decir sin garantía de entrega. |
| `queue_qos0_messages` | **`false`** | «Set to _true_ to queue messages with QoS 0 when a persistent client is disconnected». Con QoS 0 y este default, los mensajes de un cliente desconectado **no se encolan en absoluto**. Combinado con el default anterior: un corte de enlace descarta los datos en silencio. |
| `max_queued_messages` | **`1000`** por cliente | «The maximum number of QoS 1 or 2 messages to hold in the queue (per client) above those messages that are currently in flight. Defaults to 1000. Set to 0 for no maximum.» A una muestra por minuto son 1440 mensajes al día: **el default aguanta unas 17 horas de corte** y a partir de ahí descarta. `0` = sin límite. |
| `autosave_interval` | **`1800`** s (30 min) | Es el intervalo con el que la base de datos en memoria se vuelca a disco. Un corte de luz en el invernadero puede perder hasta media hora de lo encolado. Con `0` solo se guarda al salir o al recibir `SIGUSR1`, lo cual es *peor* para este caso; lo que hace falta es un valor bajo, o `autosave_on_changes true` para contar cambios en lugar de segundos. |

No existe una opción de cola específica para bridges: se ha comprobado que **`bridge_max_queued_messages` no aparece en `mosquitto.conf(5)`** y que la única opción de tamaño de cola es la general `max_queued_messages`, aplicada por cliente —y el bridge es un cliente más del broker local.

Matiz honesto sobre el SSD de 1 TB: la persistencia de Mosquitto es una **base de datos en memoria que se vuelca periódicamente** a `mosquitto.db` (`persistence_location`, `persistence_file`), no un spool en disco al estilo de una cola transaccional. En este proyecto eso no es un problema de volumen —a una muestra por minuto y unos cientos de bytes por trama, un mes de acumulación son decenas de MB, holgadamente en RAM— pero conviene no describirlo como «usar el SSD de 1 TB para el buffer», porque no es lo que hace.

### Regalo útil: detección de enlace caído

`notifications` está en `true` por defecto y publica un mensaje **retenido** con `1` o `0` en `$SYS/broker/connection/<remote_clientid>/state` según si la conexión del bridge está activa o ha fallado. Es exactamente la primitiva que el ticket de alertas ([#15](https://github.com/bisite/FertLoops/issues/15)) necesita para detectar un gateway callado, sin escribir nada.

### Camino de vuelta para los comandos

Se resuelve con una línea `topic ... in` en el bridge: el VPS publica el setpoint, la Pi lo recibe y su lector lo traduce a la trama de control del ESP32. La respuesta del ESP32 (`Ok\r\n` o `Invalid command "<línea>"\r\n`) se publica de vuelta como telemetría. La idempotencia —que un comando reenviado no provoque un riego duplicado— **no la resuelve MQTT**: QoS 1 es «al menos una vez», así que el identificador de comando y la deduplicación son diseño nuestro, y corresponden al ticket del camino de control ([#10](https://github.com/bisite/FertLoops/issues/10)).

### Coste en software propio

El lector del serie (inevitable) más ficheros de configuración. En el VPS, la escritura a la base de datos puede hacerla un consumidor MQTT existente —por ejemplo el plugin `mqtt_consumer` de Telegraf— sin código propio, si la base de datos elegida en [#3](https://github.com/bisite/FertLoops/issues/3) está soportada.

## Opción B — NATS (JetStream, leaf nodes y gateway MQTT)

NATS es el sistema técnicamente más capaz de los estudiados, y por eso mismo el que peor encaja aquí.

- **El núcleo de NATS no persiste nada.** «Core NATS delivers messages only to subscribers connected at the moment of publication — at most once, never replayed.» La durabilidad la da JetStream: «JetStream adds a persistence layer on top, giving you at-least-once delivery — messages survive restarts and can be replayed», con almacenamiento en memoria o en fichero.
- **Un leaf node es una conexión de salida** desde el borde hacia el hub, con aislamiento de espacio de nombres por cuenta: «A **leaf node** is a NATS server that opens an _outbound_ connection to a remote NATS system and bridges subject interest across it», y «The outbound direction is what makes this work».
- **El hallazgo decisivo:** la documentación de leaf nodes **no dice qué ocurre con los mensajes publicados en el borde mientras el enlace con el hub está caído**. No hay ninguna afirmación sobre buffering, encolado ni store-and-forward en ese escenario. El camino documentado para conseguir durabilidad en el borde es ejecutar **JetStream en el propio leaf node con su propio dominio** y luego espejar o *sourcear* streams a través del enlace. Es decir: no es una casilla de configuración, es una topología con dominios y streams espejados.
- **El gateway MQTT no evita JetStream, lo exige.** El servidor «currently supports most of MQTT 3.1.1» —por tanto **no MQTT 5.0**— y usa JetStream para persistir el estado de sesión, los mensajes retenidos, los mensajes QoS 1 y 2 entrantes y los PUBREL salientes, en streams internos (`$MQTT_msgs`, `$MQTT_qos2in`, `$MQTT_out`, `$MQTT_rmsgs`). No poder inicializar esos streams «would prevent the client from connecting». Entre los problemas conocidos que documenta el propio servidor: «JetStream QoS redelivery happens out of (original) order», y entregas en vuelo que no se completan tras un UNSUB o una reconexión.
- Ni la documentación de JetStream ni la de leaf nodes dan cifras mínimas de RAM o disco.

**Valoración (razonamiento propio, no documentado):** para obtener lo que Mosquitto da con cinco líneas de configuración, NATS pide JetStream en los dos extremos, un dominio en el borde y streams espejados. Sobre el criterio de «explicable en una tarde» y «difícil de romper en silencio» con estudiantes rotando, eso es un coste alto sin beneficio proporcional a este tamaño de proyecto. Reevaluar si algún día hay muchos emplazamientos.

## Opción C — HTTP contra una API en el VPS, con cola local

No requiere broker y usa un protocolo que cualquiera entiende, pero **la cola local es código propio**: hay que escribir el spool en disco, la política de reintentos, el backoff, el borrado tras confirmación y la recuperación tras reinicio. Eso es exactamente la parte que la opción A obtiene como configuración, y además es la parte donde los errores no se ven hasta que se necesita el dato. Añade también una API propia en el VPS antes de saber si vamos a tener una (decisión pendiente en [#12](https://github.com/bisite/FertLoops/issues/12)). *(Valoración propia; no hay documentación que citar aquí porque la opción consiste precisamente en construirla.)*

## Opción D — Escritura directa a la base de datos del VPS

La más simple mientras el enlace funciona y la que menos piezas tiene. Su problema es el mismo que la opción C, agravado: sin buffer local, un corte pierde datos; y con buffer local, el buffer es código propio. Acopla además el lector de la Pi al esquema de la base de datos, de modo que un cambio de esquema obliga a desplegar en el borde. *(Valoración propia.)*

## Opción E — Agente de recolección (Telegraf)

Descartada como pieza *de ingesta desde el serie*, por dos motivos documentados:

1. **No puede leer el puerto serie** con un protocolo propio de petición/respuesta (ver la aclaración previa). Obligaría a `inputs.exec`/`execd` envolviendo un script nuestro, con lo que desaparece la ventaja de no escribir código.
2. **Su buffer descarta datos en silencio.** `metric_buffer_limit` es «Maximum number of unwritten metrics per output […] **Oldest metrics are overwritten in favor of new ones when the buffer fills up**». Existe `buffer_strategy` con modo `disk`, pero la propia documentación lo describe como «**an experimental disk-backed buffer**». Para datos de ensayo que no se pueden volver a tomar, un buffer que sobrescribe lo más antiguo es el comportamiento contrario al que se necesita.

Sigue siendo **una buena opción en el lado del VPS**: `mqtt_consumer` leyendo del broker y escribiendo a la base de datos, donde un hueco es recuperable porque el broker retiene.

## Comparativa

| | Store-and-forward | Código propio añadido | Piezas nuevas | Camino de vuelta |
| --- | --- | --- | --- | --- |
| **A. Mosquitto + bridge** | Sí, por configuración (con los defaults corregidos) | Ninguno más allá del lector | 1 broker en cada extremo | `topic ... in` |
| **B. NATS** | Sí, con JetStream en el borde + dominio + mirror | Ninguno, pero mucha topología | 2 servidores + streams | Nativo |
| **C. HTTP + cola** | Sí, escribiéndola nosotros | Spool, reintentos, backoff, recuperación | API propia | Polling o webhook |
| **D. BD directa** | No | Buffer propio si se quiere | Ninguna | No natural |
| **E. Telegraf desde el serie** | No (buffer sobrescribe; disco experimental) | Script para el serie de todos modos | 1 agente | No |

## Lo que no se ha podido determinar con fuentes primarias

- **Valores por defecto numéricos de Telegraf** (`metric_buffer_limit`, `metric_batch_size`, `flush_interval`): no aparecen en la página de configuración ni en `docs/CONFIGURATION.md` del repositorio; solo se documenta la descripción del comportamiento. El fichero `etc/telegraf.conf` del repositorio devolvió 404 en la rama `master`.
- **Comportamiento exacto de un leaf node de NATS ante un enlace caído** sin JetStream en el borde: no está documentado, ni para afirmarlo ni para negarlo. Si NATS llegara a considerarse en serio, esto habría que medirlo en un banco de pruebas, no deducirlo.
- **Cifras mínimas de RAM y disco** de JetStream: no publicadas.
- **Comportamiento de Mosquitto con `max_queued_messages 0`** ante un corte de muy larga duración: la documentación dice «no maximum» pero no describe qué ocurre al agotarse la memoria. A la cadencia de este proyecto no debería llegar a ser un problema, pero no está documentado y conviene fijar un límite alto explícito en lugar de `0`.
- **Interacción entre `max_queued_messages` y la cola del bridge concretamente**: se deduce de que el bridge es un cliente del broker local y de que el límite es por cliente, pero no hay una frase en la documentación que lo afirme para el caso del bridge. Merece una prueba empírica antes de fijar el valor en producción.

## Fuentes

- Mosquitto, página de manual `mosquitto.conf(5)`: <https://mosquitto.org/man/mosquitto-conf-5.html>
- NATS, JetStream: <https://docs.nats.io/nats-concepts/jetstream>
- NATS, Leaf Nodes: <https://docs.nats.io/running-a-nats-service/configuration/leafnodes>
- NATS, configuración MQTT: <https://docs.nats.io/running-a-nats-service/configuration/mqtt>
- `nats-server`, `server/README-MQTT.md` (código fuente): <https://github.com/nats-io/nats-server/blob/main/server/README-MQTT.md>
- Telegraf, configuración: <https://docs.influxdata.com/telegraf/v1/configuration/>
- Telegraf, `docs/CONFIGURATION.md`: <https://github.com/influxdata/telegraf/blob/master/docs/CONFIGURATION.md>
- Telegraf, plugin `modbus`: <https://github.com/influxdata/telegraf/blob/master/plugins/inputs/modbus/README.md>
- Telegraf, plugin `mavlink`: <https://docs.influxdata.com/telegraf/v1/input-plugins/mavlink/>
- Telegraf, peticiones de plugin de puerto serie: <https://github.com/influxdata/telegraf/issues/7218> y <https://github.com/influxdata/telegraf/issues/10349>
