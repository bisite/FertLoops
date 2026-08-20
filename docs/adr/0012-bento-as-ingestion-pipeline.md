---
status: accepted
---

# Bento como tubería de ingesta

La ingesta la hace **Bento** (la bifurcación de Benthos que mantiene WarpStream Labs), con la imagen de serie y sin distribución propia, entre el broker MQTT del VPS y TimescaleDB. Es el **único escritor del camino de ingesta**.

## Por qué una tubería y no código propio

Los dos extremos ya estaban decididos: MQTT con Mosquitto ([ADR-0002](0002-mqtt-mosquitto-bridge-as-pi-vps-transport.md)) de un lado, PostgreSQL con TimescaleDB ([ADR-0001](0001-timescaledb-container-as-measurement-store.md)) del otro. Faltaba el trozo del medio, y [ADR-0011](0011-canonical-measurement-model.md) le puso una lista concreta:

- Traducir rutas del JSON de la trama a series, con un mapa fijo ruta→(contexto, magnitud, unidad).
- Validar rangos de plausibilidad por instrumento, siendo ese mapa la única fuente de verdad de los límites.
- Autoprovisionar `device` y sus `sensor` cuando aparece una MAC nunca vista.
- Escribir la Cuarentena cuando un valor no es plausible o no se puede adscribir.
- Detectar la divergencia del contrato en ambas direcciones.

**Todo eso son transformaciones y escrituras, no lógica de negocio.** Escribirlo a mano obligaría además a implementar reintentos, contrapresión y cola de fallidos, que es infraestructura ya resuelta. El mapa del proyecto pide «mínimo software propio», «estándares antes que invención» y «componentes pequeños, probados y empaquetados».

Pesa también el relevo: el mantenimiento recae en estudiantes que rotan, y **un mapa de transformación se lee y se cambia sin compilar nada**.

## Opciones consideradas

- **Vector.** El competidor más cercano, porque también tiene fuente y sumidero MQTT. Descartado por postura de riesgo: **ambos están marcados como beta por su propia documentación**, y el sumidero es **solo para logs**. Tampoco documenta ningún concepto de cola de mensajes fallidos: si la base de datos se cae, el modelo de búferes es toda la historia. Su lenguaje de transformación no admite funciones de usuario ni E/S por diseño.
- **Telegraf.** Descartado por su comportamiento bajo presión: al desbordarse el búfer **sobrescribe en silencio las métricas más antiguas**, es decir, su modo de fallo por defecto es **perder datos sin avisar** — lo contrario de lo que queremos con telemetría de riego durante una caída. Y su salida a PostgreSQL está diseñada para **crear tablas automáticamente** por medida, un desajuste de fondo con un esquema fijo y diseñado a mano.
- **Consumidor propio.** Descartado por lo dicho arriba. Queda como plan de respaldo si las capacidades de serie resultan insuficientes.
- **Bento**, elegido. Entrada y salida MQTT nativas y **no marcadas beta**, con los tres niveles de QoS, TLS y autenticación. Bloblang como lenguaje de mapeo y validación, sin compilación. Y la salida `sql_raw` de serie con controlador PostgreSQL, marcadores posicionales y agrupación por número, tamaño o periodo: **escribir en la base de datos no necesita ningún plugin compilado.**

Que la entrada y la salida MQTT sean simétricas no es un requisito de hoy —el camino descendente aún no está diseñado— pero evita traer una segunda herramienta cuando [#10](https://github.com/bisite/FertLoops/issues/10) lo aborde.

## De serie, sin distribución propia

Bento permite compilar un binario propio que incluya plugins. **No se hace**: todo lo que hace falta está en la construcción por defecto, y una distribución propia añadiría un binario que mantener y actualizar a cambio de nada, reintroduciendo por la puerta de atrás el software propio que elegir Bento evita.

## Consecuencias

- **Que sea el único escritor es lo que hace obligatoria la validación.** Si otro componente pudiera insertar en `reading`, los rangos serían una convención saltable en lugar de una garantía, y el invariante de [ADR-0011](0011-canonical-measurement-model.md) —en `reading` solo hay valores plausibles— depende de esa frontera.
- **La cola de fallidos se expresa encadenando destinos**, no devolviendo el mensaje al broker: MQTT no está entre los transportes que soportan eso. El destino secundario es la tabla de Cuarentena.
- **Los reintentos ante una base de datos caída son configuración**, con retroceso exponencial y contrapresión hacia el broker.
- **La configuración pasa a ser código crítico.** Es la contrapartida de mover la lógica a ficheros: un mapa de transformación equivocado corrompe datos igual que un `if` mal puesto, así que necesita revisión y pruebas como tal. Bento trae su propio ejecutor de pruebas de configuración, que conviene usar desde el principio.
- **Un servicio más en Compose**, con su sonda de salud HTTP y dependencia del contenedor de la base de datos, en el modelo de [ADR-0003](0003-docker-compose-with-native-caddy-as-vps-deployment-model.md).
- **Nada de esto está implementado todavía.** Este ADR fija la herramienta y su alcance; de qué temas MQTT se alimenta y cómo se reparten borde y servidor es de [#9](https://github.com/bisite/FertLoops/issues/9).
