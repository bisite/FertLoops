# Trama Plataforma ←→ RASPBERRY

> **Estado: PROPUESTA, pendiente de acuerdo con el equipo del servidor.**
>
> Esta sección describe el tramo entre la plataforma (servidor/VPS) y la Raspberry Pi, que
> hasta ahora estaba sin definir. Lo que sigue es la propuesta del borde, derivada de lo que
> el gateway ya implementa y de las recomendaciones del informe de transporte
> ([#4](https://github.com/bisite/FertLoops/issues/4)). **No es un contrato cerrado:** los
> topics, el formato del sobre y la política de confirmación deben acordarse con quien vaya a
> publicar los comandos y consumir la telemetría desde el servidor. Los puntos abiertos se
> señalan explícitamente.

## Marco general

Mientras que el documento anterior describe la comunicación **ESP32 ↔ Raspberry** (por línea
serie), esta sección describe la comunicación **Plataforma ↔ Raspberry** (por MQTT). Son dos
tramos distintos que el gateway de la Pi une:

```
   Plataforma  ──MQTT──►  Raspberry (gateway)  ──serie──►  ESP32
   (servidor)  ◄──MQTT──                       ◄──serie──
```

El transporte es **MQTT con QoS 1** (*at-least-once*). El broker recomendado es Mosquitto
(ver [#4](https://github.com/bisite/FertLoops/issues/4)). Hoy el broker del borde escucha solo
en `localhost`; cómo llega el servidor hasta él (bridge saliente desde la Pi) es uno de los 
**puntos abiertos** de más abajo.

## Convención de topics

Todos los topics cuelgan de un prefijo versionado, con el `gateway_id` y el `device_id` (la
MAC del ESP32, sin los dos puntos) como niveles:

```
fertloops/v1/{gateway_id}/{device_id}/{sentido}/{tipo}
```

| Topic | Sentido | Quién publica | Quién consume | Contenido |
| --- | --- | --- | --- | --- |
| `fertloops/v1/{gw}/{dev}/up/readings` | subida | Raspberry | servidor | telemetría (la trama del ESP32) |
| `fertloops/v1/{gw}/{dev}/down/commands` | bajada | servidor | Raspberry | comando de control |
| `fertloops/v1/{gw}/{dev}/up/command_result` | subida | Raspberry | servidor | resultado de un comando |

`up` es lo que sale de la Pi hacia el servidor; `down` es lo que baja del servidor hacia la
Pi. El resultado de un comando viaja como telemetría (`up`), porque es información que la Pi
reporta.

> **Punto abierto — `gateway_id`.** Hoy es `gw-pruebas` (provisional). Falta definir cómo se
> asigna el identificador real de cada gateway.

## Subida: telemetría (`up/readings`)

La Raspberry publica en `up/readings` la **trama de datos completa del ESP32** (la del inicio
de este documento), con una única modificación: el campo `Timestamp` se sobrescribe con la
hora real de la Pi (sincronizada por NTP), porque el RTC del ESP32 viene desfasado. El resto
de la trama se publica tal cual la entrega el ESP32.

Cadencia actual: una lectura cada **10 minutos** (configurable en el gateway).

> **Punto abierto — formato para el servidor.** El servidor almacena en TimescaleDB
> ([#3](https://github.com/bisite/FertLoops/issues/3)). Si el consumidor del servidor
> necesitara la telemetría en un formato distinto del JSON crudo del ESP32 (por ejemplo,
> aplanada o renombrada), habría que acordarlo. La propuesta del borde es publicar la trama
> tal cual, con el `Timestamp` corregido, y que el servidor la adapte al ingerir.

### Consideración de entrega (importante para el consumidor)

Con QoS 1, la telemetría puede llegar **duplicada** o **desordenada** tras una reconexión
—así lo midieron los prototipos de [#4](https://github.com/bisite/FertLoops/issues/4)—. El
consumidor del servidor debe ser **idempotente por `(devID, Timestamp)`** y no asumir orden
de llegada. Esto es un requisito del lado servidor, no del borde, pero se documenta aquí
porque nace del transporte.

## Bajada: comandos (`down/commands`)

El servidor publica en `down/commands` un **sobre JSON** con esta forma:

```json
{
  "command_id": "identificador-unico-del-comando",
  "control": { "Control": { "Debug": { "serial_protocol": 5 } } }
}
```

| Campo | Obligatorio | Descripción |
| --- | --- | --- |
| `command_id` | sí | Identificador único del comando. Se usa para idempotencia (ver abajo). |
| `control` | sí | La trama de control **tal como la entiende el ESP32** (ver la sección «Campos de control» de este documento). |

La decisión de que `control` lleve la trama del ESP32 directa (en lugar de un formato de alto
nivel que el gateway traduzca) es la **opción A**: el gateway actúa de portador y solo añade
las salvaguardas. Mantiene el contrato simple y reutiliza el formato de control ya definido
para el ESP32.

> **Punto abierto — origen y autenticación.** Quién puede publicar comandos, y cómo se
> garantiza que un comando es legítimo, depende de cómo se asegure el MQTT (usuarios, ACLs,
> TLS). Es parte del diseño del camino de control
> ([#10](https://github.com/bisite/FertLoops/issues/10)).

## Bajada: respuesta (`up/command_result`)

Tras procesar un comando, la Raspberry publica el resultado en `up/command_result`:

```json
{
  "command_id": "identificador-unico-del-comando",
  "result": "Ok",
  "raw": "Ok",
  "timestamp": "12/08/2026 09:02:35"
}
```

| Campo | Descripción |
| --- | --- |
| `command_id` | El mismo del comando, para que el servidor lo correle. |
| `result` | `Ok`, `Invalid`, `Rechazado` o `ErrorFormato` (ver tabla siguiente). |
| `raw` | Respuesta cruda del ESP32 (`Ok\r\n` / `Invalid command "..."`), o vacío si el comando no llegó a enviarse. |
| `timestamp` | Hora real de la Pi al procesar el comando. |

Valores de `result`:

| `result` | Significado |
| --- | --- |
| `Ok` | El ESP32 aceptó y aplicó el comando. |
| `Invalid` | El ESP32 rechazó el comando (respondió `Invalid command`). |
| `Rechazado` | El gateway rechazó el comando **antes** de enviarlo al ESP32 (por rango o por política de seguridad). |
| `ErrorFormato` | El comando llegó mal formado (no era JSON válido, faltaba `control`, etc.). |

## Idempotencia

`command_id` existe porque MQTT con QoS 1 es *at-least-once*: un comando puede reenviarse por
un problema de red. El gateway recuerda los `command_id` recientes y **no reejecuta** uno ya
visto. Esto evita que un reenvío accidental provoque, por ejemplo, un riego duplicado.

La idempotencia solo protege frente al **mismo `command_id` repetido**. Un comando **nuevo**
que pida lo mismo (p. ej. reabrir la válvula más tarde) lleva un `command_id` distinto y se
ejecuta con normalidad; nunca queda bloqueado.

> **Recomendación para el servidor.** Generar un `command_id` único por comando emitido (un
> UUID, o un contador con prefijo de origen). Reutilizar un `command_id` para una orden nueva
> haría que el gateway la ignorara como duplicado.

## Salvaguardas del borde (estado actual)

El gateway aplica, en este orden, tres capas antes de tocar el ESP32: **idempotencia →
validación de rangos técnicos → whitelist de campos permitidos**. En la fase actual la
whitelist solo admite `Debug`; **cualquier comando que toque `Valve`, `Inv`, `Restart` o
`Timestamp` se rechaza** (`result: Rechazado`). El control físico real está a la espera de las
decisiones del ticket [#10](https://github.com/bisite/FertLoops/issues/10) (modos de
autoridad, interlocks, límites de riego). El servidor puede emitir esos comandos, pero hasta
que la whitelist se amplíe recibirá `Rechazado`.

## Puntos abiertos (resumen)

1. **Acceso del servidor al broker**: Bridge saliente desde la Pi. Determina si el broker del
   borde se abre a la red o permanece en localhost.
2. **`gateway_id` real** y cómo se asigna.
3. **Formato de la telemetría** que espera el consumidor del servidor (JSON crudo del ESP32 vs.
   adaptado).
4. **Autenticación y ACLs** del MQTT: quién puede publicar comandos.
5. **Ampliación de la whitelist** de control, ligada al ticket
   [#10](https://github.com/bisite/FertLoops/issues/10).
