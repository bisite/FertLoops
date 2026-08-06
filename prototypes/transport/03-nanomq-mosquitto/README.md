# 03 · NanoMQ en el borde, Mosquitto en el hub

**Código desechable.** Existe para responder con medidas, no para desplegarse. Ver el
[protocolo compartido](../README.md), que este prototipo sigue tal cual.

## Qué prueba

La opción "NanoMQ en la Raspberry Pi con bridge MQTT hacia el Mosquitto del VPS", que el informe
de [#4](https://github.com/bisite/FertLoops/issues/4) dejó como alternativa a Mosquitto-Mosquitto
por su menor consumo en el borde, pero con dos incógnitas señaladas explícitamente como **no
confirmadas por documentación**:

1. **¿La persistencia de bridge de NanoMQ está realmente ausente del paquete por defecto?**
   Los documentos de compilación dicen que SQLite "no se compila por defecto", sin decir qué pasa
   si aun así configuras la caché. Una opción de persistencia *ignorada en silencio* es mucho más
   peligrosa que una que da error.
2. **¿Funciona siquiera el bridge NanoMQ → Mosquitto?** La documentación de NanoMQ nunca nombra a
   Mosquitto como broker remoto compatible; que funcione se deduce de que ambos hablan MQTT
   estándar, no está escrito en ningún sitio.

Y de paso: qué extremo de la cola se descarta al desbordar (`max_send_queue_len` en memoria,
`disk_cache_size` en disco), que tampoco está documentado, y si el paquete trae unidad `systemd`.

| | Borde (Raspberry Pi) | Hub (VPS) |
| --- | --- | --- |
| Imagen | `emqx/nanomq:latest` o `emqx/nanomq:0.25.5-slim` | `eclipse-mosquitto` 2.1.2 (fijada por digest) |
| Versión | NanoMQ v0.25.5-6 en las dos imágenes | |

Los dos perfiles:

- **`defaults`** — `emqx/nanomq:latest`, un bridge copiado de la documentación con un `forwards`
  a QoS 1 y **nada más tocado**: sin `max_send_queue_len`, sin bloque de caché.
- **`hardened`** — `emqx/nanomq:0.25.5-slim` (la variante que sí trae SQLite), con
  `bridges.mqtt.cache` activado, `max_send_queue_len = 4096`, `clean_start = false` y QoS 1.

## Cómo se corre

```sh
./run-experiment.sh defaults
./run-experiment.sh hardened
```

Un solo comando por perfil. Todo en contenedores, ningún puerto publicado al host, proyecto
`fl-proto-nanomq`, y `docker compose down -v` al terminar (también si falla a mitad). Cada corrida
tarda unos 6 minutos y hace, en este orden:

| Paso | Qué hace |
| --- | --- |
| **PREFLIGHT** | Sonda de SQLite: arranca la imagen del perfil con la caché configurada, con y sin el directorio de `mounted_file_path`, y mira qué pasa. |
| **P0 – P4** | Las cinco fases del protocolo compartido: warmup 10 s, corte 60 s (`docker network disconnect`), corte de corriente 20 s (`docker kill -s SIGKILL` + arranque), restauración ≤ 90 s, verificación. |
| **P5** | Sonda de desbordamiento: `max_send_queue_len = 8` y 200 mensajes con el enlace caído, para ver qué extremo sobrevive. |
| **P6** | Sonda de persistencia: el mismo corte que P1 **sin** corte de corriente. Sin esto no se puede separar "la cola no aguantó" de "el SIGKILL se lo llevó". |

El simulador publica a 20 msg/s durante 120 s (los 90 s de P0+P1+P2 más los primeros 30 s de P3,
para que el desorden al restaurar sea medible con tráfico vivo compitiendo con el atasco). Un
mensaje cuenta como **publicado** solo cuando el broker de borde devuelve el PUBACK; si el broker
no está, el simulador ni se lo pasa a paho y lo anota aparte, para no medir el búfer de paho en vez
del del broker.

---

## Resultados medidos

Corridas del 2026-08-06 con `emqx/nanomq@sha256:a03605fe4061452609e0cd89e4ac0c382dc5bb338319e712bb25b6d21a72ba0a`
(`:latest`) y `emqx/nanomq@sha256:a0877323167e3189305e78d442f4de84762bd1892472f2d78dee0aae970d6a76`
(`:0.25.5-slim`).

### Resumen

| | `defaults` | `hardened` |
| --- | --- | --- |
| Aceptados por el borde (PUBACK) | 791 de 2400 intentos | 2260 de 2400 intentos |
| Llegados al hub (`seq` únicos) | 751 | 1048 |
| Perdidos de lo aceptado | 40 | 1212 |
| Recuperados del corte P1 | **0 de 34** | **0 de 1211** |
| Duplicados | 0 | 0 |
| Desorden | 0 | 345 llegadas fuera de secuencia |
| ¿Sobrevivió el broker? | **No: 2 SIGSEGV** | Sí |
| Corte de 60 s **sin** corte de corriente (P6) | **0 de 33** recuperados, y el broker también se murió | **1000 de 1000**, 0 huecos |

Las dos cifras que importan están en la última fila. **`hardened` recupera el 100 % de un corte de
enlace de 60 s… siempre que no haya corte de corriente.** Con el SIGKILL de por medio, recupera
cero. **`defaults` no recupera nada en ningún escenario, y además el broker se cae.**

### 1 · El bridge NanoMQ → Mosquitto sí funciona

Confirmado, sin reservas. NanoMQ 0.25.5 se conecta a Mosquitto 2.1.2 sin nada especial en la
configuración de ninguno de los dos:

```
2026-08-06 10:58:39 [17] INFO  /nanomq/nanomq/bridge.c:1145 bridge_tcp_connect_cb: Bridge [mqtt-tcp://mosquitto-hub:1883] connected! RC [0]
2026-08-06 10:58:39 [17] INFO  /nanomq/nanomq/bridge.c:1179 bridge_tcp_connect_cb: No subscriptions were set.
```

En P0 llegaron **213 de 213** mensajes en los dos perfiles, y en la sonda P6 del perfil `hardened`
llegaron **1000 de 1000**. El único ajuste necesario es escribir la URL con el esquema propio de
NanoMQ, `mqtt-tcp://`, no `tcp://`.

### 2 · La persistencia SQLite: ausente de la imagen por defecto, y **falla en silencio**

Esta es la respuesta más importante del prototipo, y es la mala.

**Evidencia estática.** Contando símbolos de la API C de SQLite en el binario del broker
(`grep -aoE "sqlite3_[a-z0-9_]+" /usr/local/nanomq/nanomq | sort -u | wc -l`):

| Imagen | Símbolos `sqlite3_*` | Versión NanoMQ |
| --- | --- | --- |
| `emqx/nanomq:latest` (Alpine) | **0** | v0.25.5-6 |
| `emqx/nanomq:0.25.5-slim` (Debian) | **268** | v0.25.5-6 |

Misma versión de NanoMQ, distinto build. SQLite no está en `:latest`.

**Evidencia dinámica.** El bloque `bridges.mqtt.cache` es aceptado por el parser de las **dos**
imágenes, y las dos lo imprimen de vuelta en el volcado de configuración del arranque (con el
nombre interno `bridge.sqlite.*`, que es el único indicio de que se ha leído):

```
2026-08-06 10:53:53 [1] INFO  .../conf.c:3894 print_bridge_conf: bridge.sqlite.disk_cache_size: 102400
2026-08-06 10:53:53 [1] INFO  .../conf.c:3896 print_bridge_conf: bridge.sqlite.mounted_file_path: /nanomq/data/
2026-08-06 10:53:53 [1] INFO  .../conf.c:3898 print_bridge_conf: bridge.sqlite.flush_mem_threshold: 100
```

Esas tres líneas son de `emqx/nanomq:latest`, la imagen que **no tiene SQLite**. Son idénticas a
las de `:0.25.5-slim`. No hay ni un aviso, ni un `WARN`, ni un código de salida distinto.

Lo que las separa es lo que ocurre después:

| Sub-comprobación | `:latest` | `:0.25.5-slim` |
| --- | --- | --- |
| A) `mounted_file_path` **no existe** | arranca bien, `exit=0` | **muere en el arranque, `exit=139`** |
| A) ¿acepta la clave el parser? | sí | sí |
| A) ¿panic al abrir la base de datos? | no | sí |
| B) `mounted_file_path` **existe** | directorio **vacío** | crea `mqtt_client.db`, `-shm`, `-wal` |

