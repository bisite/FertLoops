# FertLoops

Sistema de fertirrigación de bucle cerrado para un módulo del invernadero del CIALE (USAL): Device instrumentando cada mesa de drenaje, un Gateway (Raspberry Pi) por emplazamiento, un almacén de medidas en el VPS, y un Modo de control por fases que gobierna la autoridad de decisión.

## Lenguaje

**Mesa de drenaje**:
Unidad de zona de cultivo dentro del módulo de invernadero, con su propia línea de riego y fertilización independiente. Es la identidad durable del sistema —sobrevive a la sustitución del hardware que la instrumenta— y en código se llama `plot`. Puede tener varios Device asignados a la vez, aunque hoy tenga uno. La trama UART no lleva un campo de zona propio: qué Mesa de drenaje corresponde a un Device es una adscripción que se declara, no un dato que venga en la trama.
_Evitar_: zona, bancada, parcela.

**Device**:
Rol de nodo de borde responsable de leer los sensores y controlar el inversor y la válvula de riego de la Mesa de drenaje a la que esté asignado. Se identifica en el transporte por su `devID` —la MAC del ESP32—, que es un **detalle de transporte y no la identidad de la Mesa de drenaje**: un Device es sustituible, y al cambiarlo la Mesa conserva su historia. Un Device está asignado a una sola Mesa de drenaje a la vez. Hardware actual: **ESP32** — lee los sensores (pH, conductividad del agua, temperatura y humedad del aire y del suelo, conductividad del suelo, radiación solar, volumen de riego) cada 10 segundos (`Control.Sample_per_minute`). El Gateway lo consulta por UART con el comando `read`, que devuelve la última muestra tomada, con hasta 10 segundos de antigüedad.
_Evitar_: dispositivo Slave, nodo, placa, ESP32 (como término general — vale para nombrar el hardware, no el rol).

**Gateway**:
Rol que agrega por UART las lecturas de todos los Device de un mismo emplazamiento, sostiene una cola local durante un corte del enlace con el VPS, y es la autoridad de tiempo (NTP) para las medidas — el reloj de a bordo del Device (RTC) se ignora, con hasta 10 segundos de margen. También aloja el motor de decisión que gobierna el Modo de control; su estructura y el alcance de la analítica local que incorpore siguen sin decidirse. Hardware actual: **Raspberry Pi**, con SSD de 1 TB para la cola local.
_Evitar_: el borde (es un término distinto, ver más abajo), la Pi (salvo como referencia tras la primera mención).

**El borde**:
El lado del sistema físicamente en el invernadero: los Device de cada mesa de drenaje y el Gateway que los agrega. Se opone al VPS, el lado servidor — modelo de dos niveles (borde/edge vs. nube/cloud). Aviso para quien venga de la literatura de IoT: algunos modelos de referencia (NIST, IEEE 1934) usan "edge" en sentido más estrecho, como una capa intermedia entre el "device" y el "cloud"; aquí se opta deliberadamente por el sentido de dos niveles.
_Evitar_: edge (en prosa; vale en identificadores de código, p. ej. `edge-broker`).

**Eco de control**:
Estado de los actuadores (`Valve`, `Inv.On`, `Inv.Freq`) que el Device devuelve en cada trama `read`, junto a las medidas. Se guarda como serie temporal igual que una medida, y es lo que permite comprobar si el sistema hizo de verdad lo que se le pidió: no hay un canal de acuse aparte.
_Evitar_: acuse, confirmación de comando.

**Cuarentena**:
Destino de una lectura que llega pero no se puede dar por buena: fuera del rango plausible de su instrumento, o imposible de adscribir a un sensor conocido. No se descarta ni entra en las medidas válidas; queda consultable con su valor crudo y el motivo, y puede promoverse más tarde si resulta interpretable.
_Evitar_: descarte, rechazo, dead-letter.

**Modo de control**:
Fase de autoridad que gobierna un mismo camino de código de decisión: `shadow` (registra la acción que tomaría, sin ejecutarla), `supervised` (una persona confirma cada acción antes de ejecutarse) o `autonomous` (bucle cerrado, sin supervisión humana). Las tres fases comparten la misma lógica de decisión; lo que cambia es quién autoriza la ejecución.
_Evitar_: nivel de automatización, modo de operación.
