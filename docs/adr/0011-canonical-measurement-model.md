---
status: accepted
---

# Modelo canónico de una medida

Qué es una medida, desde la trama UART hasta lo que se guarda y se consulta. Resuelve [#7](https://github.com/bisite/FertLoops/issues/7).

## Filas estrechas

Una medida es una fila `(sensor, instante, valor)`, no una fila por trama con una columna por magnitud. Razones:

- **Añadir un sensor es un `INSERT`, no una migración.** El contrato UART está congelado hoy, pero el mapa contempla un segundo módulo de invernadero.
- **Es la forma para la que están hechas la compresión y los agregados de TimescaleDB.** Con `segmentby = 'sensor_id'` se miden 51,3× de compresión; con filas anchas ese eje no existe.
- **Las magnitudes no son homogéneas**: cuatro contextos y diez unidades. En filas estrechas la unidad vive en `sensor_type`, consultable; en filas anchas viviría en el nombre de la columna.

Coste aceptado: **alinear magnitudes del mismo instante exige un `JOIN` o un pivote.** Se paga con una vista escrita una vez, y a partir de ahí las fórmulas agronómicas la consultan como si fuese ancha.

## Identidad: el `devID` es un detalle de transporte

**La MAC del ESP32 no es la identidad de la Mesa de drenaje.** El escenario que lo decide:

> A mitad de campaña el ESP32 de una Mesa se avería y se sustituye. La MAC nueva es distinta.

Con la MAC como identificador de negocio aparece una fila `device` nueva y **la serie de la Mesa queda partida en dos sin que nadie lo declare** — y comparar campañas completas entre Mesas (objetivo O6) es justo la consulta que ese corte estropea en silencio.

| Concepto | Dónde | Identidad | Sustituible |
| --- | --- | --- | --- |
| Device (ESP32) | `device`, en el núcleo | la MAC | sí |
| Mesa de drenaje | `plot`, en `db/migrations/app/` | nombre estable | no |
| Qué Device en qué Mesa y cuándo | tabla de adscripción, en `app/` | — | — |

**Restricciones:** un Device está en una sola Mesa a la vez; **una Mesa puede tener varios Device a la vez**, y no se añade ninguna restricción que lo impida porque es un caso legítimo (réplica espacial, o solape durante una sustitución).

Ambas reglas se ensayaron sobre `device_placement` del núcleo, que tiene la misma forma temporal, para confirmar que un índice único parcial las expresa bien. **Pero fue un ensayo con un sustituto**: la adscripción real vive en `app/`, así que ese índice hay que crearlo allí, y mientras esa tabla no exista la regla está documentada pero no la impone nada.

## Alta automática, sin perder lecturas

El sistema **no puede saber** si una MAC nueva es una Mesa nueva o un recambio: eso solo lo sabe una persona. Así que el diseño deja entrar el dato sin obligar a adivinar:

1. MAC desconocida: la ingesta **autoprovisiona** el `device` y sus 17 `sensor`. Las lecturas entran; nada se rechaza.
2. Ese Device no tiene adscripción activa, y eso ya *significa* «sin adscribir», sin campo nuevo: se consulta con un `NOT EXISTS`.
3. Salta un aviso — trabajo de [#15](https://github.com/bisite/FertLoops/issues/15).
4. Una persona declara la adscripción. **La fecha de inicio se puede poner hacia atrás**, y el alta debe fecharla en la **primera lectura del Device**, no en `now()`. Nada lo impide pero tampoco lo hace solo: fechada en `now()`, todo el histórico anterior queda fuera de la ventana y deja de ser atribuible, en silencio. Es el punto frágil de este flujo.
5. Sustitución: se cierra la adscripción vieja y se abre la nueva. **La serie de la Mesa es continua porque es una consulta**, no una tabla que haya que reescribir — obligatorio, porque `reading` es de solo añadido y una hipertabla.

## Las 17 series de un Device

| Contexto | Series |
| --- | --- |
| `water` | `ph` (–), `conductivity` (ppm), `irrigation_volume` (L) |
| `soil` | `temperature` (degC), `humidity` (%), `conductivity` (mS/cm) |
| `air` | `temperature` (degC), `humidity` (%), `solar_radiation` (W/m2) |
| `equipment` | `adc_error`, `pulse_counter_error`, `i2c_error`, `inverter_error`, `inverter_state` (code); `valve_position` (deg), `inverter_on` (code), `inverter_frequency` (Hz) |

**Los errores son series, no ruido.** Es la única forma de responder «¿estaba este sensor en fallo en el instante T?» con un `JOIN` de igualdad por `(device, observed_at)`. Comprobado: sobre un año de datos, ese `JOIN` aísla las 180 medidas de pH tomadas mientras el ADC fallaba, de 524.161. Un diseño de eventos exigiría un *as-of join* por rangos, y detectar transiciones obligaría a una ingesta con estado que pierde el cambio si el enlace cae en el momento equivocado.

**`inverter_state` no es un error.** Vive bajo `Errors` en la trama, pero sus valores son el estado operativo del variador (0 Standby, 1 en marcha, y a partir de 4 sí fallos). Llamarlo error haría que «el inversor está funcionando» contase como avería en cualquier panel.

**El Eco de control es estado, y sirve de confirmación.** `Valve`, `Inv.On` e `Inv.Freq` se guardan como series: contestan «¿estaba la válvula abierta cuando se tomó esta medida de humedad?» y «¿arrancó de verdad la bomba cuando se lo pedimos?», esta última cruzando la serie contra el histórico de estado deseado. **Este ADR no decide el camino de control, solo guarda el eco con fidelidad**; compararlo es de [#10](https://github.com/bisite/FertLoops/issues/10).

**Fuera de `reading`** quedan `Timestamp` (el reloj del Device, que se ignora — [ADR-0013](0013-clock-policy-and-timestamping.md)), `Sample_per_minute` (estático: 1440 filas diarias repitiendo un 6), `Restart` (transitorio) y `Debug` (13 niveles de log; trece series constantes no son datos).

Eso obliga a que **el mapa de rutas de la ingesta tenga tres categorías, no dos**: mapeada, **conocida e ignorada a propósito**, y desconocida. Sin la intermedia, esos campos irían a Cuarentena en cada trama y el aviso de «ruta desconocida» sería ruido permanente en lugar de una señal.

**No hay serie «de la Mesa» en el esquema.** Cuando una Mesa tenga varios Device, «la temperatura del suelo de mesa-03» será una consulta de la aplicación, no una entidad: hoy la relación es 1:1, cualquier regla no se ejercitaría, y las alternativas son aditivas el día que haga falta.

Lo que sí se fija, porque es lo que se rompe en silencio: **la función de agregación depende de la magnitud.**

| Categoría | Función | Series |
| --- | --- | --- |
| Incrementales | `SUM` | `irrigation_volume` |
| Instantáneas | `AVG` | las ocho magnitudes físicas restantes |
| Categóricas | `MAX` / `LAST` | los cinco códigos, y `inverter_on` |

Agregar dos Device de la misma Mesa con `AVG` sobre el volumen **divide el agua por dos**. Comprobado que la vía correcta funciona: la suma diaria del agregado cuadra con las filas crudas con diferencia nula.

## Calidad del dato

Con los errores como series, dos de los tres estados salen sin añadir nada:

- **«No medido»** = ausencia de fila. El hueco es el dato, y es detectable.
- **«Sensor en fallo»** = derivable de la serie de errores.
- **«Fuera de rango»** = **Cuarentena**.

La validación es de la tubería ([ADR-0012](0012-bento-as-ingestion-pipeline.md)). El valor fuera del rango de su instrumento **no entra en `reading`** sino en una tabla de cuarentena en `app/`. Así `reading` conserva su invariante —toda fila es plausible— que es lo que mantiene los agregados fiables **sin filtros**; y a la vez el rechazo es consultable, que un fichero en disco no lo es. El caso que lo justifica: una sonda de pH descalibrada devolviendo 15,2 sin error declarado, y sin cuarentena nadie puede ver que lleva dos semanas haciéndolo.

Se descarta **una columna de calidad en `reading`**: obligaría a cada consumidor a acordarse de filtrar y olvidarse sería silencioso. Ya hay un filtro obligatorio; añadir un segundo multiplica las formas de sacar un agregado sutilmente mal.

**Los rangos viven solo en la configuración de la ingesta**, no en una tabla espejo que se desincronice. La fila de cuarentena **se explica sola** —valor crudo, sensor, instante y el límite que violó— y su `sensor_id` es **anulable**, porque el rechazo puede deberse justo a no poder adscribir el valor a ningún sensor; entonces guarda la ruta cruda y la MAC.

Los rangos son del **instrumento**, no de la magnitud: el pH es 0–14 en todo el universo, pero 0–2000 W/m² es el fondo de escala de *este* piranómetro. Por eso son política del proyecto y no vocabulario del núcleo. Y no solo el valor tiene que ser plausible: también el instante ([ADR-0013](0013-clock-policy-and-timestamping.md)).

## Evolución del contrato

El contrato está **congelado sin versionado**, y ya cambió una vez sin avisar. El modo de fallo no es hipotético.

**El mapa de rutas de la ingesta *es* el contrato**, y la divergencia se detecta en ambas direcciones: una ruta en la trama que no está en el mapa (campo añadido o renombrado) va a Cuarentena y avisa; una ruta del mapa que falta en la trama (campo eliminado) avisa. Comprobar ausencias es configuración adicional, no sale sola de mapear lo conocido.

Un número de versión diría «algo cambió»; el mapa dice **qué** cambió. Y **no se pide campo de versión** al equipo de electrónica: la trama cambió de forma sin bombear ninguno, así que un campo que no se actualiza con disciplina da falsa confianza, no detección.

**La Cuarentena hace de búfer de reproducción.** Si se añade un sensor nuevo, sus lecturas caen ahí con su ruta cruda, y al añadir la ruta al mapa y sembrar el tipo **se promueven a `reading` con una consulta de relleno**. No se pierde el intervalo.

Queda un caso que la detección estructural **no ve**: mismas rutas, significado distinto. Si la conductividad del agua pasara de ppm a mS/cm todas las rutas cuadrarían y los datos quedarían mal por un factor de ~500 a ~700. Ese riesgo se acepta y se traslada a las alarmas de umbral de [#15](https://github.com/bisite/FertLoops/issues/15).

## Unidades: tal como llegan

Sin conversión ni normalización. La trampa: **la misma magnitud física llega en dos unidades**, conductividad del suelo en mS/cm y del agua en ppm, porque son dos sondas distintas —la de agua es TDS y mide sólidos disueltos—. Quien compare las dos series sin mirar la unidad se equivoca en tres órdenes de magnitud.

Convertir exigiría **elegir un factor de división** y no hay uno correcto: ÷500, ÷640 y ÷700 son convenios según el tipo de sal. Elegir uno sería inventar precisión que no tenemos. El esquema ya está montado para esto: la clave única de `sensor_type` es `(contexto, magnitud, unidad)`, así que salen dos filas inequívocamente distintas.

## Consecuencias

- **El volumen es el doble de series de lo que suponía el esquema, y menos de la mitad de bytes de lo estimado.** Medido con un año de datos: 17 series a una lectura por minuto son **8,91 M filas/año por Mesa**, 2171 MB → **42 MB comprimidos (51,3×)**. Para las 12 Mesas: ~294.000 filas/día, ~107 M/año y **~508 MB/año**. Es la cifra que [#14](https://github.com/bisite/FertLoops/issues/14) pedía derivar de este modelo.
- **Las series de observabilidad son casi gratis.** Pasar de 9 a 17 series duplica las filas pero apenas mueve los bytes: los códigos y los ecos son casi constantes y comprimen mucho mejor que una sinusoide, así que la razón **sube** de 26,5× a 51,3×. Trazabilidad completa por un coste marginal.
- **El vocabulario tiene su propia fuente de migración**, `db/migrations/seed/`, ya escrita: 4 contextos, 10 unidades y los 17 tipos, cada uno anotado con la ruta del JSON de la que sale. Es idempotente. No dependía de otras decisiones —la determina el contrato congelado— así que ha podido aterrizar ya.
- **Que el vocabulario esté cerrado es lo que hace que signifique algo.** La tubería rechaza una terna que no esté sembrada en lugar de crearla, y por eso añadir una magnitud es una migración. Se descartó que la ingesta creara el vocabulario al vuelo desde su propio mapa: evitaría la duplicación, pero lo volvería abierto, y una errata acuñaría un tipo espurio en silencio. La duplicación se asume y es **autodetectable**: una terna sin sembrar acaba en Cuarentena.
- **Falta escribir `db/migrations/app/`**: `plot`, la adscripción y la cuarentena. Depende de [#9](https://github.com/bisite/FertLoops/issues/9).
- **`location` y `device_placement` del núcleo quedan sin consumidor real**, porque la adscripción vive en `app/`. Hay que saberlo o alguien las usará por error.
- **Filtrar `reading` por Device sin acotar el tiempo es caro.** La hipertabla está particionada por `observed_at`, así que una consulta que no restringe el instante no puede descartar *chunks*. Medido: «la primera lectura de este Device» sobre 8,91 M filas superó los 8 minutos. Fechar la adscripción en la primera lectura es barato **si se hace pronto**, y caro si se deja acumular un año.
