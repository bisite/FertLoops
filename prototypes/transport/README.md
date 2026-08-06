# Prototipos: ¿qué sobrevive de verdad a un corte de enlace?

**Código desechable.** Estos prototipos existen para responder una pregunta del ticket
[#4](https://github.com/bisite/FertLoops/issues/4) con evidencia empírica, no para convertirse
en el despliegue real. Nada de aquí se promociona a `master` tal cual: lo que se promociona es
la decisión y su ADR.

## La pregunta

La investigación de #4 recomendó MQTT con Mosquitto, pero dejó explícitamente sin confirmar por
documentación los puntos que más importan para un invernadero con enlaces que se caen:

1. ¿`max_queued_messages` gobierna la cola del **bridge** concretamente? (deducido, no documentado)
2. ¿Qué hace Mosquitto al llenarse la cola: descarta lo nuevo o lo más viejo?
3. ¿Qué pasa con lo publicado en un **leaf node** de NATS mientras el enlace con el hub está caído?
4. ¿Cuánto se pierde en un corte de corriente, dado que `autosave_interval` son 1800 s por defecto?
5. ¿La persistencia de bridge de NanoMQ está realmente ausente del paquete por defecto?

Ninguna se responde leyendo. Todas se responden midiendo.

## Los tres prototipos

| Directorio | Borde (Raspberry Pi) | Servidor (VPS) |
| --- | --- | --- |
| [`01-nats-native/`](01-nats-native/) | `nats-server` leaf node | `nats-server` hub |
| [`02-mosquitto-mosquitto/`](02-mosquitto-mosquitto/) | Mosquitto + bridge | Mosquitto |
| [`03-nanomq-mosquitto/`](03-nanomq-mosquitto/) | NanoMQ + bridge | Mosquitto |

Todo corre en Docker. **Nada se instala en la máquina de desarrollo** y **no se publica ningún
puerto al host**, para que los tres prototipos puedan correr a la vez sin pisarse.

Cada prototipo es autocontenido y duplica el simulador y el verificador en lugar de compartirlos.
Es deliberado: son desechables, y la duplicación evita que tocar uno rompa los otros.

## El protocolo del experimento

Idéntico en los tres, porque el objetivo es **comparar**, no demostrar. Cada prototipo se corre con
dos perfiles:

- **`defaults`** — la configuración mínima que obtiene quien instala el producto y lo arranca.
  Mide lo que se pierde por no saber lo que hay que saber.
- **`hardened`** — la configuración corregida que propone el informe de #4.
  Mide si esa corrección de verdad funciona.

En NATS los dos perfiles son `naive` (leaf node con NATS core, sin JetStream en el borde) y
`jetstream-mirror` (dominio JetStream propio en el leaf + stream espejado al hub), que es el único
camino a la durabilidad en el borde que la documentación describe.

### Topología de red

Tres redes Docker, para poder cortar el enlace sin tocar el borde:

```
[simulador] --edge--> [broker de borde] --link--> [broker de hub] --hub--> [consumidor]
```

Cortar el enlace es `docker network disconnect <proyecto>_link <broker-de-borde>`: el simulador
sigue publicando contra el broker de borde con normalidad, exactamente como una Raspberry Pi que
sigue leyendo el ESP32 mientras la VPN está caída. No hace falta `NET_ADMIN` ni `iptables`.

### Fases

| Fase | Duración | Qué pasa |
| --- | --- | --- |
| **P0 warmup** | 10 s | Enlace arriba. Confirma que el camino completo funciona antes de romperlo. |
| **P1 corte** | 60 s | `network disconnect`. El simulador sigue publicando. |
| **P2 corte de corriente** | 20 s | `docker kill` (SIGKILL, **no** `restart`) del broker de borde y arranque de nuevo, con el enlace todavía caído. |
| **P3 restauración** | ≤ 90 s | `network connect`. Se espera a que drene. |
| **P4 verificación** | — | Se compara lo publicado con lo llegado. |

**P2 usa `docker kill`, no `docker restart`, a propósito.** Un `restart` manda SIGTERM y el broker
escribe su fichero de persistencia al apagarse limpiamente — eso no prueba nada. Lo que hay que
medir es el corte de luz, y para eso hace falta SIGKILL. Es la prueba directa de `autosave_interval`.

### Cadencia y por qué está comprimida

El simulador publica a **20 mensajes/s**, no a la cadencia real de una mesa de drenaje (1 cada
10 s). Es deliberado: 60 s de corte a 20 msg/s son **1200 mensajes**, que cruzan el tope de 1000
que Mosquitto trae por defecto. A cadencia real habría que esperar tres horas para ver ese
desbordamiento.

Lo que hace la traducción honesta es que **los topes de cola se cuentan en mensajes, no en tiempo**.
Así que 1200 mensajes equivalen a:

- **3,3 horas** de corte con una sola mesa de drenaje (1 muestra / 10 s), o
- **50 minutos** con cuatro mesas.

Es decir: el número que sale del experimento se lee como "cuántas horas de corte aguanta esta
configuración", que es justo la pregunta de operación.

### La trama

La carga es la trama real del ESP32 (ver `docs/trama-de-datos-riego.md` y `CONTEXT.md`), con un
campo `seq` añadido **solo para el experimento**: un contador monótono que permite medir pérdida,
duplicados y desorden de forma exacta. Ese campo no existe en el protocolo real.

### Qué mide el verificador

- Publicados frente a recibidos (totales).
- **Huecos**: qué `seq` no llegaron nunca, agrupados en rangos y atribuidos a su fase.
- **Duplicados**: cuántos `seq` llegaron más de una vez (relevante para at-least-once).
- **Desorden**: cuántos llegaron fuera de secuencia al reponerse el enlace.
- **Comportamiento al desbordar**: si al llenarse la cola se descarta lo más nuevo o lo más viejo
  — que es la diferencia entre perder el histórico o perder el presente.

## Cómo se corren

Desde cada directorio de prototipo:

```sh
./run-experiment.sh defaults    # o: naive, en el prototipo de NATS
./run-experiment.sh hardened    # o: jetstream-mirror
```

Un solo comando: levanta, ejecuta las cinco fases, imprime el veredicto y limpia.

## Resultados

Medidos el 06/08/2026. El detalle y la salida cruda están en el `README.md` de cada prototipo.

| | Perfil por defecto | Perfil corregido |
| --- | --- | --- |
| **NATS nativo** | 45,25 % perdido, **en silencio** | **0 % perdido** |
| **Mosquitto → Mosquitto** | 79,84 % perdido | **0,56 % perdido** |
| **NanoMQ → Mosquitto** | el broker **muere** (SIGSEGV ×3) | 0 de 1211 recuperados del corte |

**No compares los totales absolutos entre prototipos.** Cada broker acepta de forma distinta —NanoMQ
por defecto solo llegó a confirmar 791 de 2400 publicaciones porque se caía a mitad—, así que lo
comparable son los porcentajes y el comportamiento, no los recuentos crudos.

### Lo que respondió cada pregunta abierta

**1. ¿`max_queued_messages` gobierna la cola del bridge? Sí, uno a uno.** Dos ejecuciones que se
diferencian en una sola línea de configuración: con tope 1000 sobrevivieron 1040 mensajes; con tope
50, exactamente 90. La diferencia entre supervivientes es idéntica a la diferencia entre topes. El
log nombra al cliente local del propio bridge: `Outgoing messages are being dropped for client
fl-edge-bridge-local`. Queda deducido, pero no aislado, un desfase constante de +40 sobre el valor
configurado, compatible con `2 × max_inflight_messages` por los dos saltos del bridge.

**2. ¿Qué se descarta al desbordar? Depende del producto, y son opuestos.**

- **Mosquitto y NanoMQ descartan lo más nuevo.** Los supervivientes son siempre un prefijo exacto de
  la ventana de corte. Se conserva el principio del corte y se pierde el presente. Peor aún en
  Mosquitto: la cola se cierra y **sigue cerrada**, así que el daño sobrevive al corte (en la prueba
  con tope costó el 100 % de P2 y el 100 % de P3).
- **NATS JetStream descarta lo más viejo** (`discard: old`), con evidencia directa: el stream clavado
  en 1000 mensajes mientras `first_seq` subía de 1 a 2619. Se conserva el presente y se pierde el
  histórico.

Para fertirrigación no es un detalle de implementación, es una elección: **perder el presente o
perder el histórico.**

**3. ¿Qué pasa en un leaf node de NATS con el enlace caído? Se pierde todo, sin avisar.** Las 3622
llamadas a `publish` tuvieron éxito, y el simulador hace `flush` explícito por trama —un PING/PONG
completo contra el leaf— sin que fallara ni uno. El leaf confirma cada byte y descarta el mensaje
porque no hay interés registrado: no hay cola, así que la pregunta de qué se descarta ni siquiera
aplica. Con JetStream en un dominio propio y stream espejado, cero pérdida. Es el único camino
documentado y funciona de verdad.

**4. ¿Cuánto cuesta un corte de corriente? Lo que diga `autosave_interval`.** Con Mosquitto
`hardened` (`autosave_interval 5`), el SIGKILL costó 11 tramas: los últimos 0,5 s. Con el valor por
defecto de 1800 s esa ventana pasa a ser media hora.

**Salvedad honesta:** `docker kill` no vacía la caché de página del host, así que estos resultados
demuestran «no se pierde nada cuando el proceso muere», no «no se pierde nada cuando se va la luz».
La segunda afirmación necesita hardware real.

**5. ¿Falta la persistencia SQLite en la imagen por defecto de NanoMQ? Sí, y falla en silencio.**
`emqx/nanomq:latest` tiene **0** símbolos `sqlite3_*` en su binario frente a **268** en
`:0.25.5-slim` (misma versión v0.25.5-6). Aun así acepta `bridges.mqtt.cache` y lo repite al arrancar
**igual que la imagen que sí lo soporta**, sin aviso y sin código de salida distinto de cero.

**Y un hallazgo que pesa más que el anterior: incluso con la imagen correcta, la caché no guarda
nada.** `t_client_msg` se quedó en **0 filas** con 1211 mensajes encolados, antes del kill, después
y tras reiniciar. Probado con `flush_mem_threshold` 1 y 100, `disk_cache_size` 32 y 102400,
`max_send_queue_len` 8 y 4096, y `clean_start` en ambos valores. Lo único que amortigua un corte es
`max_send_queue_len`, en RAM. Coincide con [nanomq#1741](https://github.com/nanomq/nanomq/issues/1741),
abierto desde abril de 2024.

### Hallazgos no buscados que cambian decisiones

- **Mosquitto `defaults` entrega a QoS 0 de punta a punta** aunque el publicador y el suscriptor sean
  ambos QoS 1: Mosquitto nunca eleva el QoS de salida, así que el QoS del bridge degrada todo el
  camino.
- **El aviso de desbordamiento de Mosquitto se escribe una vez por cliente y arranque, no por
  mensaje.** Una sola línea representó 1115 tramas perdidas. Y el perfil `defaults` emite **esa misma
  línea** con una cola medida de 0, así que la línea por sí sola no distingue «la cola se desbordó»
  de «no había cola».
- **NanoMQ por defecto no pierde mensajes: se cae.** Sin `max_send_queue_len` el broker recibe
  SIGSEGV ~1,7 s después de perder el remoto del bridge, tres veces en una ejecución. No es un
  artefacto de `docker network disconnect`: se reprodujo parando el hub con la interfaz intacta.
  Añadir `max_send_queue_len = 4096` por sí solo lo evita.
- **El espejo de JetStream tarda ~26 s en reanudarse** tras volver el enlace (26,4 s y 26,3 s en dos
  ejecuciones), aunque la conexión leaf se restablece en ~1,5 s. Con la cola llena esa latencia
  **cuesta backlog**, porque las tramas vivas siguen expulsando a las viejas durante esos 26 s.
- **El leaf de NATS tarda ~20 s en enterarse de que el enlace no está**, y lo detecta el plazo de
  escritura de 10 s (`Slow Consumer Detected: WriteDeadline of 10s exceeded`), no `ping_interval`.
- **NanoMQ → Mosquitto sí funciona** (`bridge_tcp_connect_cb: ... connected! RC [0]`), con la única
  pega de que la URL necesita el esquema `mqtt-tcp://`. Y **ninguna de las dos imágenes trae unidad
  `systemd`**, ni está en el árbol de fuentes en el tag `0.25.5`.
- **JetStream en el borde no cubre que se caiga el broker del borde.** Lo que salvó P2 en el
  prototipo de NATS fue el buffer en memoria del propio simulador. Sea cual sea el transporte, el
  agente de la Raspberry Pi necesita su propia cola de salida.

### Lo que sigue sin resolverse

- Comportamiento de `max_queued_bytes` al desbordar, y de Mosquitto con topes muy grandes.
- Desgaste de la tarjeta SD con `autosave_interval 5` en hardware real de Raspberry Pi.
- Aislar `queue_qos0_messages` del QoS del bridge: el perfil `hardened` cambia los dos a la vez.
- De dónde salen los ~26 s de reanudación del espejo de JetStream, y cómo se comporta con un backlog
  mucho mayor.
- Pérdida real ante corte de corriente (no SIGKILL) y el papel de `sync_interval`.