El panic de la variante con SQLite, que es la prueba de que ahí el código sí se ejecuta:

```
panic: Can't open database /nanomq/data/mqtt_client.db: unable to open database file
This message is indicative of a BUG.
```

Y el contenido de `mounted_file_path` en `:latest` tras arrancar con la caché configurada:

```
total 8
drwxr-xr-x    2 root     root          4096 Aug  6 10:53 .
drwxr-xr-x    1 root     root          4096 Aug  6 10:54 ..
```

Vacío. **Respuesta: sí, la persistencia de bridge está ausente del paquete por defecto, y
configurarla ahí no da error de ningún tipo — se ignora en silencio.** Quien despliegue
`emqx/nanomq:latest` con un bloque `bridges.mqtt.cache` en su configuración creerá que tiene
persistencia, verá sus parámetros ecoados en el log del arranque, y no tendrá nada.

Efecto secundario útil: la variante `-slim` **exige que el directorio de `mounted_file_path` ya
exista** y se mata en el arranque si no. Es ruidoso, pero al menos es honesto.

### 3 · Pero la caché SQLite tampoco almacena nada (perfil `hardened`)

Este resultado no se esperaba. Con la imagen correcta, la base de datos creada y
`disk_cache_size = 102400`, la tabla donde irían los mensajes encolados del bridge **se queda a
cero filas** durante todo el corte:

```
cache SQLite con el enlace caido, antes del SIGKILL:
  ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
cache SQLite con el broker muerto:
  ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
cache SQLite tras el rearranque:
  ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
```

`t_client_msg: 0` en los tres momentos, con 1211 mensajes esperando a ser reenviados. Lo mismo en
la sonda P6, donde el corte se aguanta entero y se recupera el 100 %: también `t_client_msg: 0`.

Es decir: **en NanoMQ 0.25.5 lo único que aguanta un corte es `max_send_queue_len`, que está en
RAM.** La caché de disco crea el fichero, crea las tablas, y no guarda mensajes. Por eso `hardened`
recupera 1000 de 1000 en P6 (cabe todo en los 4096 huecos de memoria) y 0 de 1211 en P1 (el SIGKILL
se lleva la RAM y en disco no había nada).

Esto coincide con [nanomq/nanomq#1741](https://github.com/nanomq/nanomq/issues/1741), abierto en
abril de 2024, todavía **sin resolver** y con una configuración casi igual a la nuestra
(`disk_cache_size=102400`, `flush_mem_threshold=1`): *"We are seeing lost messages"* donde se
esperaba recuperación completa.

Se le dio a la caché su mejor oportunidad y siguió sin escribir: con `max_send_queue_len = 8`,
`disk_cache_size = 102400` y `flush_mem_threshold = 1`, publicando 200 mensajes con el enlace
caído, `t_client_msg` seguía en 0 y llegaron los mismos 40 mensajes que sin caché ninguna.

### 4 · El perfil `defaults` no pierde mensajes: se cae

El bridge sin `max_send_queue_len` no sobrevive a que el enlace se caiga mientras hay tráfico QoS 1.
Reproducido **tres veces en una sola corrida** de `./run-experiment.sh defaults` (dos SIGSEGV en el
experimento principal y uno en la sonda P6):

```
2026-08-06 10:54:21 [24] WARN  /nanomq/nanomq/bridge.c:1956 bridge_pub_handler: Cached Message in ctx_msgs is lost!
2026-08-06 10:54:21 [24] WARN  /nanomq/nanomq/bridge.c:1962 bridge_pub_handler: Msg lost! put msg to ctx_msgs failed!
2026-08-06 10:54:21 [12] WARN  /nanomq/nanomq/bridge.c:1956 bridge_pub_handler: Cached Message in ctx_msgs is lost!
2026-08-06 10:54:21 [12] ERROR /nanomq/nanomq/apps/broker.c:118 sig_handler: signal signumber: 11 received!
```

Señal 11 es SIGSEGV. El contenedor termina con `exit=1`. Ocurrió a los ~1,7 s del corte (34
mensajes a 20 msg/s), y otra vez a los ~2 s de rearrancarlo con el enlace todavía caído.

No es un artefacto de `docker network disconnect`: se reprodujo igual **parando el contenedor del
hub**, dejando la interfaz de red intacta. Lo que lo dispara es que el bridge se quede sin destino
mientras siguen entrando mensajes QoS 1.

Y **sí se puede evitar con configuración**: la misma `emqx/nanomq:latest`, con lo único añadido de
`max_send_queue_len = 4096`, aguantó 30 s de corte sin caerse. La causa es no fijar el tamaño de
cola, no la imagen.

Consecuencia práctica: el broker de borde muere solo, y como el paquete **no trae unidad
`systemd`** (ver punto 6), nadie lo levanta.

### 5 · Al desbordar, se descarta lo más NUEVO

Sonda P5, con `max_send_queue_len = 8` y 200 mensajes publicados con el enlace caído. Resultado
**idéntico en los dos perfiles**, con y sin caché de disco:

```
  ventana del corte  : seq 1..200
  supervivientes     : seq 1..200
  reparto            : 32 en la primera mitad / 8 en la segunda
  VERDICTO           : descarta los MAS NUEVOS (conserva la cabeza: lo mas antiguo)
```

Sobreviven `seq 1-32` y `seq 193-200`. Se pierde el bloque `33-192` de un tirajo. Es decir:

- Una vez llena la cola, **los mensajes nuevos se tiran** y los viejos se conservan — lo contrario
  de lo que interesa en fertirriego, donde el dato reciente vale más que el de hace tres horas.
- Los 8 últimos (`193-200`) son exactamente `max_send_queue_len`: la ventana de envío se sigue
  refrescando con lo último que entra.
- Los 32 primeros coinciden con el número de contextos paralelos del broker (`parallel: 32` en el
  volcado de arranque), que es donde aparece el `ctx_msgs` de los mensajes de error del punto 4.

Que el perfil `hardened` con `disk_cache_size = 32` diera **exactamente los mismos 40
supervivientes** que el perfil sin SQLite es la confirmación cruzada del punto 3: el disco no
absorbe el desbordamiento.

> **Cuidado al leer el bloque "Comportamiento al desbordar" del experimento principal de
> `hardened`**, que dice "descarta los MÁS VIEJOS". Ahí el verificador está describiendo bien lo
> que ve (solo sobrevivieron los `seq` del final de la ventana) pero la causa no es un
> desbordamiento: es el SIGKILL, que borró la RAM con todo P1 dentro y dejó pasar solo lo publicado
> después del rearranque. La respuesta buena sobre el desbordamiento es la de la sonda P5.

### 6 · No hay unidad `systemd`

Ni en el árbol de fuentes en la etiqueta `0.25.5` (`repos/nanomq/nanomq/git/trees/0.25.5` no
contiene ningún `.service`), ni dentro de ninguna de las dos imágenes. Confirma lo que encontró la
investigación de #4. Con el punto 4 encima, hace falta supervisión propia sí o sí.

### Traducción a tiempo real de invernadero

Los topes se cuentan en mensajes, no en tiempo. Con una mesa de drenaje a 1 muestra / 10 s:

| Medida | Mensajes | Con 1 mesa | Con 4 mesas |
| --- | --- | --- | --- |
| Recuperado por `hardened` sin corte de luz (P6) | 887 durante el corte | ≈ 2,5 h | ≈ 37 min |
| Recuperado por `hardened` con corte de luz | 0 | 0 | 0 |
| Recuperado por `defaults` | 0 | 0 | 0 |

Los 887 son lo que se llegó a medir, no el techo: con `max_send_queue_len = 4096` la cola no se
llenó en ningún momento de la sonda.

---

## Salida cruda

### Perfil `defaults` — experimento principal

```
==============================================================================
VEREDICTO  03-nanomq-mosquitto  perfil=defaults
imagen de borde: emqx/nanomq:latest
generado: 2026-08-06 10:56:21+0000
==============================================================================

-- Totales ------------------------------------------------------------
  intentos de publicacion (simulador)      : 2400
  aceptados por el broker de borde (PUBACK): 791
  nunca entregados a paho (broker caido)   : 1609
  llegados al hub (mensajes, con repetidos): 751
  llegados al hub (seq unicos)             : 751
  tasa de entrega sobre lo aceptado        : 94.94 %
  cadencia configurada                     : 20.0 msg/s durante 120.0 s (qos=1)

-- Por fase (sobre lo aceptado por el borde) --------------------------
  fase                        intentos  aceptados  llegados  perdidos
  P0 warmup                        213        213       213         0
  P1 corte                        1211         34         0        34
  P2 corte corriente               429          6         0         6
  P3 restauracion                  547        538       538         0

-- Huecos ------------------------------------------------------------
  seq aceptados que nunca llegaron: 40
  rangos (2):
    214-247                  34 msg   fase: P1 corte
    1596-1601                 6 msg   fase: P2 corte corriente

-- Duplicados --------------------------------------------------------
  seq llegados mas de una vez : 0
  copias extra totales        : 0
  mensajes con flag MQTT DUP  : 0

-- Desorden ----------------------------------------------------------
  llegadas fuera de secuencia : 0
  mayor salto hacia atras     : 0 seq

-- Comportamiento al desbordar ---------------------------------------
  mensajes aceptados durante el corte : 40
  de ellos, llegaron al hub           : 0
  no sobrevivio ningun mensaje del corte: no hay extremo que medir

==============================================================================
-- Estabilidad del broker de borde -----------------------------------
  murio durante el experimento              : yes
  el broker de borde murio en P2 (estado=exited exit=1, SIGSEGV x2)
  lineas 'Msg lost' en el log del bridge    : 6
  SIGSEGV registrados por NanoMQ (signal 11): 2
  estado final del contenedor de borde      : running exit=0

-- Cache SQLite de bridge (filas en mounted_file_path) ---------------
  con el enlace caido, antes del SIGKILL : sin mqtt_client.db; contenido de mounted_file_path: (vacio)
  con el broker muerto                   : sin mqtt_client.db; contenido de mounted_file_path: (vacio)
  tras el rearranque                     : sin mqtt_client.db; contenido de mounted_file_path: (vacio)
======================================================================
```

### Perfil `defaults` — sonda de persistencia (corte SIN corte de corriente)

```
==============================================================================
VEREDICTO  03-nanomq-mosquitto  perfil=defaults-persistencia
imagen de borde: emqx/nanomq:latest
generado: 2026-08-06 10:58:00+0000
==============================================================================

-- Totales ------------------------------------------------------------
  intentos de publicacion (simulador)      : 1000
  aceptados por el broker de borde (PUBACK): 146
  nunca entregados a paho (broker caido)   : 854
  llegados al hub (mensajes, con repetidos): 113
  llegados al hub (seq unicos)             : 113
  tasa de entrega sobre lo aceptado        : 77.40 %
  cadencia configurada                     : 20.0 msg/s durante 50.0 s (qos=1)

-- Por fase (sobre lo aceptado por el borde) --------------------------
  fase                        intentos  aceptados  llegados  perdidos
  antes del corte                  113        113       113         0
  corte                            887         33         0        33
  restauracion                       0          0         0         0

-- Huecos ------------------------------------------------------------
  seq aceptados que nunca llegaron: 33
  rangos (1):
    114-146                  33 msg   fase: corte

-- Duplicados --------------------------------------------------------
  seq llegados mas de una vez : 0
  copias extra totales        : 0
  mensajes con flag MQTT DUP  : 0

-- Desorden ----------------------------------------------------------
  llegadas fuera de secuencia : 0
  mayor salto hacia atras     : 0 seq

==============================================================================
-- Contexto de la sonda ----------------------------------------------
  misma configuracion que el perfil defaults, sin docker kill
  estado del borde al terminar : running exit=0
  SIGSEGV                      : 1
  cache SQLite al terminar     : sin mqtt_client.db; contenido de mounted_file_path: (vacio)
======================================================================
```

### Perfil `hardened` — experimento principal

```
==============================================================================
VEREDICTO  03-nanomq-mosquitto  perfil=hardened
imagen de borde: emqx/nanomq:0.25.5-slim
generado: 2026-08-06 11:00:55+0000
==============================================================================

-- Totales ------------------------------------------------------------
  intentos de publicacion (simulador)      : 2400
  aceptados por el broker de borde (PUBACK): 2260
  nunca entregados a paho (broker caido)   : 140
  llegados al hub (mensajes, con repetidos): 1048
  llegados al hub (seq unicos)             : 1048
  tasa de entrega sobre lo aceptado        : 46.37 %
  cadencia configurada                     : 20.0 msg/s durante 120.0 s (qos=1)

-- Por fase (sobre lo aceptado por el borde) --------------------------
  fase                        intentos  aceptados  llegados  perdidos
  P0 warmup                        213        213       213         0
  P1 corte                        1211       1211         0      1211
  P2 corte corriente               429        289       288         1
  P3 restauracion                  547        547       547         0

-- Huecos ------------------------------------------------------------
  seq aceptados que nunca llegaron: 1212
  rangos (1):
    214-1425               1212 msg   fase: P1 corte

-- Duplicados --------------------------------------------------------
  seq llegados mas de una vez : 0
  copias extra totales        : 0
  mensajes con flag MQTT DUP  : 0

-- Desorden ----------------------------------------------------------
  llegadas fuera de secuencia : 345
  mayor salto hacia atras     : 345 seq

-- Comportamiento al desbordar ---------------------------------------
  mensajes aceptados durante el corte : 1500
  de ellos, llegaron al hub           : 288
  ventana del corte  : seq 214..1853
  supervivientes     : seq 1566..1853
  reparto            : 0 en la primera mitad / 288 en la segunda
  VERDICTO           : descarta los MAS VIEJOS (conserva la cola: lo mas reciente)

==============================================================================
-- Estabilidad del broker de borde -----------------------------------
  murio durante el experimento              : no
  lineas 'Msg lost' en el log del bridge    : 0
  SIGSEGV registrados por NanoMQ (signal 11): 0
  estado final del contenedor de borde      : running exit=0

-- Cache SQLite de bridge (filas en mounted_file_path) ---------------
  con el enlace caido, antes del SIGKILL : ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
  con el broker muerto                   : ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
  tras el rearranque                     : ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
======================================================================
```

### Perfil `hardened` — sonda de persistencia (corte SIN corte de corriente)

El resultado clave del prototipo: 0 huecos.

```
==============================================================================
VEREDICTO  03-nanomq-mosquitto  perfil=hardened-persistencia
imagen de borde: emqx/nanomq:0.25.5-slim
generado: 2026-08-06 11:02:39+0000
==============================================================================

-- Totales ------------------------------------------------------------
  intentos de publicacion (simulador)      : 1000
  aceptados por el broker de borde (PUBACK): 1000
  nunca entregados a paho (broker caido)   : 0
  llegados al hub (mensajes, con repetidos): 1010
  llegados al hub (seq unicos)             : 1000
  tasa de entrega sobre lo aceptado        : 100.00 %
  cadencia configurada                     : 20.0 msg/s durante 50.0 s (qos=1)

-- Por fase (sobre lo aceptado por el borde) --------------------------
  fase                        intentos  aceptados  llegados  perdidos
  antes del corte                  113        113       113         0
  corte                            887        887       887         0
  restauracion                       0          0         0         0

-- Huecos ------------------------------------------------------------
  seq aceptados que nunca llegaron: 0

-- Duplicados --------------------------------------------------------
  seq llegados mas de una vez : 6
  copias extra totales        : 10
  mensajes con flag MQTT DUP  : 0
  ejemplos: [114, 146, 147, 148, 149, 814]

-- Desorden ----------------------------------------------------------
  llegadas fuera de secuencia : 585
  mayor salto hacia atras     : 669 seq

-- Comportamiento al desbordar ---------------------------------------
  mensajes aceptados durante el corte : 887
  de ellos, llegaron al hub           : 887
  sobrevivio todo el corte: la cola no se desbordo

==============================================================================
-- Contexto de la sonda ----------------------------------------------
  misma configuracion que el perfil hardened, sin docker kill
  estado del borde al terminar : running exit=0
  SIGSEGV                      : 0
  cache SQLite al terminar     : ficheros=['mqtt_client.db', 'mqtt_client.db-shm', 'mqtt_client.db-wal'] filas={'t_client_msg': 0, 'sqlite_sequence': 1, 't_client_offline_msg': 0, 't_client_info': 1}
======================================================================
```

Los 10 duplicados y las 585 llegadas fuera de secuencia son el precio del at-least-once al drenar:
el atasco sale a la vez que el tráfico vivo. Cualquier consumidor de esto tiene que ser idempotente
por `(devID, Timestamp)` y no puede asumir orden de llegada.

### Sonda de desbordamiento (`hardened`, la de `defaults` da lo mismo)

```
-- Comportamiento al desbordar ---------------------------------------
  mensajes aceptados durante el corte : 200
  de ellos, llegaron al hub           : 40
  ventana del corte  : seq 1..200
  supervivientes     : seq 1..200
  reparto            : 32 en la primera mitad / 8 en la segunda
  VERDICTO           : descarta los MAS NUEVOS (conserva la cabeza: lo mas antiguo)

-- Huecos ------------------------------------------------------------
  seq aceptados que nunca llegaron: 160
  rangos (1):
    33-192                  160 msg   fase: corte

-- Limites usados en la sonda ----------------------------------------
  max_send_queue_len = 8
  disk_cache_size = 32
  flush_mem_threshold = 4
  estado del borde al terminar: running exit=0
```

---

## Qué respondió esto

### Preguntas de #4 que quedan cerradas

**5. ¿La persistencia de bridge de NanoMQ está realmente ausente del paquete por defecto?**
**Sí, y falla en silencio.** `emqx/nanomq:latest` no tiene ni un símbolo de SQLite (0 frente a 268
en `:0.25.5-slim`), acepta el bloque `bridges.mqtt.cache` sin rechistar, lo imprime en el log del
arranque igual que la imagen que sí lo soporta, y deja `mounted_file_path` vacío. Es el peor de los
tres desenlaces posibles.

**Y una que no estaba en la lista: activarla tampoco sirve.** Con la imagen correcta,
`t_client_msg` se queda en 0 filas durante todo el corte. Lo único que aguanta un corte en NanoMQ
0.25.5 es `max_send_queue_len`, en RAM, que un corte de luz se lleva por delante. Corroborado por
[nanomq/nanomq#1741](https://github.com/nanomq/nanomq/issues/1741), abierto desde abril de 2024 sin
respuesta del mantenedor.

**¿Funciona el bridge NanoMQ → Mosquitto?** Sí. Sin configuración especial en ninguno de los dos
lados, más allá del esquema `mqtt-tcp://` en la URL.

**¿Qué extremo se descarta al desbordar?** El más nuevo. La cola conserva la cabeza y tira lo que
llega. Al revés de lo que conviene aquí.

**¿Trae unidad `systemd`?** No, ni en las imágenes ni en el árbol de fuentes de la etiqueta
`0.25.5`.

**4 (parcialmente). ¿Cuánto se pierde en un corte de corriente?** Para NanoMQ: **todo lo encolado**,
porque nada llega al disco. No hay un `autosave_interval` que ajustar; no hay autosave.

### Lo que este prototipo NO responde

- **Las preguntas 1 y 2 de #4 sobre Mosquitto** (`max_queued_messages` en el bridge, y qué extremo
  descarta Mosquitto al llenarse). Aquí Mosquitto es el hub y está configurado a propósito con
  colas sin límite para no ser el cuello de botella. Eso es del prototipo
  [`02-mosquitto-mosquitto/`](../02-mosquitto-mosquitto/).
- **La pregunta 3 sobre NATS.** No aplica.
- **El techo real de `max_send_queue_len`.** Se confirmó que retiene al menos 887 mensajes con el
  valor en 4096; no se buscó dónde deja de hacerlo.
- **Si el fallo de la caché SQLite es un error de configuración nuestro.** Se probó con
  `flush_mem_threshold` a 100 y a 1, con `disk_cache_size` a 32 y a 102400, con
  `max_send_queue_len` a 8 y a 4096, con `clean_start` a `true` y a `false`. En todas las
  combinaciones `t_client_msg` se quedó en 0. No se descarta que exista una combinación que
  funcione, pero no está en la documentación y no la encontramos.
- **Consumo de recursos.** El argumento a favor de NanoMQ en #4 era su huella en la Raspberry Pi, y
  aquí no se midió nada de eso.

### Lectura para la decisión

Con lo medido, NanoMQ en el borde **no cumple el requisito que motivó la comparación**. Un
invernadero con enlaces que se caen necesita que el dato sobreviva al corte; aquí sobrevive solo si
además no se va la luz, que es justo la combinación que en el CIALE no se puede garantizar. Y el
perfil que alguien instalaría sin leer nada no pierde datos: tumba el broker.

Si aun así se quisiera, el mínimo innegociable sería: variante `-slim`/`-full`,
`max_send_queue_len` explícito y generoso (evita el SIGSEGV y es la única cola que existe de
verdad), directorio de `mounted_file_path` creado de antemano, y supervisión externa del proceso.
Aceptando que un corte de luz se lleva todo lo pendiente.
