---
status: accepted
---

# MQTT con Mosquitto como transporte entre el Gateway y el VPS

El transporte entre el Gateway y el VPS es **MQTT con un broker Mosquitto local en el Gateway y un `bridge` hacia un Mosquitto en el VPS**, con persistencia en disco, sin tope de cola, y con el camino de vuelta de los comandos como una segunda línea `topic ... in` del mismo bridge. Se eligió porque, medido a la escala real del piloto —12 mesas de drenaje publicando una lectura por minuto al VPS, con un objetivo de 15 días de corte sin perder una muestra—, no falla ninguno de los cuatro umbrales fijados de antemano para preferir la alternativa (NATS con JetStream). El criterio, acordado antes de medir: **Mosquitto gana salvo que falle un umbral**, precisamente para no dejar que un resultado bonito mueva la meta después de verlo.

## Escala del experimento

15 días de corte a 12 mesas × 1 lectura/60 s × 347 B/trama = **259.200 mensajes** (objetivo real); 30 días = 518.400 (margen). Dos rondas de medición en `prototypes/transport/`: la primera (`6618232`) confirmó el comportamiento al desbordar a escala de segundos; la segunda (`00fe82a`) llena ambos transportes hasta el backlog real y mide RSS, drenaje, reinicio y comandos.

## Los cuatro umbrales, medidos a escala real

| Umbral | Mosquitto (medido) | Resultado |
| --- | --- | --- |
| RSS proyectada a 15 días < 1.024 MB (mitad de una Raspberry Pi de 2 GB) | 243 MB (pendiente medida: 912 B/mensaje encolado; 267,6 MB medidos directamente a 291.800 mensajes) | **PASA** |
| Cero pérdida al drenar un backlog completo con tráfico en vivo compitiendo | 0 de 531.859 mensajes de backlog, drenados en 34,3 s a 15.515 msg/s | **PASA** |
| Arranque operable con una base de persistencia grande | 258 MB restaurados en 1,5 s | **PASA** |
| Los comandos emitidos con el Gateway inalcanzable llegan, una vez y en orden | 60/60, sin duplicados, QoS 1 de punta a punta | **PASA** |

## Opciones consideradas

- **NATS con JetStream** (leaf node en el Gateway con dominio propio + stream espejado al VPS). Es la única alternativa que sobrevivió a la primera ronda de investigación y prototipado; MQTT con NanoMQ quedó descartado ahí por evidencia (su persistencia de bridge no guarda nada ni con la imagen correcta, y sin afinar se cae solo). A escala real, **JetStream mide incluso mejor que Mosquitto en varios ejes**: 0 % de pérdida exacto (519.364/519.364, frente al 0,0064 % de Mosquitto), RSS plana entre 47 y 70 MB con independencia del tamaño del backlog —la persistencia vive en el fichero, no en memoria—, y un reinicio del leaf en 1,07 s con 212,5 MB de *file store*. La reanudación del espejo tras la reconexión tardó 24 s con un backlog real de 518.857 mensajes, **casi lo mismo que los ~26 s que la primera ronda midió con un backlog de solo 1.000** — evidencia de que ese retraso es un coste de reconexión aproximadamente constante, no proporcional al backlog. Con el criterio pactado de antemano esto no cambia la elección: Mosquitto no falló ningún umbral, así que gana por ser la opción con menos software propio que explicar a quien rote. Lo que si cambia es la confianza en el descarte: no es una alternativa peor, es una alternativa **más cara de operar y enseñar** para un margen que este piloto no necesita. El camino de vuelta de los comandos no se probó en NATS porque exigiría un segundo stream espejado en sentido inverso —exactamente el coste de topología que se estaba evaluando— y solo merecía construirse si Mosquitto fallaba su propio umbral 4.
- **MQTT con NanoMQ.** Descartado en la primera ronda (ver `docs/research/transporte-pi-vps.md` y `prototypes/transport/03-nanomq-mosquitto/`): sin afinar el broker se cae solo, y afinado, su caché de persistencia no guarda ni un mensaje pese a aceptar la configuración sin avisar.
- **HTTP con cola local, escritura directa a la base de datos, Telegraf leyendo el puerto serie.** Descartadas en la investigación original por obligar a escribir y mantener a mano precisamente la parte que falla en silencio (la cola), o por no poder leer el puerto serie del ESP32 en absoluto.

