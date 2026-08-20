---
status: accepted
---

# Política de relojes, sellado de tiempo y frescura

Hay dos relojes en el sistema y solo uno cuenta. Resuelve [#8](https://github.com/bisite/FertLoops/issues/8).

## Un solo reloj: el del Gateway

La Raspberry Pi, sincronizada por NTP, es la autoridad de tiempo. El **RTC del Device se ignora como si no existiese**, aunque la trama traiga un campo `Timestamp` leído de él.

No es solo que la Pi sea más fiable: **el RTC no es fiable de ninguna manera utilizable.** Puede venir desfasado, **puede venir vacío**, y la trama de control permite *ponerlo en hora*, así que ni siquiera es monótono. Un dato que puede faltar y que cualquiera puede reescribir no sirve como sello de un registro científico. Fue lo acordado con el equipo de electrónica en [#2](https://github.com/bisite/FertLoops/issues/2), con el contrato congelado.

- `reading` se queda con **dos** columnas de tiempo, no tres: `observed_at` y `created_at`.
- Se acepta un **margen de hasta 10 segundos** entre el instante físico de la muestra y `observed_at`, que es el intervalo de muestreo del Device. Es imprecisión conocida, no un error a corregir.
- **No se registra el desvío entre relojes.** Se consideró guardarlo como serie para vigilar la deriva y se descartó: medir la deriva de un reloj que nadie usa, y que puede no reportar, es guardar basura.
- **Nunca se envía `Timestamp` en la trama de control.** Si el RTC se ignora, sincronizarlo es trabajo sin consumidor.
- **La ventaja de no añadir una segunda columna de instante es que nadie puede consultar por el reloj equivocado.** La pregunta «cuál manda en las consultas» desaparece en lugar de resolverse por convenio, que es la clase de convenio que alguien acaba rompiendo.

## Almacenamiento en UTC, lectura en hora local

`TIMESTAMPTZ` guarda un instante absoluto, así que el formato con el que publique el Gateway es indiferente y la conversión es transparente.

Lo que **no** es transparente es la agregación por días: `time_bucket` ancla en las 00:00 UTC e ignora la zona horaria de la sesión, así que hizo falta un argumento explícito. Ver [ADR-0009](0009-timescaledb-layer-hypertable-and-aggregates.md).

## Cotas de plausibilidad sobre `observed_at`

[ADR-0011](0011-canonical-measurement-model.md) acotó los **valores** y no los **instantes**. El escenario que cierra ese hueco: **la Pi no tiene RTC**, así que tras un arranque en frío sin red su reloj es lo que sea hasta que NTP responda. Como el del Device se ignora, ese sello equivocado es el único que hay.

- **Superior: hora del servidor más un minuto.** Una lectura con fecha futura no es sospechosa, es **activamente dañina**: envenena cualquier consulta de «último valor», la agregación en tiempo real y todo panel de estado actual **hasta que el tiempo real la alcance**. Con Gateway y VPS sincronizados por NTP, más de un minuto de adelanto es una avería.
- **Inferior: la fecha de arranque del proyecto**, fija. Una lectura de 1970 significa reloj sin sincronizar, no una medida antigua legítima.
- **Nada de ventanas móviles por abajo.** La cola local está diseñada para 15 días de corte ([ADR-0002](0002-mqtt-mosquitto-bridge-as-pi-vps-transport.md)) y no hay retención, así que rechazar lo antiguo destruiría su propósito.
- **Lo que se sale va a Cuarentena, nunca al descarte.** El fallo de reloj es transitorio y **las medidas son buenas**: lo único malo es el sello. Dejarlas consultables permite recuperarlas y convierte el fallo en algo visible en lugar de un hueco silencioso.

## Orden y duplicados al reponerse un corte

Nada que decidir: está comprobado que funciona.

- **Desorden**: la hipertabla absorbe una inserción con `observed_at` viejo, y el refresco sin límite de los agregados reagrega lo que llegue tarde.
- **Duplicados**: MQTT con QoS 1 es «al menos una vez», así que las repeticiones son lo esperado. El índice único parcial las rechaza, y `ON CONFLICT (sensor_id, observed_at) WHERE corrects_reading_id IS NULL DO NOTHING` infiere ese índice y las salta en silencio, dejando pasar una corrección en el mismo instante.
- **La deduplicación sobrevive a la compresión.** Era la duda que más importaba, porque toda la idempotencia depende de ella: un duplicado dentro de un *chunk* ya comprimido **también se rechaza**.
- **Los 15 días de cola y los 14 de compresión no se contradicen.** Reinyectar un día de una serie en zona comprimida cuesta 28,7 ms frente a 20,8 ms sin comprimir, y el *chunk* **sigue comprimido**.

Se descarta la deduplicación por caché en la ingesta: una ventana anula la garantía de «al menos una vez» justo cuando el duplicado llega tarde, que es el caso que importa.

## Frescura: el bucle de control no cruza el VPS

Es el punto menos evidente, y por eso se escribe: **la suposición natural es la contraria.**

El motor de decisión vive en el **Gateway** (`CONTEXT.md`), que ya tiene las lecturas del UART, así que para decidir no necesita ningún viaje al servidor. **Si el enlace con el VPS se cae quince días, el control sigue funcionando**: se pierde la observación, no el gobierno. Por eso **no se fija ningún objetivo de latencia para el bucle de control** — no aplica. Quien diseñe pensando que el servidor decide acabaría inventando un requisito que no existe.

El presupuesto extremo a extremo, derivado de lo ya decidido:

| Tramo | Peor caso |
| --- | --- |
| Muestreo del Device | 10 s |
| Publicación del Gateway al VPS | 60 s |
| Agrupación por lotes de la ingesta | ≤ 1 s |
| **Total** | **~71 s** |

- **Para el modo `supervised` es holgadamente suficiente.** Es el único modo con una persona en el lazo, y el riego es un proceso de minutos a horas. Dónde confirma esa persona es de [#10](https://github.com/bisite/FertLoops/issues/10) y [#12](https://github.com/bisite/FertLoops/issues/12); lo que aquí se aporta es que **la antigüedad del dato no es el factor limitante**.
- **«Dejaron de llegar medidas» necesita un umbral holgadamente por encima de esos 71 s**, porque vaciar la cola tras una recuperación lleva tiempo y un umbral apretado convertiría cada reconexión en falsa alarma. Se entrega a [#15](https://github.com/bisite/FertLoops/issues/15) el punto de partida: **5 minutos**, más de cuatro veces el peor caso.
- **El periodo de agrupación de la ingesta forma parte del presupuesto de frescura.** Hoy es ≤1 s y es despreciable, pero subirlo para «optimizar escrituras» gastaría latencia sin que nadie lo notase.
