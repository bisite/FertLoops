# El borde: la Raspberry Pi como portero del invernadero

Este documento describe el **borde** del sistema FertLoops: la Raspberry Pi que lee el
ESP32, guarda un histórico local, publica la telemetría por MQTT y recibe
comandos de control hacia los actuadores. Es el lado que se ejecuta *en el campo*,
frente al servidor (VPS) que vive *arriba*.

El documento se escribe para quien herede el sistema: explica qué hay montado, por qué se
tomó cada decisión, cómo operarlo, y —con la misma honestidad que el resto de la
documentación del proyecto— qué **no** está hecho y qué depende de decisiones aún abiertas.

## Qué resuelve el borde

El ESP32 (dispositivo *Slave*) habla un protocolo de petición/respuesta por línea serie: se
le envía `read\r\n` y responde una trama JSON con sensores, estado y errores (ver
`docs/trama-de-datos-riego.md`). El borde tiene cuatro responsabilidades:

1. **Leer** el ESP32 a intervalo fijo y corregir su marca de tiempo (el RTC del ESP viene
   desfasado).
2. **Custodiar** los datos localmente, en disco, de modo que un corte no los pierda.
3. **Publicar** la telemetría en un broker MQTT, para que el servidor la consuma.
4. **Recibir** comandos de control desde MQTT, traducirlos a la trama del ESP32, aplicarlos
   y confirmar el resultado — con las salvaguardas que el control de actuadores exige.

