# Prototipo 02: Mosquitto (borde) --bridge--> Mosquitto (hub)

**Código desechable.** Existe para responder con medidas las preguntas 1, 2 y 4 de
[#4](https://github.com/bisite/FertLoops/issues/4). El protocolo compartido con los otros dos
prototipos está en [`../README.md`](../README.md).

## Qué prueba

Un Mosquitto 2.1.2 en el borde (la Raspberry Pi) con un `connection` bridge hacia un Mosquitto
2.1.2 en el hub (la VPS). Un simulador en Python publica la trama real del ESP32 a 20 msg/s
contra el broker de borde; un consumidor con sesión durable (`clean_session false`, QoS 1) la
recoge en el hub. Se corta el enlace, se mata el borde con SIGKILL, se restaura y se cuenta.

La pregunta central es la que el informe de #4 marcó como **deducida pero no documentada**:

> ¿`max_queued_messages` gobierna de verdad la cola de salida del **bridge**, y al desbordarse
> se descarta lo más nuevo o lo más viejo?

## Cómo se corre

```sh
./run-experiment.sh defaults    # config mínima que sale de un tutorial de bridge
./run-experiment.sh hardened    # la corrección que propone el informe de #4
```

Un solo comando: construye, levanta, ejecuta las cinco fases (incluidos el
`docker network disconnect`/`connect` y el `docker kill` de P2), imprime el veredicto y limpia con
`docker compose down -v`. No publica ningún puerto al host y el nombre de proyecto es
`fl-proto-mosq`, para poder correr a la vez que los prototipos hermanos.

Hay además dos perfiles de diagnóstico, que existen sólo para aislar el tope de cola:

```sh
./run-experiment.sh queue-probe       # = hardened, pero sin tocar max_queued_messages (default 1000)
./run-experiment.sh queue-probe-50    # = queue-probe, pero con max_queued_messages 50
SKIP_P2_KILL=1 ./run-experiment.sh queue-probe   # pasada de control: mismo guion sin el SIGKILL
```

`SKIP_P2_KILL=1` sirve para leer el desbordamiento sin que el SIGKILL pueda quitar mensajes de la
cola por otra vía. Todo lo demás del guion es idéntico.

## Resultados medidos

Cinco ejecuciones reales, en este orden, el 2026-08-06. Ninguna cifra de este documento está
estimada: todas salen del bloque de veredicto que imprime `run-experiment.sh`.

| Ejecución | Aceptados por el borde | Llegados al hub | Perdidos | Duplicados | Desorden |
| --- | ---: | ---: | ---: | ---: | ---: |
| `defaults` | 2123 | 428 | **1695 (79,84 %)** | 0 | 0 |
| `hardened` | 2124 | 2112 | **12 (0,56 %)** | 0 | 0 |
| `queue-probe` (tope 1000) | 2124 | 1246 | 878 (41,34 %) | 0 | 0 |
| `queue-probe` sin SIGKILL | 2215 | 1350 | 865 | 0 | 0 |
| `queue-probe-50` sin SIGKILL | 2215 | 395 | 1820 | 0 | 0 |

"Aceptados" son los mensajes con PUBACK del broker de borde, no los que el simulador intentó
mandar: es el conjunto del que el broker se hizo responsable, y por tanto el denominador honesto
de la pérdida. Los frames que el simulador no pudo entregar porque el broker de borde estaba
muerto (141 en cada pasada con SIGKILL) se contabilizan aparte, porque son un problema del origen
y no del transporte.

**Cero duplicados y cero desórdenes en las cinco pasadas.** Con QoS 1 en el bridge y en la
suscripción, el drenaje de 1194 mensajes tras la restauración llegó completo y en orden.

### La respuesta a la pregunta 1: sí, `max_queued_messages` gobierna la cola del bridge

Dos pasadas idénticas salvo por una línea de configuración:

| `max_queued_messages` en el borde | Mensajes de P1 que sobrevivieron al corte |
| --- | ---: |
| sin poner (default, 1000) | **1040** |
| `50` | **90** |

`1040 − 90 = 950 = 1000 − 50`. **El tope se traslada uno a uno a la profundidad de la cola del
bridge**: eso es lo medido, y es la respuesta a la pregunta.

Queda además un desplazamiento constante de **+40** sobre el valor configurado, en las dos
pasadas. La explicación que encaja es `2 × max_inflight_messages` (default 20): la documentación
dice que el tope cuenta "por encima de los que están actualmente en vuelo", y en un bridge hay
dos saltos con ventana propia — del broker local al cliente local del bridge, y del bridge al
hub. **Eso es una inferencia, no una medida:** no se ha variado `max_inflight_messages` para
comprobarlo. Lo que sí está medido es que el desplazamiento es constante e igual a 40 con dos
topes muy distintos, así que a efectos de dimensionado se puede contar como
`max_queued_messages + 40`.

El log del propio Mosquitto nombra al cliente afectado, y el nombre es el del **cliente local del
bridge**, no el de un suscriptor cualquiera:

```
1785834597: Outgoing messages are being dropped for client fl-edge-bridge-local.
```

(`fl-edge-bridge-local` es el `local_clientid` que fija `config/queue-probe/edge.conf`.)

La correlación temporal cierra el argumento. Mismo corte de 60 s a 20 msg/s, distinto tope:

| Tope | Corte del enlace | Primera línea de descarte | Retardo | Profundidad medida |
| --- | --- | --- | ---: | ---: |
| 1000 | 11:09:05 | 11:09:57 | **52 s** | 1040 mensajes |
| 50 | 11:12:06 | 11:12:11 | **5 s** | 90 mensajes |

Al bajar el tope de 1000 a 50, el instante en que Mosquitto empieza a tirar mensajes se adelanta
de 52 s a 5 s dentro del mismo corte. A 20 msg/s, 52 s son exactamente los 1040 mensajes contados
por el verificador; el caso de 5 s sólo acota (el log tiene resolución de un segundo), y la cifra
buena es la del verificador, 90. No hay deducción posible salvo que el tope es el que manda.

**Aviso operativo:** Mosquitto emite esa línea **una sola vez por cliente y por arranque del
broker**, no una por mensaje descartado. En la pasada `queue-probe-50` esa única línea representa
1115 tramas perdidas; en la pasada completa de `queue-probe` salen dos líneas, pero sólo porque el
SIGKILL de P2 reinició el proceso y con él el contador. Vigilar el log dice *que* se está
perdiendo, nunca *cuánto*.

### La respuesta a la pregunta 2: al desbordar se descarta lo MÁS NUEVO

En las tres pasadas que llegaron a desbordar, lo que sobrevivió fue siempre un **prefijo exacto**
de la ventana de corte, nunca un sufijo:

| Ejecución | Aceptados en P1 | Sobreviven | Prefijo intacto | Sufijo intacto | Primer hueco |
| --- | ---: | ---: | ---: | ---: | ---: |
| `queue-probe` (1000) | 1205 | 1040 | 1040 | 0 | posición 1041 |
| `queue-probe` sin SIGKILL | 1205 | 1040 | 1040 | 0 | posición 1041 |
| `queue-probe-50` | 1205 | 90 | 90 | 0 | posición 91 |

Es decir: **la cola se llena y se cierra**. Los mensajes que ya estaban dentro se conservan y se
entregan intactos al reponerse el enlace; los que llegan después se tiran. Para un invernadero
eso significa que un corte largo deja el **principio** del corte y pierde el **presente** — justo
al revés de lo que uno querría si tuviera que elegir, porque el dato reciente es el que sirve para
decidir un riego.

Y tiene una consecuencia peor, que se ve en la pasada completa de `queue-probe`: una vez llena la
cola, **sigue llena**. En esa pasada se perdió el 100 % de P2 (310 tramas) y el 100 % de P3 (403
tramas), no por el corte sino porque el hueco no se liberó hasta que el bridge reconectó y drenó.
El tope no recorta el corte: lo prolonga más allá del corte.

### La respuesta a la pregunta 4: cuánto cuesta el SIGKILL

| Perfil | `persistence` | `autosave_interval` | Perdido del encolado previo al SIGKILL |
| --- | --- | --- | --- |
| `defaults` | `false` (default) | 1800 s (default, irrelevante) | 1204 tramas = **todo** |
| `hardened` | `true` | 5 s | 11 tramas = **los últimos 0,5 s** |
| `queue-probe` | `true` | 5 s | 0 adicionales sobre el control sin SIGKILL |

En `defaults` la comparación no es limpia, y hay que decirlo: con `persistence false` no había
nada que salvar, pero es que con QoS 0 en el bridge tampoco había cola. Las dos causas se
solapan y el resultado (pérdida total) es el mismo, así que esa pasada no separa una de otra.
Quien sí lo separa es `hardened`: con la cola llena de 1194 mensajes y persistencia activa,
el SIGKILL sólo se llevó las 11 tramas de los **0,5 s** transcurridos desde el último volcado a
disco. Lo que fija ese techo es `autosave_interval`: se midieron 24 líneas
`Saving in-memory database to /mosquitto/data//mosquitto.db.` a lo largo de la pasada, una cada
~6 s, y el peor caso es una ventana entera de `autosave_interval`. Con el default de **1800 s**
esa ventana sería de media hora de cola.

`queue-probe` da el contraste más limpio de todos: la pasada con SIGKILL y la pasada de control
sin SIGKILL perdieron **exactamente las mismas 165 tramas de P1**. Es decir, el SIGKILL costó
cero: la cola había dejado de crecer al tocar el tope mucho antes de la muerte del proceso, así
que todo lo que quedaba dentro ya estaba volcado a disco.

### Hallazgo no buscado: tras el reinicio, el bridge no resuelve el nombre del hub

Con el enlace caído, el broker de borde reiniciado no puede ni resolver `hub-broker` (en
producción, el nombre de la VPS a través de la VPN), y entra en backoff exponencial:

```
1785834243: Warning: Error resolving bridge address: Name does not resolve.
1785834243: Error creating bridge: Name does not resolve.
1785834243: Bridge to-hub next backoff will be 9024 ms
1785834253: Connecting bridge to-hub (hub-broker:1883)
1785834253: Error creating bridge: Name does not resolve.
1785834253: Bridge to-hub next backoff will be 15527 ms
```

El `restart_timeout` por defecto es jitter con base 5 s y tope 30 s, así que **al volver el
enlace el bridge puede tardar decenas de segundos en darse cuenta**. Se midieron reconexiones a
t+9,1 s, t+9,9 s, t+15,0 s, t+15,1 s y t+20,6 s de la restauración.

Eso es inofensivo si la cola funciona (en `hardened` llegaron los 404 frames de P3, encolados
mientras el bridge dormía) y demoledor si no: en `defaults` se perdieron **181 de 403** tramas de
P3 — publicadas con el enlace ya restaurado — sólo porque el bridge todavía estaba en backoff y
QoS 0 no encola. Si esa ventana molesta, hay que poner `restart_timeout` explícitamente; no se ha
tocado aquí para no ensuciar la comparación con los perfiles hermanos.

### Por qué `defaults` pierde el 100 % del corte

No es el tope de cola: es que **no hay cola**. La línea `topic fertloops/# out` sin nivel de QoS
deja el bridge en QoS 0, y `queue_qos0_messages` es `false` por defecto, así que el broker no
encola nada para un cliente desconectado. Se confirma de dos maneras:

- La profundidad de cola observada del bridge fue **0 mensajes** (ninguna trama de P1 llegó tras
  la restauración; el drenaje no existió).
- El QoS de entrega observado en el hub fue **`[0]`**, pese a que el consumidor se suscribió a
  QoS 1 y el simulador publicó a QoS 1. Mosquitto no sube el QoS de salida
  (`upgrade_outgoing_qos` es `false` por defecto), así que el QoS del bridge degrada el camino
  entero de extremo a extremo.

Ojo con la implicación inversa, que es la trampa fácil: poner el bridge a QoS 1 **no basta** si
quien publica lo hace a QoS 0, por la misma razón. Para ese caso está `queue_qos0_messages true`,
que el perfil `hardened` incluye.

Un detalle menor pero real: en `defaults` el cliente local del bridge se llamó
`local.8b01db739b65.to-hub`, con el hostname embebido. En un contenedor recreado ese nombre
cambia y la sesión persistida ya no se reengancha. Por eso `hardened` fija `clientid` y
`local_clientid` explícitamente.

### Traducción a horas de corte

Los topes de cola se cuentan en mensajes, no en tiempo. Aplicando la aritmética del protocolo a
la profundidad medida de **1040 mensajes** con el tope por defecto:

- **2,9 horas** de corte con una sola mesa de drenaje (1 muestra / 10 s), o
- **43 minutos** con cuatro mesas.

Con `max_queued_messages 0` (perfil `hardened`) no hay tope: el límite pasa a ser la RAM y el
disco. La trama medida ocupa **348 bytes** de media en el hub, y los 1205 mensajes de P1 ocuparon
unos **409 KiB** de cola.

## Salida cruda

### `./run-experiment.sh defaults`

```
==============================================================================
VEREDICTO -- perfil `defaults`  |  Mosquitto borde --bridge--> Mosquitto hub
==============================================================================

Fases (reloj del simulador, t0 = arranque)
  t+   0.0s  PW pre-warmup   (esperando al bridge, no se mide)          seq 1..86 (86 frames)
  t+   4.3s  P0 warmup       (enlace arriba)                            seq 87..292 (206 frames)
  t+  14.6s  P1 corte        (enlace caido)                             seq 293..1496 (1204 frames)
  t+  74.8s  P2 corte de luz (SIGKILL del broker de borde, enlace caido) seq 1497..1947 (451 frames)
  t+  97.4s  P3 restauracion (enlace arriba, drenando)                  seq 1948..2350 (403 frames)
  t+ 117.5s  stop                                                       (sin frames)

Totales (excluida la ventana PW de pre-warmup: 86 frames descartados del calculo)
  Producidos por la fuente ......................   2264
    aceptados (PUBACK) por el broker de borde ...   2123
    rechazados (broker de borde inalcanzable) ...    141
  Recibidos en el hub (entregas) ................    428
    seq unicos ...................................    428
  Perdidos (aceptados y nunca llegados) .........   1695   (79.84% de lo aceptado)
  Duplicados (entregas de un seq ya visto) ......      0
  Fuera de orden (seq < maximo ya visto) ........      0

Perdidas por fase de publicacion
  P0: aceptados   206  perdidos     0 (0.00%)  rechazados en origen     0
  P1: aceptados  1204  perdidos  1204 (100.00%)  rechazados en origen     0
  P2: aceptados   310  perdidos   310 (100.00%)  rechazados en origen   141
  P3: aceptados   403  perdidos   181 (44.91%)  rechazados en origen     0

Rangos de huecos (2 rangos)
  seq 293..1497  (1205 msgs, fase P1+P2)
  seq 1639..2128  (490 msgs, fase P2+P3)

Reparto temporal de las llegadas de P1
  llegaron ANTES de restaurar el enlace (fuga del corte) ...      0
  llegaron DESPUES de restaurar (drenaje real de la cola) .      0
  -> profundidad de cola observada del bridge: 0 mensajes
  entrega en vivo reanudada a t+9.1s de la restauracion (222/403 frames de P3 llegaron)

Desbordamiento de cola (solo fase P1: enlace caido, broker de borde vivo)
  aceptados durante P1 .........   1204
  sobrevivieron ................      0
  -> no se encolo NADA de P1: no es desbordamiento, es que la cola del bridge nunca existio para este perfil

Coste del SIGKILL (fase P2)
  aceptados antes del SIGKILL (P0+P1) ..........   1410
  de esos, perdidos ............................   1204
  rechazados en origen mientras el broker estaba
    muerto (la fuente no tenia donde dejarlos) .    141
  la perdida es la COLA contigua de la cola pre-SIGKILL:
    seq 293..1496, es decir los ultimos 60.2 s antes del SIGKILL

Tamano medio de la trama recibida: 347 bytes (los 1204 msgs de P1 ~= 409 KiB de cola en el borde)
QoS de entrega observado en el hub: [0]   entregas con flag DUP: 0

------------------------------------------------------------------------------
RESUMEN  perfil=defaults  aceptados=2123  llegados=428  perdidos=1695  duplicados=0  desorden=0
RESUMEN  desbordamiento: no se encolo NADA de P1: no es desbordamiento, es que la cola del bridge nunca existio para este perfil
------------------------------------------------------------------------------
```

Log del broker de borde en esa pasada (única línea de descarte de toda la ejecución):

```
1785834052: Outgoing messages are being dropped for client local.8b01db739b65.to-hub.
```

Es la misma línea que emite el desbordamiento de cola, pero aquí no hubo desbordamiento: la
profundidad de cola medida fue 0. Mosquitto usa el mismo aviso para el descarte de QoS 0 hacia un
cliente desconectado, así que **la línea por sí sola no distingue "se llenó la cola" de "no había
cola"**. Sin el contraste con los perfiles de diagnóstico no habría forma de saber cuál de las dos
cosas está pasando.

### `./run-experiment.sh hardened`

```
==============================================================================
VEREDICTO -- perfil `hardened`  |  Mosquitto borde --bridge--> Mosquitto hub
==============================================================================

Fases (reloj del simulador, t0 = arranque)
  t+   0.0s  PW pre-warmup   (esperando al bridge, no se mide)          seq 1..86 (86 frames)
  t+   4.3s  P0 warmup       (enlace arriba)                            seq 87..291 (205 frames)
  t+  14.6s  P1 corte        (enlace caido)                             seq 292..1496 (1205 frames)
  t+  74.8s  P2 corte de luz (SIGKILL del broker de borde, enlace caido) seq 1497..1947 (451 frames)
  t+  97.4s  P3 restauracion (enlace arriba, drenando)                  seq 1948..2351 (404 frames)
  t+ 117.6s  stop                                                       (sin frames)

Totales (excluida la ventana PW de pre-warmup: 86 frames descartados del calculo)
  Producidos por la fuente ......................   2265
    aceptados (PUBACK) por el broker de borde ...   2124
    rechazados (broker de borde inalcanzable) ...    141
  Recibidos en el hub (entregas) ................   2112
    seq unicos ...................................   2112
  Perdidos (aceptados y nunca llegados) .........     12   (0.56% de lo aceptado)
  Duplicados (entregas de un seq ya visto) ......      0
  Fuera de orden (seq < maximo ya visto) ........      0

Perdidas por fase de publicacion
  P0: aceptados   205  perdidos     0 (0.00%)  rechazados en origen     0
  P1: aceptados  1205  perdidos    11 (0.91%)  rechazados en origen     0
  P2: aceptados   310  perdidos     1 (0.32%)  rechazados en origen   141
  P3: aceptados   404  perdidos     0 (0.00%)  rechazados en origen     0

Rangos de huecos (1 rangos)
  seq 1486..1497  (12 msgs, fase P1+P2)

Reparto temporal de las llegadas de P1
  llegaron ANTES de restaurar el enlace (fuga del corte) ...      0
  llegaron DESPUES de restaurar (drenaje real de la cola) .   1194
    reconexion del bridge: primer mensaje drenado a t+9.9s de la restauracion; ultimo a t+9.9s
  -> profundidad de cola observada del bridge: 1194 mensajes
  entrega en vivo reanudada a t+9.9s de la restauracion (404/404 frames de P3 llegaron)

Desbordamiento de cola (solo fase P1: enlace caido, broker de borde vivo)
  aceptados durante P1 .........   1205
  sobrevivieron ................   1194
  prefijo intacto ..............   1194 mensajes
  sufijo intacto ...............      0 mensajes
  primer hueco en la posicion ..   1195 de 1205
  -> se conserva el PRINCIPIO de P1 (posiciones 1..1194) y se descartan los 11 MAS NUEVOS -> al desbordar se tira lo nuevo: se pierde el presente y se salva el historico
  -> de esos supervivientes, 1194 salieron de la cola del bridge tras la restauracion: ese es el tope efectivo medido

Coste del SIGKILL (fase P2)
  aceptados antes del SIGKILL (P0+P1) ..........   1410
  de esos, perdidos ............................     11
  rechazados en origen mientras el broker estaba
    muerto (la fuente no tenia donde dejarlos) .    141
  la perdida es la COLA contigua de la cola pre-SIGKILL:
    seq 1486..1496, es decir los ultimos 0.5 s antes del SIGKILL

Tamano medio de la trama recibida: 348 bytes (los 1205 msgs de P1 ~= 409 KiB de cola en el borde)
QoS de entrega observado en el hub: [1]   entregas con flag DUP: 0

------------------------------------------------------------------------------
RESUMEN  perfil=hardened  aceptados=2124  llegados=2112  perdidos=12  duplicados=0  desorden=0
RESUMEN  desbordamiento: se conserva el PRINCIPIO de P1 (posiciones 1..1194) y se descartan los 11 MAS NUEVOS -> al desbordar se tira lo nuevo: se pierde el presente y se salva el historico
------------------------------------------------------------------------------
```

El log de esta pasada **no tiene ninguna línea de descarte**. Lo que sí tiene son 24 volcados de
persistencia, uno cada ~6 s, que son los que acotan la pérdida por SIGKILL a 0,5 s:

```
1785834164: Saving in-memory database to /mosquitto/data//mosquitto.db.
1785834170: Saving in-memory database to /mosquitto/data//mosquitto.db.
1785834176: Saving in-memory database to /mosquitto/data//mosquitto.db.
```

Nota sobre el aviso de `hardened`: la etiqueta "se descartan los 11 MAS NUEVOS" que imprime el
verificador es su clasificación geométrica del hueco, no un desbordamiento. Con
`max_queued_messages 0` no hay tope; esas 11 tramas son las que el SIGKILL se llevó por estar
después del último volcado, y por eso caen justo al final de P1.

### Perfiles de diagnóstico

```
RESUMEN  perfil=queue-probe  aceptados=2124  llegados=1246  perdidos=878  duplicados=0  desorden=0
RESUMEN  desbordamiento: se conserva el PRINCIPIO de P1 (posiciones 1..1040) y se descartan los 165 MAS NUEVOS -> al desbordar se tira lo nuevo: se pierde el presente y se salva el historico

RESUMEN  perfil=queue-probe  aceptados=2215  llegados=1350  perdidos=865  duplicados=0  desorden=0
RESUMEN  desbordamiento: se conserva el PRINCIPIO de P1 (posiciones 1..1040) y se descartan los 165 MAS NUEVOS -> al desbordar se tira lo nuevo: se pierde el presente y se salva el historico

RESUMEN  perfil=queue-probe-50  aceptados=2215  llegados=395  perdidos=1820  duplicados=0  desorden=0
RESUMEN  desbordamiento: se conserva el PRINCIPIO de P1 (posiciones 1..90) y se descartan los 1115 MAS NUEVOS -> al desbordar se tira lo nuevo: se pierde el presente y se salva el historico
```

(La segunda y la tercera son las pasadas de control con `SKIP_P2_KILL=1`.)

## Qué respondió esto

**Respondidas.**

1. **¿`max_queued_messages` gobierna la cola del bridge?** Sí, y de forma directa. Cambiar sólo
   esa línea de 1000 a 50 movió la profundidad medida de la cola de 1040 a 90 mensajes
   (`1040 − 90 = 1000 − 50`) y adelantó el primer descarte de 52 s a 5 s dentro del mismo corte.
   El log de Mosquitto nombra explícitamente al cliente local del bridge. Queda además medido un
   desplazamiento constante de +40 mensajes sobre el valor configurado, que a efectos de
   dimensionado hay que sumar (su atribución a `2 × max_inflight_messages` encaja con la
   documentación pero no se ha comprobado variando ese parámetro).
2. **¿Qué se descarta al llenarse?** **Lo más nuevo.** En las tres pasadas que desbordaron, lo que
   sobrevivió fue un prefijo exacto de la ventana de corte y el sufijo intacto fue siempre 0. La
   cola se cierra al llenarse y no vuelve a admitir nada hasta que drena, así que el daño se
   extiende más allá del corte (100 % de P2 y de P3 perdidos en la pasada completa de
   `queue-probe`).
4. **¿Cuánto se pierde en un corte de corriente?** Exactamente la ventana de `autosave_interval`.
   Con `autosave_interval 5` el SIGKILL costó 11 tramas (0,5 s) de una cola de 1194. Con
   `persistence false` (el default) no hay nada que recuperar. El default de 1800 s convertiría
   esas 11 tramas en media hora de cola.

**No respondidas por este prototipo.** Las preguntas 3 (NATS leaf node) y 5 (persistencia de
bridge en NanoMQ) son de los prototipos [`01-nats-native/`](../01-nats-native/) y
[`03-nanomq-mosquitto/`](../03-nanomq-mosquitto/).

**No respondidas y que quizá habría que medir.**

- **Qué pasa al llenarse `max_queued_bytes`.** Aquí se dejó en 0 (sin límite) en todos los
  perfiles y el desbordamiento medido fue siempre por número de mensajes. Si el dimensionado real
  se hace por bytes, hace falta otra pasada.
- **Si el comportamiento se mantiene con colas grandes de verdad.** El corte más largo probado son
  1205 mensajes (~409 KiB). No se ha probado si un tope de, digamos, 500 000 mensajes se comporta
  igual o si la persistencia empieza a costar.
- **El coste del `autosave_interval` corto.** Se midieron 24 volcados en ~2 minutos sin efecto
  visible sobre la cadencia, pero esto corre en un contenedor con SSD, no en la SD de una
  Raspberry Pi. El desgaste de la tarjeta con un volcado cada 5 s es una pregunta abierta y
  probablemente la razón para subir ese valor en producción.
- **Aislar `queue_qos0_messages`.** El perfil `hardened` cambia a la vez el QoS del bridge y
  `queue_qos0_messages`, así que no separa cuál de los dos rescata a un publicador que use QoS 0.
  La documentación dice que `queue_qos0_messages` basta cuando el `topic` del bridge está a QoS
  ≥ 1, pero eso aquí no se ha medido.

## Qué hay en este directorio

| Fichero | Qué es |
| --- | --- |
| `docker-compose.yml` | Tres redes (`edge`/`link`/`hub`), sin puertos al host, proyecto `fl-proto-mosq` |
| `config/<perfil>/edge.conf` | Config del broker de borde; los comentarios dicen qué default corrige cada línea |
| `config/<perfil>/hub.conf` | Config del broker de hub |
| `Dockerfile` | Imagen común de simulador, consumidor y verificador (`paho-mqtt` sobre Python slim) |
| `src/frame.py` | La trama real del ESP32 más el campo `seq`, que sólo existe para el experimento |
| `src/simulator.py` | Publica a 20 msg/s y registra por separado lo intentado y lo que el broker confirmó |
| `src/consumer.py` | Sesión durable en el hub, QoS 1, registra el orden de llegada |
| `src/verifier.py` | Compara y emite el veredicto |
| `run-experiment.sh` | Las cinco fases, un solo comando |
