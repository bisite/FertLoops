# Prototipo 01 — NATS nativo de extremo a extremo

**Código desechable.** Existe para responder con medidas la pregunta 3 de
[#4](https://github.com/bisite/FertLoops/issues/4): *¿qué pasa con lo publicado en un leaf node de
NATS mientras el enlace con el hub está caído?* La documentación de NATS no lo dice en ninguna
parte, así que hay que medirlo.

Aquí no hay MQTT en ningún punto: un `nats-server` en modo **leaf node** hace de borde (la
Raspberry Pi), un `nats-server` **hub** hace de VPS, y el simulador y el consumidor son clientes
Python con el cliente oficial `nats-py`.

## Topología

```
[simulator] --edge--> [nats-leaf] --link--> [nats-hub] --hub--> [consumer]
   Python              leaf node             hub            Python
```

Tres redes Docker separadas. Cortar el enlace es `docker network disconnect fl-proto-nats_link
fl-proto-nats-leaf`: el simulador sigue publicando contra el borde con total normalidad, igual que
una Raspberry Pi que sigue leyendo el ESP32 mientras la VPN está caída.

No se publica ningún puerto al host y no se instala nada fuera de contenedores, para que los tres
prototipos puedan correr a la vez. El nombre de proyecto es `fl-proto-nats`.

## Los dos perfiles

| Perfil | Borde | Hub |
| --- | --- | --- |
| `naive` | leaf node con NATS core, sin JetStream | hub sin JetStream, `publish` y ya |
| `jetstream-mirror` | JetStream con dominio propio `edge` + stream `FRAMES` en disco | JetStream con dominio `hub` + stream `FRAMES_MIRROR` que espeja a `FRAMES` |

`jetstream-mirror` es el único camino a la durabilidad en el borde que la documentación de NATS
describe. El espejo se configura con `mirror.external.api = "$JS.edge.API"`, que es lo que hace
que el hub mande sus peticiones de espejado al dominio del leaf a través del enlace leafnode.

## Cómo se corre

```sh
./run-experiment.sh naive
./run-experiment.sh jetstream-mirror
```

Un solo comando: construye, levanta, ejecuta las cinco fases (P0 warmup 10 s, P1 corte 60 s, P2
`SIGKILL` del borde 20 s, P3 restauración ≤ 90 s, P4 verificación), imprime el veredicto y hace
`docker compose down -v`.

Variable opcional, para medir el desbordamiento:

```sh
LEAF_MAX_MSGS=1000 ./run-experiment.sh jetstream-mirror
```

Limita el stream del borde a 1000 mensajes, el mismo tope que Mosquitto trae por defecto, para que
el número salga comparable con el de los prototipos hermanos. Por defecto es `-1` (sin tope).

## Qué se midió y con qué

- Ejecutado el **2026-08-04** sobre Docker 29.6.2.
- `nats:2.14.4-alpine` (`sha256:f2123f533c2b0cada0a5c5ec434fb2b8cfe1cf220215ef9d7517e1372917ad66`),
  `python:3.13.14-slim-trixie`, `nats-py==2.15.0`.
- Trama real del ESP32 (`docs/trama-de-datos-riego.md`) con un campo `seq` añadido sólo para el
  experimento: **347 bytes** de JSON por trama, publicada a 20 msg/s sobre
  `fertloops.A4CF12345678.reading`.

---

## Resultado 1: `naive` — se pierde todo y nadie se entera

```
========================================================================
 FertLoops prototype 01 / native NATS  |  profile = naive
========================================================================
 PHASES
   P0  t+    0.0s .. t+   10.1s  (10.1s)
   P1  t+   10.1s .. t+   70.3s  (60.2s)
   P2  t+   70.3s .. t+   90.7s  (20.4s)
   P3  t+   90.7s .. t+  180.0s  (89.4s)

 PUBLISHER (edge simulator)
   frames generated                    : 3622  (seq 1..3622)
   frames the publisher considers sent : 3622
   frames the publisher knows failed   : 0
   individual publish attempts errored : 0
   client connection events            : {'error': 22, 'disconnected': 2, 'reconnected': 1}
   max in-process outbox lag           : 0.0s
   frames still in the outbox at the end: 0

 CONSUMER (hub)
   frames received (raw)               : 1983
   distinct seq received               : 1983

 RESTORE (from the moment the link came back)
   first arrival after reconnect       : +1.4s (seq 1862)
   frames delivered after reconnect    : 1761
   backlog frames recovered            : 0 (nothing published during the outage came back)

 LOSS
   seq sent but never received         : 1639  (45.25% of sent)
   gap ranges                          : 1
     seq 223-1861  (1639 frames)  generated in P1+P2+P3

 DUPLICATES
   seq received more than once         : 0
   seq received but never published    : 0
   acks flagged duplicate by the server: 0
   frames that needed a retry          : 0

 ORDERING
   arrivals out of sequence            : 0
   largest backwards jump              : 0 seq

 OVERFLOW BEHAVIOUR
   classification                      : no-queue / total-loss (nothing published during the outage survived)
   outage window                       : seq 223..1834 (1612 frames)
   lost inside the window              : 1612
   lost as a leading run (oldest)      : 1612
   lost as a trailing run (newest)     : 1612

 PER PHASE
   phase     sent  pub_fail  received    lost   lost%
   P0         202         0       202       0    0.0%
   P1        1204         0         0    1204  100.0%
   P2         408         0         0     408  100.0%
   P3        1787         0      1760      27    1.5%
   pre         20         0        20       0    0.0%
   post         1         0         1       0    0.0%

 VERDICT
   LOSS: 1639 of 3622 accepted frames never reached the hub (45.25%).
   PER-FRAME FEEDBACK: none. No publish call ever failed, even for the 1639 frames that never arrived.
   CONNECTION-LEVEL FEEDBACK: {'error': 22, 'disconnected': 2, 'reconnected': 1} (says the broker link moved, says nothing about which frames were lost)
   DUPLICATES: none.
   ORDERING: preserved end to end.
   OVERFLOW: no-queue / total-loss (nothing published during the outage survived)
========================================================================
```

### Lectura

**Se pierde el 100 % de lo publicado durante el corte y el publicador no recibe ninguna señal.**
1612 tramas de las fases P1 y P2 no llegaron nunca, y sin embargo las 3622 llamadas a `publish`
devolvieron correctamente. El simulador incluso hace un `flush` explícito por cada trama —un
PING/PONG completo contra el leaf— y **ninguno falló**: el leaf confirma haber recibido cada byte
mientras tira el mensaje a la basura por no tener a quién entregárselo.

No es que el mensaje se encole y se descarte al llenarse una cola: es que **no hay cola**. Sin
interés registrado desde el hub, el leaf sencillamente no enruta el mensaje a ninguna parte. Por
eso el verificador lo clasifica como `no-queue / total-loss` y no como `discard-old` o
`discard-new`: la distinción de qué extremo se descarta no aplica cuando no se guarda nada.

Lo único que sí ve el publicador son eventos de conexión (`{'error': 22, 'disconnected': 2,
'reconnected': 1}`), y esos vienen sólo del `SIGKILL` de la fase P2 —de que el broker de borde se
cayó— no del corte del enlace. Durante los 60 s de P1 el cliente estuvo conectado y feliz todo el
rato.

**El leaf tarda unos 20 s en darse cuenta de que el enlace se cayó.** Corte a las `08:58:40Z`,
detección a las `08:59:00.21Z`:

```
[INF] Slow Consumer Detected: WriteDeadline of 10s exceeded with 1 chunks of 408 total bytes.
[INF] Leafnode connection closed: Slow Consumer (Write Deadline) - Remote: hub
```

No lo detecta el `ping_interval` (2 min por defecto, nunca llegó a agotarse) sino el *write
deadline* de 10 s al intentar escribir en un socket muerto. Durante esos ~20 s el leaf siguió
escribiendo tramas en una conexión TCP que ya no existía. En el log del servidor esos errores sí
aparecen; **en ninguna parte del log dice cuántos mensajes se perdieron**.

Las 27 tramas perdidas de P3 son la cola del corte: el enlace vuelve, pero el interés del
consumidor tarda 1,4 s en propagarse de nuevo hasta el leaf, y lo publicado en ese hueco se pierde
igual que lo anterior.

---

## Resultado 2: `jetstream-mirror` — cero pérdida

```
========================================================================
 FertLoops prototype 01 / native NATS  |  profile = jetstream-mirror
========================================================================
 PHASES
   P0  t+    0.0s .. t+   10.1s  (10.1s)
   P1  t+   10.1s .. t+   70.3s  (60.2s)
   P2  t+   70.3s .. t+   90.7s  (20.4s)
   P3  t+   90.7s .. t+  181.2s  (90.5s)

 PUBLISHER (edge simulator)
   frames generated                    : 3645  (seq 1..3645)
   frames the publisher considers sent : 3645
   frames the publisher knows failed   : 0
   individual publish attempts errored : 8
   client connection events            : {'error': 22, 'disconnected': 2, 'reconnected': 1}
   max in-process outbox lag           : 5.6s
   frames still in the outbox at the end: 0

 CONSUMER (hub)
   frames received (raw)               : 3645
   distinct seq received               : 3645

 RESTORE (from the moment the link came back)
   first arrival after reconnect       : +26.4s (seq 223)
   frames delivered after reconnect    : 3423
   backlog frames recovered            : 1612
   backlog drained in                  : 0.0s (32857 msg/s)
   caught up at                        : +26.4s after reconnect

 LOSS
   seq sent but never received         : 0  (0.00% of sent)
   gap ranges                          : 0

 DUPLICATES
   seq received more than once         : 0
   seq received but never published    : 0
   acks flagged duplicate by the server: 1
   frames that needed a retry          : 1

 ORDERING
   arrivals out of sequence            : 0
   largest backwards jump              : 0 seq

 OVERFLOW BEHAVIOUR
   classification                      : no-overflow
   all 1612 frames generated during P1+P2 arrived at the hub
   edge stream peak messages held : 3638
   edge stream first_seq reached   : 1
   edge stream last_seq reached    : 3638
   first_seq stayed at 1 => the edge stream never hit its cap, nothing dropped at the edge
   mirror max reported lag        : 0 msgs
   mirror max idle (source quiet) : 106.7 s
   mirror errors reported         : 0

 PER PHASE
   phase     sent  pub_fail  received    lost   lost%
   P0         202         0       202       0    0.0%
   P1        1204         0      1204       0    0.0%
   P2         408         0       408       0    0.0%
   P3        1809         0      1809       0    0.0%
   pre         20         0        20       0    0.0%
   post         2         0         2       0    0.0%

 VERDICT
   NO LOSS: every frame the publisher accepted reached the hub.
   PER-FRAME FEEDBACK: 8 publish attempts errored, but every frame was eventually placed.
   CONNECTION-LEVEL FEEDBACK: {'error': 22, 'disconnected': 2, 'reconnected': 1} (says the broker link moved, says nothing about which frames were lost)
   DUPLICATES: none.
   ORDERING: preserved end to end.
   OVERFLOW: no-overflow
========================================================================
```

### Lectura

**3645 publicadas, 3645 recibidas, cero huecos, cero duplicados, orden intacto.** El camino
JetStream-en-el-borde + mirror-en-el-hub sobrevive al corte de enlace (P1), al `SIGKILL` del broker
de borde (P2) y drena entero al restaurarse (P3).

**Sobrevive al `SIGKILL`.** El log del leaf al arrancar de nuevo:

```
[INF]   Starting restore for stream '$G > FRAMES'
[INF]   Restored 1,426 messages for stream '$G > FRAMES' in 2ms
```

Nada quedó pendiente de escribir: el `SIGKILL` no costó ni una trama.

**El publicador sí recibe señal cuando algo falla.** Los 5 s en que el broker de borde estuvo
muerto produjeron 8 intentos de `publish` fallidos, todos sobre la misma trama, que se reintentó
hasta colocarse. Es el contraste exacto con el perfil `naive`: aquí el agente de borde *sabe* que
no ha podido colocar la trama y puede actuar; allí no se entera nunca.

**La deduplicación por `Nats-Msg-Id` se disparó una vez.** `acks flagged duplicate by the server: 1`
sobre la única trama que necesitó reintento: el leaf ya la tenía en disco desde antes del `SIGKILL`,
la restauró al arrancar, y rechazó la copia del reintento devolviendo `duplicate: true`. El
consumidor recibió 0 duplicados. Es una sola observación, no una prueba estadística, pero es
evidencia directa de que el mecanismo funciona incluso cruzando un reinicio del broker.

**El espejo tarda unos 26 s en reanudarse.** Este es el número operativamente incómodo. El enlace
leafnode se restablece en ~1,5 s, pero la primera trama no llega al hub hasta **26,4 s** después
(26,3 s en la otra ejecución con espejo, ver más abajo). En ese momento las 1612 tramas del corte
llegan **de golpe**, en menos de una décima de segundo. O sea: el espejo no drena poco a poco,
espera, se reengancha y vuelca todo el backlog. Para 1612 tramas eso es instantáneo; para un
backlog de horas habría que volver a medirlo, porque este experimento no lo cubre.

### Cuidado con lo que P2 prueba de verdad

`docker kill` manda `SIGKILL`: mata el proceso sin darle ocasión de cerrar limpiamente, que es
justo lo que hace falta para no medir un apagado ordenado. Pero **no vacía la caché de página del
núcleo del host**: todo lo que JetStream ya había pasado a `write()` sigue estando ahí y el kernel
lo termina escribiendo en disco. Un corte de alimentación real de la Raspberry Pi sí perdería lo
que estuviera en esa caché sin `fsync`.

Así que lo que este prototipo demuestra es que **JetStream con `storage: file` no pierde nada
cuando el proceso muere**, no que no pierda nada cuando se va la luz. Para eso haría falta medir
`sync_interval` con un corte de energía real o un `dm-flakey`, y no se ha hecho.

---

## Resultado 3: `jetstream-mirror` con el stream del borde limitado a 1000 mensajes

Misma configuración, con `LEAF_MAX_MSGS=1000`, para responder qué hace NATS al desbordarse la cola
del borde y para que el número sea comparable con el tope por defecto de Mosquitto.

```
========================================================================
 FertLoops prototype 01 / native NATS  |  profile = jetstream-mirror
========================================================================
 PHASES
   P0  t+    0.0s .. t+   10.1s  (10.1s)
   P1  t+   10.1s .. t+   70.3s  (60.2s)
   P2  t+   70.3s .. t+   90.7s  (20.4s)
   P3  t+   90.7s .. t+  180.2s  (89.5s)

 PUBLISHER (edge simulator)
   frames generated                    : 3625  (seq 1..3625)
   frames the publisher considers sent : 3625
   frames the publisher knows failed   : 0
   individual publish attempts errored : 8
   client connection events            : {'error': 22, 'disconnected': 2, 'reconnected': 1}
   max in-process outbox lag           : 5.6s
   frames still in the outbox at the end: 0

 CONSUMER (hub)
   frames received (raw)               : 2487
   distinct seq received               : 2487

 RESTORE (from the moment the link came back)
   first arrival after reconnect       : +26.3s (seq 1361)
   frames delivered after reconnect    : 2265
   backlog frames recovered            : 474
   backlog drained in                  : 0.0s (27679 msg/s)
   caught up at                        : +26.3s after reconnect

 LOSS
   seq sent but never received         : 1138  (31.39% of sent)
   gap ranges                          : 1
     seq 223-1360  (1138 frames)  generated in P1

 DUPLICATES
   seq received more than once         : 0
   seq received but never published    : 0
   acks flagged duplicate by the server: 1
   frames that needed a retry          : 1

 ORDERING
   arrivals out of sequence            : 0
   largest backwards jump              : 0 seq

 OVERFLOW BEHAVIOUR
   classification                      : discard-old (the oldest queued frames are dropped)
   outage window                       : seq 223..1834 (1612 frames)
   lost inside the window              : 1138
   lost as a leading run (oldest)      : 1138
   lost as a trailing run (newest)     : 0
   edge stream peak messages held : 1000
   edge stream first_seq reached   : 2619
   edge stream last_seq reached    : 3618
   first_seq left 1 and reached 2619 => the edge stream hit its cap and dropped its OLDEST messages (discard: old)
   mirror max reported lag        : 0 msgs
   mirror max idle (source quiet) : 106.7 s
   mirror errors reported         : 0

 PER PHASE
   phase     sent  pub_fail  received    lost   lost%
   P0         202         0       202       0    0.0%
   P1        1204         0        66    1138   94.5%
   P2         408         0       408       0    0.0%
   P3        1790         0      1790       0    0.0%
   pre         20         0        20       0    0.0%
   post         1         0         1       0    0.0%

 VERDICT
   LOSS: 1138 of 3625 accepted frames never reached the hub (31.39%).
   PER-FRAME FEEDBACK: 8 publish attempts errored, but every frame was eventually placed.
   CONNECTION-LEVEL FEEDBACK: {'error': 22, 'disconnected': 2, 'reconnected': 1} (says the broker link moved, says nothing about which frames were lost)
   DUPLICATES: none.
   ORDERING: preserved end to end.
   OVERFLOW: discard-old (the oldest queued frames are dropped)
========================================================================
```

### Lectura

**NATS descarta lo más viejo.** Las 1138 tramas perdidas forman una tirada limpia por el extremo
antiguo (`lost as a leading run: 1138`, `lost as a trailing run: 0`), y la evidencia directa está
en el propio stream: se quedó clavado en 1000 mensajes mientras su `first_seq` subía de 1 a 2619.
Es la política `discard: old`, que es el valor por defecto de JetStream y que aquí se declara
explícitamente en `consumer.py`. Con esta configuración se conserva el presente y se pierde el
histórico.

Después del `SIGKILL` el leaf restauró exactamente el tope:

```
[INF]   Restored 1,000 messages for stream '$G > FRAMES' in 3ms
```

**El retardo de reanudación del espejo también cuesta backlog.** De las 1612 tramas del corte sólo
se recuperaron 474, no 1000. La razón es que durante los 26,3 s que el espejo tarda en reengancharse
el borde sigue recibiendo tramas nuevas, que van empujando a las viejas fuera de la ventana de 1000.
Es decir: con un tope por mensajes, **la latencia de reenganche se paga en histórico perdido**, y
no sólo la duración del corte.

**Traducción a cadencia real.** El tope se cuenta en mensajes, no en tiempo, así que 1000 mensajes
son ~2,8 h de corte con una sola mesa de drenaje (1 muestra / 10 s) o ~42 min con cuatro mesas.
Sin tope y con `storage: file`, el límite pasa a ser el disco del borde: a 347 bytes por trama, una
mesa genera unos 3 MB al día.

---

## Qué respondió esto

De las cinco preguntas abiertas de [#4](https://github.com/bisite/FertLoops/issues/4):

**3. ¿Qué pasa con lo publicado en un leaf node de NATS mientras el enlace con el hub está caído?
— RESPONDIDA.**

Con NATS core (perfil `naive`) **desaparece en silencio**. No se bloquea, no da error, no se encola:
el leaf acepta el mensaje, confirma la escritura al cliente hasta el nivel de `flush`, y lo tira
porque no hay interés registrado. Medido: 1612 de 1612 tramas perdidas durante el corte, con cero
llamadas a `publish` fallidas. El log del servidor deja constancia de que el enlace se cayó, pero
nunca de cuántos mensajes se perdieron. Además, el leaf tarda ~20 s en enterarse siquiera de que el
enlace no está, porque lo detecta el *write deadline* de 10 s, no el `ping_interval`.

Con JetStream en el borde y espejo en el hub (perfil `jetstream-mirror`) **no se pierde nada**,
siempre que el stream del borde tenga sitio: 3645 de 3645, sin duplicados y en orden. Es el único
camino documentado a la durabilidad en el borde y **funciona**.

**2. ¿Qué se descarta al llenarse la cola, lo nuevo o lo más viejo? — RESPONDIDA para NATS.**

`discard: old`, que es el valor por defecto de JetStream: se conserva el presente y se pierde el
histórico. Medido con el tope en 1000 mensajes: 1138 tramas perdidas, todas por el extremo antiguo,
con el `first_seq` del stream subiendo de 1 a 2619 mientras el total se quedaba en 1000. La pregunta
original iba sobre Mosquitto; para Mosquitto la responden los prototipos hermanos.

**4. ¿Cuánto se pierde en un corte de corriente? — PARCIALMENTE.**

JetStream con `storage: file` no perdió **ni una trama** al recibir `SIGKILL` (restauró 1426 y 1000
mensajes en las dos ejecuciones). Pero, como se explica arriba, `SIGKILL` mata el proceso y no la
caché del núcleo: esto demuestra que no hay pérdida cuando el proceso muere, no que no la haya
cuando se va la luz. La pregunta original es sobre `autosave_interval` de Mosquitto, que es un
mecanismo distinto —Mosquitto sí mantiene su base en memoria entre autoguardados— y la responden
los prototipos hermanos.

**1 (`max_queued_messages` del bridge) y 5 (persistencia de bridge en NanoMQ) — NO APLICAN.**
Son preguntas sobre MQTT y aquí no hay MQTT en ninguna parte.

## Qué dejó sin responder, y lo que abrió nuevo

- **El reenganche del espejo tarda ~26 s** (26,4 s y 26,3 s en las dos ejecuciones con espejo) desde
  que el enlace vuelve hasta que llega la primera trama. No se ha investigado de dónde sale ese
  valor ni si es configurable. Para un invernadero probablemente da igual; para control en lazo
  cerrado no.
- **El drenaje de backlogs grandes no está medido.** 1612 tramas se volcaron en menos de una décima
  de segundo. Un corte de horas es otro orden de magnitud y habría que medirlo aparte.
- **La pérdida por corte de alimentación real** (no `SIGKILL`) sigue sin medir, y con ella el efecto
  de `sync_interval` de JetStream.
- **JetStream en el borde no cubre que el broker de borde esté caído.** Durante los 5 s de P2 las
  publicaciones fallaron; lo que salvó esas tramas fue el *outbox* en memoria del propio simulador
  (5,6 s de retraso máximo, ninguna trama abandonada). Es decir: el agente de la Raspberry Pi
  necesita su propia cola de salida además de JetStream, y este experimento no dice qué pasa si el
  agente también se reinicia en ese momento.
- **La deduplicación se observó una sola vez.** Funcionó, y funcionó cruzando un reinicio del
  broker, pero una observación no es una medida.

## Ficheros

| Fichero | Qué es |
| --- | --- |
| `docker-compose.yml` | Los dos servidores, los tres clientes, las tres redes. Sin puertos al host. |
| `nats/leaf-naive.conf`, `nats/hub-naive.conf` | Perfil `naive`: NATS core, todo por defecto. |
| `nats/leaf-jetstream.conf`, `nats/hub-jetstream.conf` | Perfil `jetstream-mirror`: dominios `edge` y `hub`. |
| `client/frame.py` | La trama del ESP32 con el campo `seq` del experimento. |
| `client/simulator.py` | Agente de borde: genera a 20 msg/s y publica en orden. |
| `client/consumer.py` | Ingesta del hub; también crea los dos streams (el del borde, entre dominios). |
| `client/verifier.py` | Compara publicado contra recibido e imprime el veredicto. |
| `run-experiment.sh` | Las cinco fases en un comando. |
