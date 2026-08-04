# Transporte de datos entre la Raspberry Pi y el VPS

Informe de investigación para el ticket [#4](https://github.com/bisite/FertLoops/issues/4) del mapa de wayfinding ([#1](https://github.com/bisite/FertLoops/issues/1)). Fuentes consultadas el 30 de julio de 2026; opciones F y G (NATS nativo y NanoMQ) añadidas el 4 de agosto de 2026 con fuentes consultadas en esa fecha. Todas las afirmaciones citadas provienen de documentación primaria (documentación oficial, páginas de manual y código fuente); cuando algo es razonamiento propio y no una afirmación documentada, se dice explícitamente.

## Recomendación

**MQTT con Mosquitto: un broker local persistente en la Raspberry Pi con un `bridge` hacia el broker del VPS.** Es la única de las opciones estudiadas que ofrece store-and-forward como *configuración* en lugar de como código propio, y trae de regalo una señal de vida del enlace que el ticket de alertas ([#15](https://github.com/bisite/FertLoops/issues/15)) va a necesitar.

Con una condición que no es negociable: **hay que sobrescribir explícitamente cinco valores por defecto de Mosquitto**. Tal y como viene de fábrica, un corte de enlace en este proyecto perdería datos en silencio. El ADR que salga de este ticket tiene que incluir esa configuración, no solo el nombre del producto.

### Actualización: NATS nativo y NanoMQ no cambian la recomendación

Esta revisión añade dos opciones más, estudiadas contra fuentes primarias adicionales: **NATS de punta a punta sin ninguna pasarela MQTT** (opción F) y **MQTT con NanoMQ** como implementación alternativa a Mosquitto (opción G). Ninguna de las dos mueve la recomendación.

NATS nativo no resuelve lo que la variante con pasarela MQTT ya dejaba sin resolver: la documentación sigue sin decir qué pasa con lo publicado en el borde mientras el enlace con el hub está caído, y el único camino documentado hacia la durabilidad —JetStream en el leaf node con dominio propio y streams espejados hacia el hub— es la misma topología pesada de antes, ahora con más detalle de lo que exige configurar. El problema nunca estuvo en la pasarela MQTT: está en JetStream en el borde en general.

NanoMQ sí es una alternativa MQTT seria y documentadamente más liviana que Mosquitto en CPU y memoria. Pero su persistencia de bridge (basada en SQLite) **no viene compilada en el paquete ni en la imagen que se instalan por defecto** (`apt-get install nanomq`, `docker pull emqx/nanomq:latest`): hace falta elegir explícitamente el paquete `nanomq-sqlite`/`nanomq-full` o la imagen `-slim`/`-full`. Es un fallo silencioso de la misma familia que los cinco defaults de Mosquitto, pero un peldaño más arriba —en la elección del paquete, no en un fichero de configuración— y sin una unidad `systemd` publicada para arrancarlo. Se mantiene la recomendación de Mosquitto; NanoMQ queda anotado como alternativa razonable si algún día el rendimiento de Mosquitto fuera un problema real, cosa que a esta cadencia de datos no ocurre.

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

## Opción F — NATS nativo (sin pasarela MQTT)

Esta opción es distinta de la B: aquí no hay MQTT en ningún punto. La Pi ejecuta un `nats-server` como **leaf node** que abre una conexión saliente hacia un `nats-server` completo en el VPS (el hub); los clientes en ambos extremos hablan el protocolo NATS nativo. Se investiga porque el informe original dejó sin resolver, explícitamente, qué le pasa a un leaf node durante un corte de enlace.

### Qué le pasa a lo publicado en el borde durante un corte

Hay que separar dos capas, porque la documentación primaria las trata por separado y mezclarlas lleva a conclusiones erróneas:

- **La conexión del cliente de aplicación contra el `nats-server` local de la Pi.** Aquí sí hay documentación explícita: «A publish call during the gap doesn't fail; the client writes it to a **reconnect buffer**, an outbound queue that keeps your publishes during the outage and flushes them, in order, once the link returns.» Ese búfer está acotado —«bounded (8 MB by default)»— y si se llena, «a publish that would overflow it returns an error rather than growing without limit». Es memoria de proceso, no disco: «it lives in your client's memory. It isn't written anywhere durable, and if the process itself dies, everything in the buffer dies with it.» Esto protege al publicador de una caída *entre la aplicación y su servidor local*, no del enlace leaf-hub.
- **La conexión saliente del leaf node hacia el hub.** Aquí la documentación de leaf nodes sigue sin decir nada, ni para afirmar ni para negar que haya buffering. Lo único documentado sobre el comportamiento general de NATS sin JetStream es el modelo de «interés»: «The publish call returns immediately. It doesn't wait for a subscriber (...) A publish with no interest is a silent no-op: no error, no stored backlog.» Aplicado al caso del leaf: si el hub no está alcanzable, cualquier interés que dependiera de él desaparece del grafo, y lo publicado para ese interés se descarta sin aviso. **Esto es razonamiento propio a partir del modelo general de core NATS, no una frase de la documentación de leaf nodes que lo confirme para este escenario en concreto** —el mismo vacío que ya señalaba el informe original.

### Durabilidad en el borde: dominio de JetStream + streams espejados

El único camino documentado para que el borde sobreviva un corte es que el propio leaf node ejecute JetStream con un **dominio** propio. La configuración de JetStream expone `domain`: «The JetStream domain the server is part of» (sin valor por defecto, no recargable en caliente). La documentación de topologías es explícita sobre por qué hace falta: «If `factory-1` runs its own JetStream (a local `ORDERS` store on the plant floor), it needs a JetStream domain», y advierte que «without distinct domains, a leaf's JetStream and its hub's JetStream collide». Es decir, no es una casilla que se marca: es un espacio de nombres de JetStream aparte en cada extremo.

Para llevar esos datos del dominio del borde al dominio del hub (o viceversa), el mecanismo documentado son los **streams espejo** (`mirror`) o **fuente** (`source`): «A mirror is a stream that continuously copies every message from one upstream stream», con secuencia y timestamp idénticos al origen, de solo lectura, y con retención propia e independiente de la del origen. La sincronización es asíncrona: «A mirror is eventually consistent: the server copies the upstream stream continuously, so the mirror can run slightly behind», y expone un campo `Lag` para medir cuánto. La configuración de un mirror es fija en el momento de crearlo —«you can't point a mirror at a different upstream or add a filter later»—, mientras que un `source` si admite cambios después de creado. Ninguna de las dos páginas de referencia consultadas describe paso a paso un ejemplo concreto de mirror leaf-a-hub para este escenario de borde con enlace intermitente; hay que montarlo a partir de las piezas generales.

**Matiz sobre una fuente no oficial:** el sitio de recursos de Synadia (la empresa detrás de NATS, no la documentación de referencia) afirma en tono comercial: «when the upstream connection drops, the leaf node keeps running (...) With JetStream enabled, messages destined for the cluster queue until connectivity is restored—no data loss, no application changes.» Es una confirmación direccional de que el diseño (dominio + JetStream en el borde) sí logra el efecto buscado, pero es una página de marketing, no documentación de referencia, y no explica el mecanismo ni matiza los límites que sí documenta la referencia (consistencia eventual, `Lag`, configuración fija de los mirrors). Se cita porque es una de las fuentes que este ticket pedía consultar explícitamente, no porque sustituya a la documentación técnica.

### Huella de recursos

La página de sizing documenta los defaults de JetStream —«75% of system RAM (...) falling back to **256 MB**» para almacenamiento en memoria si no se puede leer el sistema, y «75% of the disk space actually available under `store_dir` (...) falling back to **1 TB**» para almacenamiento en fichero— y que «JetStream spends roughly two FDs per stream». Ninguna de las páginas de sizing, JetStream ni leaf nodes da una cifra mínima de RAM o disco para hardware de clase Raspberry Pi ni para un hub de 2 vCPU / 4 GB. La única cifra concreta relacionada con el borde es la de marketing de Synadia: el leaf node es «a standalone NATS server (20MB binary)», que solo describe el tamaño del binario, no el consumo en ejecución.

### Entrega, orden y deduplicación al reconectar

- **At-least-once, no exactly-once, con deduplicación opcional por cabecera.** El contrato de publicación documenta un `PubAck` con `Duplicate: false` para un mensaje nuevo o `true` si el servidor lo reconoció como repetido. La deduplicación se activa con la cabecera `Nats-Msg-Id`: «the server refuses to store the same ID twice within the stream's duplicate-tracking window», con una ventana «two minutes by default». La documentación es explícita en que esto no es exactamente-una-vez: «A `PubAck` means the stream stored the message, not that a consumer processed it», y un timeout en la publicación «no confirmation, not no write: the server may have stored the message and the ack got lost on the way back» —un reintento sin `Nats-Msg-Id` puede duplicar.
- **Confirmación explícita y doble-ack para los casos críticos.** Con `ack_policy Explicit`, «the server only advances that position once a reader acks each message»; si no llega ack en el plazo (`Ack Wait`), «the server assumes the reader failed and delivers the message again». Para cuando ni siquiera se puede tolerar un reproceso, existe el doble-ack: «the client sends the ack and waits for the server to confirm it before treating the message as done».
- **La reentrega no respeta el orden del stream.** «A redelivery doesn't slot back into stream order.» Si hace falta orden estricto, la documentación recomienda `MaxAckPending = 1`, lo que en la práctica serializa la entrega y limita el paralelismo.
- **Los streams `source` tampoco garantizan orden entre orígenes.** «The merge interleaves. Messages from one upstream keep their own order, but across upstreams there's no ordering guarantee.» Un mirror de un único origen sí conserva el orden y la secuencia del origen.

### El camino de vuelta para los comandos

Sin JetStream, NATS ofrece de fábrica el patrón *request-reply* sobre core NATS, con una señal explícita y documentada que faltaba mencionar en la opción B: el **no-responders**. «If none is [subscribed], the server sends back an immediate no-responders signal (a 503 status with no body)», que permite distinguir «a timeout means someone is there, so the client should try again soon» de «no responders means nobody is there yet». Para que los reintentos no dupliquen el efecto de un comando, la documentación recomienda una clave de idempotencia por operación: «`order-svc` keys every inventory check by its `order_id`» y el receptor recuerda los identificadores ya vistos para devolver la respuesta cacheada en lugar de reaplicar el efecto.

Pero ese patrón de petición-respuesta es *síncrono y en vivo*: no sirve si la Pi está desconectada cuando se emite el comando. Para que el comando sobreviva a un corte hace falta la misma topología de la sección anterior —el VPS publica el comando a un stream de su dominio JetStream, ese stream se espeja hacia el dominio del leaf node, la Pi lo consume con `ack_policy Explicit` cuando reconecta, traduce a la trama del ESP32, y la respuesta (`Ok`/`Invalid command`) vuelve por el mismo mecanismo en sentido opuesto—. La deduplicación por `Nats-Msg-Id` cubre el reenvío del propio comando; que el riego no se dispare dos veces sigue siendo diseño nuestro, igual que en la opción A.

### Valoración (razonamiento propio, no documentado en su totalidad)

Estudiar la variante nativa no abarata la decisión: confirma que el coste de NATS no estaba en la pasarela MQTT, sino en JetStream en el borde como tal. Para igualar lo que Mosquitto da con cinco líneas de configuración hace falta: JetStream habilitado en los dos extremos, un dominio explícito en el leaf, al menos un mirror configurado y monitorizado por `Lag`, una política de ack explícita, y (si se quiere evitar duplicados) cabeceras `Nats-Msg-Id` gestionadas por el código propio. A cambio se obtiene deduplicación con ventana configurable, doble-ack, y un patrón de petición-respuesta con señal de "nadie escucha" más rico que el de MQTT. Ninguna de esas capacidades resuelve un problema que este proyecto tenga hoy. Se reafirma el descarte de la opción B, ahora con más detalle de lo que costaría revertirlo si el proyecto creciera a muchos emplazamientos.

## Opción G — MQTT con NanoMQ (broker alternativo a Mosquitto)

Mismo diseño que la opción A —broker local en la Pi con un bridge hacia un broker en el VPS— sustituyendo Mosquitto por [NanoMQ](https://nanomq.io/), que se presenta como «Ultra-lightweight and Blazing-fast MQTT Broker for IoT Edge».

### Persistencia: opt-in, pero un peldaño más arriba que en Mosquitto

Este es el hallazgo equivalente, para NanoMQ, a los cinco defaults de Mosquitto: **el soporte de persistencia en disco (SQLite) no viene compilado en el paquete ni en la imagen que se instalan por defecto.**

La documentación de compilación lo dice sin rodeos: «SQLite3, which is used for message persistence, isn't built by default. To enable it, use the `-DNNG_ENABLE_SQLITE=ON` flag.» Eso se traduce en una matriz de variantes empaquetadas, documentada explícitamente tanto para Docker como para Linux:

| Variante | SQLite | Cómo se obtiene |
| --- | --- | --- |
| Docker `emqx/nanomq:latest` (básica, la del comando de arranque rápido de la propia web) | **❌** | — |
| Docker `emqx/nanomq:<versión>-slim` | ✅ | hay que pedir la etiqueta explícitamente |
| Docker `emqx/nanomq:<versión>-full` | ✅ | hay que pedir la etiqueta explícitamente |
| `apt-get install nanomq` / `yum install nanomq` (paquete básico) | **❌** | — |
| Paquete/AUR `nanomq-sqlite` | ✅ | hay que pedir el paquete explícitamente |
| Paquete/AUR `nanomq-full` | ✅ | hay que pedir el paquete explícitamente |

Es decir: quien siga literalmente el comando de la página de inicio (`docker run ... emqx/nanomq:latest`) o el de instalación en Linux (`apt-get install nanomq`) obtiene un broker **sin ninguna posibilidad de persistencia en disco para el bridge**, ni activada ni desactivable por configuración —no está compilada—. Es un fallo silencioso más severo en cierto sentido que los defaults de Mosquitto, porque no se corrige editando un fichero: hay que reinstalar con otro nombre de paquete o de imagen.

Con SQLite habilitado, el bloque de configuración (`sqlite { ... }` a nivel de broker, o `bridges.mqtt.cache { ... }` compartido entre bridges) expone:

| Opción | Valor por defecto documentado | Qué significa |
| --- | --- | --- |
| `disk_cache_size` | `102400` mensajes | «the maximum number of messages that can be cached in the SQLite database»; `0` = caché inefectiva. No documenta qué ocurre al llenarse. |
| `mounted_file_path` | ruta de ejecución de NanoMQ | dónde se guarda el fichero SQLite. |
| `flush_mem_threshold` | `100` mensajes | umbral de mensajes acumulados antes de volcar a SQLite. |
| `resend_interval` (bloque `sqlite`) | `5000` ms | **Solo aplica a clientes locales reconectando, no al bridge**: «Only work for the NanoMQ broker to resend cached messages to local client, not for bridging connections.» |
| `resend_interval` (bloque `bridges.mqtt.<nombre>`) | `5000` ms | Éste sí es del bridge: «Only takes effect in bridging», timer del reenvío de QoS pendiente. |

Aviso de terminología: hay **dos parámetros con el mismo nombre** (`resend_interval`) en bloques de configuración distintos, con alcance distinto —uno para clientes locales, otro para el bridge—. Conviene que quien escriba el ADR lo señale explícitamente, porque es fácil confundirlos al leer un `nanomq.conf` de ejemplo.

La propia documentación del bridge confirma el efecto combinado: «If you enabled SQLite feature, NanoMQ will automatically flush cached messages into disk when network is disconnected. NanoMQ will resend cached messages once bridging connection is restored. But each cached message will be resent in a certain interval to avoid bandwidth exhaustion.»

### Bridging: qué expone el `bridge` de NanoMQ

El bridge se declara en un bloque `bridges.mqtt.<nombre>` con transporte TCP (`mqtt-tcp://host:puerto`) o QUIC (`mqtt-quic://host:puerto`), protocolo MQTT v3.1/v3.1.1/v5 configurable (`proto_ver`), remapeo de topics con prefijo/sufijo y comodines en ambas direcciones (`forwards`/`subscription`), y ajustes de cola:

- `max_send_queue_len` / `max_recv_queue_len`: tamaño de la ventana en vuelo, `1024` en el ejemplo HOCON de la web y `32`/`128` en el formato KV clásico —**no hay un único valor por defecto documentado, cambia entre ejemplos y formatos de configuración**—.
- `resend_interval`, `resend_wait`, `cancel_timeout`: temporizador de reintento de QoS, espera antes de empezar a reintentar, y plazo máximo antes de dejar de esperar el ACK. La documentación aclara que cancelar la espera de un ACK «doesn't actually mean the msg is lost; just means it stopped waiting for the ACK (...) from the remote broker»: sigue en la ventana en vuelo o en la caché SQLite si está habilitada.
- `hybrid_bridging` / `hybrid_servers`: lista de brokers remotos alternativos, con reintento en orden ante fallos de conexión —una forma de failover que Mosquitto no tiene documentada—.

Ni la introducción de bridges ni la referencia de configuración mencionan Mosquitto por nombre como broker remoto compatible. El bridge habla MQTT estándar sobre TCP con versión de protocolo configurable, lo que hace razonable esperar que interopere con Mosquitto como con cualquier otro broker MQTT 3.1.1/5 —**pero esto es inferencia, no una frase de la documentación que confirme la combinación NanoMQ↔Mosquitto probada**—.

Sobre el comportamiento de la ventana en vuelo al llenarse, la documentación dice: «It will be dropped only if the inflight window is full and new QoS msg keep comming» —**no especifica si se descarta el mensaje entrante nuevo o el más antiguo de la cola**, ambigüedad que conviene resolver empíricamente antes de fijar `max_send_queue_len` en producción, igual que el informe original señalaba para `max_queued_messages` en Mosquitto.

### Huella de recursos

La web comercial afirma un arranque «less than 200Kb in minimum feature set» y «up to 10 times faster than Mosquitto on a multi-core CPU» —afirmaciones de marketing, no de un informe de benchmarking neutral—. El informe de pruebas propio de NanoMQ sí aporta cifras medidas, aunque en hardware x86_64, no ARM:

- 8 núcleos / 16 GB RAM (Xeon Gold 6266C), 500 000 msg/s: «process memory consumption is around 10M and each CPU only consumes around 80%».
- 1 núcleo / 2 GB RAM (Xeon Gold 6278C), 200 000 msg/s sostenidos.
- Prueba uno-a-uno: 14 000 msg/s con «CPU usage is 30% and the memory consumption is around 200MB».

**Ninguna de estas pruebas se ejecuta en Raspberry Pi ni en arquitectura ARM**, así que la huella exacta en la Pi de este proyecto no está confirmada por fuentes primarias, solo por la cifra de arranque en frío (200 KB) y por el hecho de que sí existen paquetes ARM (ver empaquetado).

### Empaquetado

- **Docker:** tres imágenes (`básica`/`slim`/`full`), diferenciadas en la tabla de funciones citada arriba; configuración por fichero montado (`docker cp` + `-v`) o por variables de entorno (`NANOMQ_CONF_PATH`, `NANOMQ_TLS_ENABLE`, etc., documentadas con tipo y valor por defecto).
- **Linux:** instalación por Apt/Yum con script propio (`install-nanomq-deb.sh`/`-rpm.sh`), o por paquete `.deb`/`.rpm` directo. Arquitecturas documentadas: `amd64, arm64, riscv64, mips, armhf, armel, x86_64` —**`armhf` y `arm64` cubren Raspberry Pi**—.
- **Formato de configuración:** dos formatos en paralelo, documentados como tales: HOCON (`nanomq.conf`, el que usan los ejemplos nuevos) y KV clásico (`nanomq_old.conf`, formato v0.13 en vías de quedar solo como apéndice). Conviene fijar HOCON como estándar del proyecto si se elige esta opción, porque es el que la documentación actual trata como principal.
- **`systemd`:** **no se ha encontrado ninguna unidad `systemd` ni en la documentación ni en el repositorio fuente.** Se revisó el árbol del repositorio (`github.com/nanomq/nanomq`); la carpeta `deploy/` solo contiene `docker/`, y una búsqueda de código en el repositorio por `systemd` no devolvió resultados. A diferencia de Mosquitto —que en Debian/Ubuntu trae su unidad de fábrica—, aquí no está confirmado que el paquete `.deb` instale un servicio arrancable con `systemctl`; es un punto a verificar en el propio paquete antes de decidir, no algo que la documentación resuelva.

### Comparación directa con el punto flaco ya encontrado en Mosquitto

Mosquitto también viene inseguro por defecto, pero el arreglo es editar cinco líneas de un fichero de configuración que cualquiera puede repasar en una revisión de código. En NanoMQ el arreglo empieza **antes** de escribir configuración: hay que haber elegido el paquete o la imagen correctos. Un `git diff` de `nanomq.conf` no lo va a mostrar si alguien instaló accidentalmente la variante básica; hace falta comprobar el binario o el paquete instalado. Para un proyecto que van a mantener estudiantes rotando, eso es una superficie de error más difícil de auditar que la de Mosquitto, aunque el motivo de fondo —persistencia apagada por defecto— sea de la misma familia.

### Valoración

NanoMQ es una alternativa MQTT real y bien documentada, con más controles finos de bridge que Mosquitto (colas de envío/recepción separadas, `hybrid_bridging` para failover, cancelación explícita de espera de ACK) y una huella medida menor en las pruebas propias, aunque no verificada en ARM. No cambia la recomendación porque no elimina el riesgo que motivó la condición de la opción A —persistencia apagada por defecto—, y lo traslada a un punto (selección de paquete/imagen) más difícil de detectar en revisión que un fichero de configuración, además de no tener confirmada una unidad `systemd` de fábrica. Queda anotada como opción de reserva si el rendimiento de Mosquitto se convirtiera en un problema real.

## Comparativa

| | Store-and-forward | Código propio añadido | Piezas nuevas | Camino de vuelta |
| --- | --- | --- | --- | --- |
| **A. Mosquitto + bridge** | Sí, por configuración (con los defaults corregidos) | Ninguno más allá del lector | 1 broker en cada extremo | `topic ... in` |
| **B. NATS** | Sí, con JetStream en el borde + dominio + mirror | Ninguno, pero mucha topología | 2 servidores + streams | Nativo |
| **C. HTTP + cola** | Sí, escribiéndola nosotros | Spool, reintentos, backoff, recuperación | API propia | Polling o webhook |
| **D. BD directa** | No | Buffer propio si se quiere | Ninguna | No natural |
| **E. Telegraf desde el serie** | No (buffer sobrescribe; disco experimental) | Script para el serie de todos modos | 1 agente | No |
| **F. NATS nativo (sin MQTT)** | Sí, con JetStream en el borde + dominio + mirror (igual que B) | Ninguno, pero mucha topología + gestión de `Nats-Msg-Id` para dedup | 2 servidores + streams espejo | Request-reply con no-responders, o stream + ack explícito para que sobreviva a un corte |
| **G. NanoMQ + bridge** | Sí, pero solo si se instala el paquete/imagen `-sqlite`/`-full` (no es el paquete por defecto) | Ninguno más allá del lector | 1 broker en cada extremo | `subscription` en el bridge |

## Lo que no se ha podido determinar con fuentes primarias

- **Valores por defecto numéricos de Telegraf** (`metric_buffer_limit`, `metric_batch_size`, `flush_interval`): no aparecen en la página de configuración ni en `docs/CONFIGURATION.md` del repositorio; solo se documenta la descripción del comportamiento. El fichero `etc/telegraf.conf` del repositorio devolvió 404 en la rama `master`.
- **Comportamiento exacto de un leaf node de NATS ante un enlace caído** sin JetStream en el borde: no está documentado, ni para afirmarlo ni para negarlo, ni en la variante con pasarela MQTT (B) ni en la nativa (F). Si NATS llegara a considerarse en serio, esto habría que medirlo en un banco de pruebas, no deducirlo.
- **Cifras mínimas de RAM y disco** de JetStream, tanto para un leaf node en una Raspberry Pi como para el hub en el VPS de 2 vCPU/4 GB: no publicadas. Solo hay una cifra de marketing de Synadia sobre el tamaño del binario del leaf node (20 MB), que no es lo mismo que el consumo en ejecución.
- **Comportamiento de Mosquitto con `max_queued_messages 0`** ante un corte de muy larga duración: la documentación dice «no maximum» pero no describe qué ocurre al agotarse la memoria. A la cadencia de este proyecto no debería llegar a ser un problema, pero no está documentado y conviene fijar un límite alto explícito en lugar de `0`.
- **Interacción entre `max_queued_messages` y la cola del bridge concretamente**: se deduce de que el bridge es un cliente del broker local y de que el límite es por cliente, pero no hay una frase en la documentación que lo afirme para el caso del bridge. Merece una prueba empírica antes de fijar el valor en producción.
- **Qué hace exactamente NanoMQ cuando se llena la ventana en vuelo del bridge** (`max_send_queue_len`): la documentación dice que el mensaje «is dropped» al llenarse, pero no especifica si descarta el mensaje entrante nuevo o el más antiguo ya encolado.
- **Qué hace exactamente NanoMQ cuando se llena `disk_cache_size`** (la caché SQLite del bridge, 102 400 mensajes por defecto): no documentado, el mismo tipo de vacío que el de Mosquitto con `max_queued_messages 0`.
- **Si el paquete `.deb`/`.rpm` de NanoMQ instala una unidad `systemd`**: no encontrado ni en la documentación ni en el repositorio fuente (`deploy/` solo contiene `docker/`; una búsqueda de código por `systemd` en el repositorio no dio resultados). Habría que comprobarlo instalando el paquete, no se puede confirmar por lectura de fuentes.
- **Interoperabilidad de bridge probada entre NanoMQ y Mosquitto concretamente**: no confirmada por la documentación, que no nombra a Mosquitto como broker remoto de referencia; se infiere de que ambos hablan MQTT estándar sobre TCP.
- **Huella de NanoMQ en hardware ARM/Raspberry Pi**: los únicos benchmarks publicados (`test-report`) se ejecutan en Xeon x86_64; solo está confirmado que existen paquetes para `armhf`/`arm64`, no el rendimiento real en ellos.

## Fuentes

- Mosquitto, página de manual `mosquitto.conf(5)`: <https://mosquitto.org/man/mosquitto-conf-5.html>
- NATS, JetStream: <https://docs.nats.io/nats-concepts/jetstream>
- NATS, Leaf Nodes: <https://docs.nats.io/running-a-nats-service/configuration/leafnodes>
- NATS, configuración MQTT: <https://docs.nats.io/running-a-nats-service/configuration/mqtt>
- `nats-server`, `server/README-MQTT.md` (código fuente): <https://github.com/nats-io/nats-server/blob/main/server/README-MQTT.md>
- NATS, Leaf Nodes (guía): <https://docs.nats.io/learn/topologies/leaf-nodes>
- NATS, referencia de configuración de Leaf Nodes: <https://docs.nats.io/reference/config/leafnodes>
- NATS, referencia de configuración de JetStream (`domain`, `store_dir`, `max_memory_store`, `max_file_store`): <https://docs.nats.io/reference/config/jetstream>
- NATS, Mirrors and Sources: <https://docs.nats.io/learn/jetstream/mirrors-and-sources>
- NATS, Sizing & Resources: <https://docs.nats.io/learn/deployment/sizing-and-resources>
- NATS, Connect MQTT devices to NATS: <https://docs.nats.io/learn/mqtt>
- NATS, Delivery and acknowledgment (JetStream): <https://docs.nats.io/learn/jetstream/delivery-and-acknowledgment>
- NATS, Publishing (PubAck, `Nats-Msg-Id`, duplicate window): <https://docs.nats.io/learn/jetstream/publishing>
- NATS, Publish-subscribe (core NATS): <https://docs.nats.io/learn/core-nats/publish-subscribe>
- NATS, Connection lifecycle (reconnect buffer del cliente): <https://docs.nats.io/learn/core-nats/connection-lifecycle>
- NATS, Request-reply (core NATS): <https://docs.nats.io/learn/core-nats/request-reply>
- NATS, Request-reply resilience: <https://docs.nats.io/learn/resilient-clients/request-reply-resilience>
- Synadia, recursos y documentación de producto (fuente comercial, no de referencia): <https://nats.synadia.com/>
- NanoMQ, página de producto: <https://nanomq.io/>
- NanoMQ, documentación — introducción a Data Bridges: <https://nanomq.io/docs/en/latest/bridges/introduction.html>
- NanoMQ, documentación — MQTT over TCP Bridge: <https://nanomq.io/docs/en/latest/bridges/tcp-bridge.html>
- NanoMQ, documentación — configuración de Data Bridges: <https://nanomq.io/docs/en/latest/config-description/bridges.html>
- NanoMQ, documentación — configuración del broker (caché SQLite): <https://nanomq.io/docs/en/latest/config-description/broker.html>
- NanoMQ, documentación — configuración de MQTT Messaging: <https://nanomq.io/docs/en/latest/config-description/mqtt.html>
- NanoMQ, documentación — MQTT Stream, introducción: <https://nanomq.io/docs/en/latest/mqtt-stream/introduction.html>
- NanoMQ, documentación — MQTT Stream, configuración: <https://nanomq.io/docs/en/latest/mqtt-stream/configuration.html>
- NanoMQ, documentación — despliegue con Docker: <https://nanomq.io/docs/en/latest/installation/docker.html>
- NanoMQ, documentación — paquetes Linux: <https://nanomq.io/docs/en/latest/installation/packages.html>
- NanoMQ, documentación — compilación desde el código fuente: <https://nanomq.io/docs/en/latest/installation/build-options.html>
- NanoMQ, documentación — informe de pruebas de rendimiento: <https://nanomq.io/docs/en/latest/test-report.html>
- NanoMQ, código fuente (para confirmar la ausencia de unidad `systemd`): <https://github.com/nanomq/nanomq>
- Telegraf, configuración: <https://docs.influxdata.com/telegraf/v1/configuration/>
- Telegraf, `docs/CONFIGURATION.md`: <https://github.com/influxdata/telegraf/blob/master/docs/CONFIGURATION.md>
- Telegraf, plugin `modbus`: <https://github.com/influxdata/telegraf/blob/master/plugins/inputs/modbus/README.md>
- Telegraf, plugin `mavlink`: <https://docs.influxdata.com/telegraf/v1/input-plugins/mavlink/>
- Telegraf, peticiones de plugin de puerto serie: <https://github.com/influxdata/telegraf/issues/7218> y <https://github.com/influxdata/telegraf/issues/10349>