Las cuatro están implementadas hoy, con un matiz importante en la cuarta: el control físico
real (válvula, inversor) está **deliberadamente bloqueado** a la espera de las decisiones de
diseño del ticket [#10](https://github.com/bisite/FertLoops/issues/10). Solo se permiten
comandos que no mueven nada físico. La sección de seguridad lo detalla.

## Arquitectura del borde

```
                          RASPBERRY PI (el borde)
   ESP32  ──serie──►  ┌──────────────────────────────────────────┐
  (Slave)  ttyAMA0    │  gateway_riego.py  (portero único)        │
                      │     │                                     │
                      │     ├──► InfluxDB 2   (histórico local)   │
                      │     │        en el SSD NVMe de 2 TB       │
                      │     │                                     │
                      │     └──► Mosquitto    (broker MQTT local) │
                      │              127.0.0.1:1883               │
                      └──────────────────────────────────────────┘
                                        │
                                   (MQTT, futuro bridge)
                                        ▼
                              SERVIDOR / VPS (arriba)
                          PostgreSQL + TimescaleDB, Grafana
```

Los tres componentes de software —InfluxDB, Mosquitto y el gateway— corren como **servicios
`systemd`**, de modo que arrancan solos al encender la Pi y se recuperan si se caen. El borde
es autónomo: tras un reinicio, se pone a funcionar sin intervención.

### Hardware

| Elemento | Detalle |
| --- | --- |
| Placa | Raspberry Pi 5, 16 GB RAM |
| Almacenamiento | SSD NVMe de 2 TB (HAT M.2 sobre PCIe) |
| Sistema operativo | Raspberry Pi OS (Debian Trixie), 64-bit, **en el SSD** |
| Arranque | Por NVMe, **sin microSD** |
| Enlace con el ESP32 | Serie por `/dev/ttyAMA0` a 9600 baudios |

El requisito de «sistema operativo en el SSD y la Pi sin SD» está cumplido y verificado:
`findmnt /` devuelve `/dev/nvme0n1p2` como raíz, y `lsblk` no muestra ningún `mmcblk`
(microSD). El histórico local (InfluxDB) vive en ese mismo SSD, que es lo que cubre el hueco
de datos cuando el enlace con el servidor falla.

## El histórico local: InfluxDB 2

**Decisión: InfluxDB 2.x como almacén de series temporales del borde.**

El histórico local guarda cada lectura del ESP32 de forma permanente (retención infinita).
Es la **red de seguridad** del borde: pase lo que pase con el enlace o el broker, el dato
queda en disco en el momento de leerlo.

Detalles de la instalación:

| Parámetro | Valor |
| --- | --- |
| Producto | InfluxDB OSS 2.9.1 |
| Organización | `riego` |
| Bucket | `riego_data` |
| Retención | infinita |
| Escucha | `localhost:8086` |
| Servicio | `influxdb.service` (systemd, arranca solo) |

Dentro del bucket hay dos *measurements*:

- **`lecturas`** — una fila por lectura del ESP32, con los sensores, el estado de control y
  los errores como *fields*, y `gateway_id`/`device_id` como *tags*.
- **`comandos`** — auditoría del flujo de bajada: una fila por cada comando recibido (ver la
  sección de auditoría).

**Por qué InfluxDB 2 y no otra cosa.** La decisión inicial barajaba SQLite; se cambió a
InfluxDB por indicación del equipo. Sobre la versión, se eligió la 2.x frente a la 1.x (sin
desarrollo activo) y frente a la 3.x (más pesada, y con la limitación de rango de consulta
que el informe de [#3](https://github.com/bisite/FertLoops/issues/3) documenta para InfluxDB
3 Core). Conviene una aclaración honesta que evita una confusión frecuente: **el servidor
usa TimescaleDB, no InfluxDB, y eso es correcto y deliberado.** Son dos roles distintos —el
borde custodia y amortigua, el servidor analiza campañas completas— y se comunican por MQTT
(mensajes JSON), no por replicación de bases de datos, así que cada lado usa la herramienta
que le conviene. Que el informe de [#3](https://github.com/bisite/FertLoops/issues/3)
descartara InfluxDB *para el servidor* no aplica al borde: descartó InfluxDB **3** por
necesidades de análisis (consultas de meses, *joins* con eventos) que el borde no tiene.

**Nota de seguridad sobre credenciales.** El gateway lee su token de InfluxDB desde
`~/.riego_influx_token` (permisos `600`). El token actual tiene permisos de **lectura y
escritura** sobre el bucket `riego_data`: escritura para guardar lecturas y auditar
comandos, y lectura para recargar la idempotencia al arrancar (ver más abajo). Un token
anterior era de solo escritura, lo que rompía la recarga de idempotencia con un `404`
silencioso — de ahí el cambio.

## El broker MQTT: Mosquitto

**Decisión: Mosquitto como broker MQTT del borde. NanoMQ queda descartado.**

Mosquitto recibe la telemetría que publica el gateway y entrega los comandos de bajada. Ahora
escucha **solo en `127.0.0.1:1883`** (localhost), que es el valor por defecto de Mosquitto
2.x y el más seguro: el broker no está expuesto a la red. Para el funcionamiento actual
(gateway y broker en la misma Pi) es suficiente.

| Parámetro | Valor |
| --- | --- |
| Producto | Eclipse Mosquitto 2.0.21 |
| Escucha | `127.0.0.1:1883` (solo localhost) |
| QoS | 1 en publicación y suscripción |
| Servicio | `mosquitto.service` (systemd, arranca solo) |

**Por qué Mosquitto y no NanoMQ.** El borde llegó a montarse sobre NanoMQ, pero el equipo
descartó NanoMQ con firmeza tras los prototipos del ticket
[#4](https://github.com/bisite/FertLoops/issues/4). Los hallazgos medidos, resumidos:

- La persistencia de bridge de NanoMQ (caché SQLite) **no guarda nada** ni con la imagen
  correcta: la tabla `t_client_msg` se quedó en 0 filas con 1211 mensajes encolados. Coincide
  con [nanomq#1741](https://github.com/nanomq/nanomq/issues/1741), abierto desde abril de
  2024.
- Sin `max_send_queue_len`, el broker de NanoMQ **se cae** (SIGSEGV) al perder el enlace del
  bridge.
- NanoMQ no trae unidad `systemd`, mientras que el paquete Debian de Mosquitto sí — ventaja
  directa para un borde que debe arrancar solo.

La migración de NanoMQ a Mosquitto fue **transparente para el gateway**: ambos hablan MQTT
estándar en `localhost:1883`, así que no hubo que tocar el código de publicación (el esquema
`mqtt-tcp://` que NanoMQ exigía era cosa de *su* bridge, no de un cliente `paho`).

**Sobre la configuración `hardened` de los prototipos.** El informe de
[#4](https://github.com/bisite/FertLoops/issues/4) insiste en cinco valores por defecto de
Mosquitto que hay que corregir para no perder datos en un corte. La mayoría de esos valores
—`max_queued_messages`, `queue_qos0_messages`, QoS del bridge, `autosave_interval` para la
cola del bridge— gobiernan el **bridge** hacia el servidor. **El borde no tiene bridge
todavía** (no hay servidor), así que esa configuración se aplicará cuando el bridge exista, no
antes. Configurarla ahora sería ajustar parámetros de una pieza que no está montada. La
fiabilidad del borde hoy no descansa en la persistencia del broker, sino en el histórico
local (InfluxDB en el SSD), como recomienda el propio informe.

## El gateway: portero único

`gateway_riego.py` es el corazón del borde. Se le llama «portero único» porque es el
**único proceso que habla con el puerto serie**, lo cual es un requisito, no una comodidad:
el campo `Volume` del ESP32 no es acumulativo —cuenta los litros entre lecturas y se resetea
en cada `read`—, así que dos lectores compitiendo por el puerto se robarían volumen entre
ellos. Un solo portero lo evita.

Corre como `riego-gateway.service` (systemd). El usuario `riego` pertenece al grupo
`dialout`, que le da acceso al puerto serie sin privilegios adicionales.

### Flujo de subida

Cada `INTERVALO` segundos (hoy **600 s = 10 minutos**), el gateway:

1. Envía `read\r\n` al ESP32 y parsea la respuesta JSON.
2. **Corrige el `Timestamp`** con la hora real de la Pi. El RTC del ESP32 está desfasado
   (envía fechas como `30/07/2026` cuando el reloj real es otro), así que el gateway lo
   sobrescribe con la hora del sistema.
3. Escribe la lectura en InfluxDB (`measurement` `lecturas`).
4. Publica la trama JSON completa en MQTT, QoS 1, en un **único topic**:
   `fertloops/v1/{gateway_id}/{device_id}/up/readings`.

La decisión de publicar **un único topic con todo el JSON** (frente a un topic por magnitud)
es deliberada y acordada con el equipo: el servidor se «mete desde el suscriptor» y desempaqueta
el JSON al otro lado.

### Flujo de bajada

El gateway se suscribe a `fertloops/v1/{gateway_id}/+/down/commands`. Al recibir un comando,
lo procesa en varias etapas —descritas en la sección de seguridad— y, si procede, lo escribe
al ESP32 y publica el resultado en `fertloops/v1/{gateway_id}/{device_id}/up/command_result`.

El puerto serie está protegido por un **candado (`threading.Lock`)**: la lectura periódica y
la escritura de comandos no pueden solaparse, de modo que una orden que llega en mitad de una
lectura espera su turno en lugar de corromper ambas.

### Configuración interna (parámetros clave)

| Parámetro | Valor actual | Notas |
| --- | --- | --- |
| `INTERVALO` | `600` s | intervalo de lectura (10 min) |
| `GATEWAY_ID` | `gw-pruebas` | **PROVISIONAL**, pendiente del valor real |
| `MQTT_HOST:PORT` | `localhost:1883` | broker local |
| `MQTT_QOS` | `1` | at-least-once |
| `CAMPOS_CONTROL_PERMITIDOS` | `{"Debug"}` | whitelist de seguridad (ver abajo) |
| `VENTANA_IDEMPOTENCIA_HORAS` | `1` | recarga de idempotencia al arrancar |

## Seguridad del flujo de bajada

El control de actuadores actúa sobre el mundo físico —válvula, bomba dosificadora, inversor—,
así que el flujo de bajada está construido con varias capas de defensa. Un comando entrante
pasa, **en este orden**, por:

1. **Idempotencia.** Cada comando trae un `command_id`. Si ya se ha visto, se ignora. Esto
   evita que un reenvío por red (QoS 1 es *at-least-once*) provoque un riego duplicado. La
   deduplicación es diseño propio, como advierte el informe de
   [#4](https://github.com/bisite/FertLoops/issues/4): MQTT no la da.
2. **Validación de rangos técnicos.** Se comprueba que cada valor esté dentro del rango que
   el ESP32 acepta (`Valve` 0–90, `Inv.Freq` 0–650, `Inv.On`/`Restart` 0/1, `Debug.<TAG>`
   0–5 con TAG existente). Es *defensa en profundidad*: el ESP32 ya rechaza fuera de rango,
   pero el gateway lo para antes de escribir nada.
3. **Whitelist de campos permitidos.** Aquí está la barrera principal: hoy
   `CAMPOS_CONTROL_PERMITIDOS = {"Debug"}`. **Cualquier comando que toque `Valve`, `Inv`,
   `Restart` o `Timestamp` se rechaza.** Solo se permiten cambios de nivel de log (`Debug`),
   que no mueven nada físico.

Un comando de válvula con valor válido (p. ej. `Valve: 45`) se rechaza igualmente en la etapa
3, porque `Valve` no está en la whitelist. Un comando de válvula con valor imposible (p. ej.
`Valve: 200`) se rechaza antes, en la etapa 2. Ambos casos quedan auditados.

### Por qué el control físico está bloqueado

La whitelist en `{"Debug"}` es **temporal y deliberada**. Habilitar el control real exige
antes las decisiones del ticket [#10](https://github.com/bisite/FertLoops/issues/10), que no
son de implementación sino de diseño y responsabilidad, y no corresponden al borde en
solitario:

- **Modos de autoridad** (shadow / supervised / autonomous): cuánta libertad tiene el sistema
  para actuar solo.
- **Interlocks y límites duros**: tiempo máximo de riego, mínimo entre riegos, comportamiento
  ante sensores en fallo. Requieren conocimiento agronómico del cultivo y la instalación.
- **Modo supervised**: cómo se pide confirmación humana y qué pasa si nadie contesta.
- **Convivencia** con el riego manual y el programador existente.

Solo cuando esas decisiones estén tomadas e implementadas se ampliará la whitelist más allá
de `Debug`. La validación de rangos (etapa 2) ya está preparada para ese momento; la barrera
que falta abrir es la política de seguridad, no el mecanismo.

## Auditoría e idempotencia persistente

Cada comando de bajada —**se ejecute, se rechace por seguridad o llegue mal formado**— se
registra en InfluxDB, en el `measurement` `comandos`:

| Campo | Tipo | Contenido |
| --- | --- | --- |
| `gateway_id` | tag | identificador del gateway |
| `device_id` | tag | MAC del ESP32 destino |
| `result` | tag | `Ok` / `Invalid` / `Rechazado` / `ErrorFormato` |
| `command_id` | field | identificador del comando |
| `control` | field | la trama de control pedida (JSON como texto) |
| `raw` | field | respuesta cruda del ESP32 |
| `motivo` | field | por qué se rechazó (si aplica) |

El `command_id` se guarda como *field* y no como *tag* a propósito: es de alta cardinalidad
(un valor distinto por comando), y en InfluxDB los tags de alta cardinalidad degradan el
rendimiento.

Esta auditoría cumple lo que pide el ticket
[#10](https://github.com/bisite/FertLoops/issues/10) («reconstruir después por qué se regó en
un momento dado») y, de paso, da la **idempotencia persistente**: al arrancar, el gateway
recarga desde InfluxDB los `command_id` de la última hora
(`VENTANA_IDEMPOTENCIA_HORAS`), de modo que un reinicio no le hace olvidar los comandos
recientes. La ventana es corta a propósito: un reenvío accidental por red ocurre en segundos
o minutos, no horas, así que una hora sobra para el propósito de deduplicación. La auditoría,
en cambio, es permanente: guardar un comando no es lo mismo que impedir repetirlo. Reabrir la
válvula mañana es un comando **nuevo** (con `command_id` nuevo) y nunca queda bloqueado por la
idempotencia.

## Operación

Los tres servicios se gestionan con `systemctl`. Comandos habituales:

```sh
# Estado y logs del gateway
systemctl status riego-gateway
journalctl -u riego-gateway -f

# Parar / arrancar / reiniciar el gateway
sudo systemctl restart riego-gateway

# Los otros dos servicios del borde
systemctl status mosquitto
systemctl status influxdb
```

**Aplicar un cambio en el código del gateway.** Editar `gateway_riego.py` no basta: el
proceso en marcha sigue con la versión cargada en memoria. Hay que reiniciar el servicio:

```sh
sudo systemctl restart riego-gateway
```

Si se cambia el **archivo del servicio** (`/etc/systemd/system/riego-gateway.service`) en
lugar del código, hay que recargar systemd antes: `sudo systemctl daemon-reload`.

**Nota sobre los logs.** El servicio fija `Environment=PYTHONUNBUFFERED=1`; sin ello, Python
retiene los `print` en un buffer y no aparecen en `journalctl` hasta acumularse.

**Probar el flujo de bajada en local** (comando de `Debug`, inofensivo):

```sh
mosquitto_pub -h localhost \
  -t "fertloops/v1/gw-pruebas/C8C9A3CB1BDC/down/commands" \
  -m '{"command_id":"prueba-1","control":{"Control":{"Debug":{"serial_protocol":5}}}}'

# Ver la respuesta que publica el gateway
mosquitto_sub -h localhost -t "fertloops/v1/gw-pruebas/+/up/command_result" -v
```

**Consultar la auditoría de comandos:**

```sh
influx query 'from(bucket:"riego_data") |> range(start:-24h)
  |> filter(fn:(r) => r._measurement == "comandos")'
```

### Ficheros del borde

| Ruta | Qué es |
| --- | --- |
| `~/riego/gateway_riego.py` | el gateway en producción |
| `~/riego/gateway_riego_v*_backup.py` | versiones anteriores, conservadas por prudencia |
| `~/.riego_influx_token` | token de InfluxDB (permisos `600`) |
| `/etc/systemd/system/riego-gateway.service` | unidad systemd del gateway |

## Estado del borde: qué está hecho

- Sistema operativo en el SSD NVMe, arranque sin microSD. **Verificado.**
- InfluxDB 2 como histórico local en el SSD, servicio autónomo.
- Mosquitto como broker MQTT local, servicio autónomo (NanoMQ descartado y desinstalado).
- Gateway como servicio autónomo, con:
  - subida (lectura, corrección de `Timestamp`, escritura en InfluxDB, publicación MQTT);
  - bajada (recepción de comandos, escritura al ESP32, publicación del resultado);
  - candado del puerto serie compartido entre subida y bajada;
  - seguridad en capas (idempotencia → rangos → whitelist);
  - auditoría de todos los comandos en InfluxDB;
  - idempotencia persistente entre reinicios.

Todo lo anterior está probado de punta a punta.

## Estado del borde: qué falta y de qué depende

**Trabajo técnico del borde que puede hacerse sin dependencias externas:**

- **Cola de salida del gateway** para reenviar al servidor lo acumulado durante un corte de
  enlace. Su lógica puede montarse, pero no puede probarse de verdad sin servidor al que
  reenviar; conviene hacerla cuando el bridge exista. El informe de
  [#4](https://github.com/bisite/FertLoops/issues/4) es tajante: la fiabilidad ante cortes la
  da el agente del borde con su propia cola, no el broker.

**Bloqueado por decisiones del equipo o por piezas que aún no existen:**

- **Contrato de bajada Plataforma↔Raspberry**: los topics y el formato del flujo de bajada
  (`down/commands`, `up/command_result`, el sobre con `command_id`) son una **propuesta
  provisional** de este borde. Hay que acordarlos. La sección «Trama Plataforma ←→ RASPBERRY»
  de `docs/trama-de-datos-riego.md` está por escribir.
- **Acceso del suscriptor**: hoy el broker escucha solo en localhost. Falta saber desde dónde
  se conectará el consumidor del servidor —en la propia Pi, en la red, o vía bridge— para
  decidir si se abre el broker o se monta el bridge.
- **`gateway_id` real**: hoy es `gw-pruebas`.
- **Bridge al servidor** y su configuración `hardened`: cuando el servidor exista.
- **Control físico de actuadores** (ticket
  [#10](https://github.com/bisite/FertLoops/issues/10)): modos de autoridad, interlocks,
  límites de riego, convivencia con el riego manual. Hasta que el equipo lo decida, la
  whitelist se queda en `Debug`.

## Referencias

- `docs/trama-de-datos-riego.md` — protocolo ESP32 ↔ Raspberry (tramas de datos y de control).
- Ticket [#3](https://github.com/bisite/FertLoops/issues/3) — almacén de series temporales del
  servidor (PostgreSQL + TimescaleDB).
- Ticket [#4](https://github.com/bisite/FertLoops/issues/4) — transporte de datos Pi ↔ VPS
  (Mosquitto, y descarte de NanoMQ y NATS).
- Ticket [#10](https://github.com/bisite/FertLoops/issues/10) — camino de control y sus
  salvaguardas.