## Consecuencias

- **La configuración de producción no es la que midió el experimento.** El perfil `hardened` de la primera ronda usó `autosave_interval 5` a propósito, para poder *medir* la ventana de pérdida en segundos. A escala real ese valor tiene un coste que solo aparece con un backlog grande: Mosquitto reescribe el fichero de persistencia **entero** en cada volcado, así que durante un corte de 15 días con `autosave_interval 5` el broker de borde escribiría del orden de **16 TB** al SSD de la Raspberry Pi para proteger una ventana de pérdida de un único mensaje. La tabla siguiente es aritmética sobre el ritmo de crecimiento medido (486 B de fichero por mensaje encolado), no un nuevo experimento:

  | `autosave_interval` | Ventana de pérdida ante un corte de corriente | Escritura total en un corte de 15 días |
  | --- | --- | --- |
  | 5 s (perfil de medición de la ronda 1) | ~1 mensaje | ~16,3 TB |
  | 60 s | ~12 mensajes | ~1,4 TB |
  | **300 s (recomendado para producción)** | ~60 mensajes (5 min de lecturas de la flota) | ~272 GB |
  | 1800 s (default de Mosquitto) | ~360 mensajes (30 min) | ~45 GB |

  Se recomienda **`autosave_interval 300`** para el despliegue real: cinco minutos de exposición ante un corte de corriente es un precio razonable frente al riesgo de desgastar la SSD de la Raspberry Pi en el único escenario donde ese coste se paga —un corte de enlace largo, que es justo el que este transporte existe para sobrevivir.
- **`max_queued_messages 0` deja de dar miedo a esta cadencia.** El miedo original era una pregunta de OOM sin resolver; con la cadencia real (1 lectura/60 s, no 10 s) la cola crece a menos de 1 KB por mensaje y una Raspberry Pi de 2 GB aguanta muchos meses de corte antes de acercarse a agotar memoria. El comportamiento al agotarla de verdad sigue sin estar documentado ni medido — ver «Lo que sigue sin resolverse».
- **El camino de vuelta de los comandos es una segunda línea `topic`, no un mecanismo aparte:** `topic fertloops/+/cmd in 1` en el broker de borde, simétrica a la de las lecturas. La idempotencia del comando —que un reenvío de QoS 1 no dispare un riego duplicado— sigue sin resolver MQTT por sí solo y es diseño propio, tal y como ya señalaba la investigación original; corresponde al ticket del camino de control ([#10](https://github.com/bisite/FertLoops/issues/10)).
- **Quién consume en el VPS y escribe en TimescaleDB queda para el ticket [#9](https://github.com/bisite/FertLoops/issues/9)**, que este cierre desbloquea. La investigación original apuntaba a Telegraf con `mqtt_consumer` para ese extremo; esta decisión no lo confirma ni lo descarta.
- **`restart_timeout` acotado (`2 8` en vez del jitter por defecto de hasta 30 s).** Sin esto el bridge puede tardar decenas de segundos en darse cuenta de que el enlace volvió, lo que la primera ronda ya midió como pérdida real en el perfil `defaults`. Con la corrección, el bridge reconecta en segundos.

## Lo que sigue sin resolverse

- **Comportamiento real al agotar memoria.** `max_queued_messages 0` traslada el límite de "mensajes" a "memoria disponible"; no se ha medido qué hace Mosquitto al llegar ahí, solo se ha argumentado que a esta cadencia no debería ocurrir en los horizontes considerados.
- **Corte de corriente real (no SIGKILL), en ambas rondas.** `docker kill` no vacía la caché de página del host; la pregunta necesita hardware real y queda pendiente para cuando haya una Raspberry Pi disponible.
- **Desgaste de la tarjeta SD/SSD de la Raspberry Pi en producción**, con el `autosave_interval` finalmente elegido.
- **Si el retraso de ~24-26 s de reanudación del espejo de JetStream es realmente constante** o solo coincide en dos backlogs muy distintos por casualidad — irrelevante para esta decisión, pero digno de una nota si NATS se reconsidera en el futuro.
