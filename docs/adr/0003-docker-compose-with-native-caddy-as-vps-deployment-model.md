---
status: accepted
---

# Docker Compose con Caddy nativo como modelo de despliegue del VPS

El VPS se opera con **todo orquestado en Docker Compose, salvo el proxy inverso**, que corre como paquete nativo de Ubuntu bajo systemd, desacoplado de Docker por completo. La base de datos ya vivía en Compose desde [#3](https://github.com/bisite/FertLoops/issues/3); esta decisión generaliza ese régimen al resto de servicios propios en lugar de al revés, invirtiendo la recomendación inicial del informe de investigación (que partía de paquetes nativos como norma y contenedores como excepción). El proxy elegido es **Caddy**, no nginx como recomendaba el informe.

## Por qué se invierte la recomendación del informe

El informe (`docs/research/despliegue-y-operacion.md`, rama `research/despliegue-y-operacion`) recomendaba paquetes nativos como modelo general porque `unattended-upgrades` cubre por defecto los parches de seguridad de APT, mientras que los contenedores necesitarían un mecanismo de actualización de imágenes aparte. Esa observación sigue siendo cierta, pero la elección aquí es priorizar la homogeneidad operativa de "todo en Compose salvo lo que sirve TLS al público" sobre el argumento de la actualización desatendida — y compensar esa pérdida con una política explícita (ver «Consecuencias»).

## Decisiones

- **Modelo de despliegue.** Docker Compose para todos los servicios propios (base de datos, ingesta, Grafana). Caddy es la única excepción: paquete APT nativo + systemd, sin contenedor.
- **Actualizaciones.** `unattended-upgrades` solo para el sistema operativo del host, Docker y Caddy. Todo lo que vive en Compose se actualiza de forma manual y deliberada — el mismo régimen ya fijado para la base de datos en [#3](https://github.com/bisite/FertLoops/issues/3), ahora generalizado.
- **Proxy inverso y TLS.** Caddy, con el certificado emitido a mano por USAL (sin ACME, restricción fija del mapa). Verificado en [#21](https://github.com/bisite/FertLoops/issues/21) (`docs/research/certificado-caddy.md`): cargar un certificado propio desactiva la emisión/renovación automática para ese sitio, aunque el refresco de OCSP staple sigue corriendo cada hora igualmente (salida de red esperable, no un fallo); el fichero de certificado sigue el mismo convenio que nginx — certificado del servidor primero, intermedios después, en un único fichero — deducido del código de Go (`tls.X509KeyPair`) al no estar explícito en la documentación de Caddy; y la recarga documentada (`caddy reload --force` / `SIGUSR1`) tiene un fallo conocido y sin resolver en el propio repositorio de Caddy al recachear un certificado sustituido, así que el runbook debe prever un reinicio completo como respaldo probado, no asumir que la recarga basta.
- **Secretos y configuración.** El `.env` con valores reales vive solo en el VPS y se transmite a mano entre personas que rotan (gestor de contraseñas del equipo); el repositorio solo lleva `.env.example` con las claves sin valores. Respaldo del `.env` real por dos vías: el gestor de contraseñas del equipo **y**, además, el propio repositorio de restic.
- **Copias de seguridad.** restic, tal y como recomendaba el informe, con restauración real periódica verificada por consulta (no solo `check`). Alcance ampliado respecto al informe original por el giro a contenedores: volcados de PostgreSQL, SQLite de Grafana, y la configuración de Caddy del host.
- **Observabilidad mínima.** Dos de las tres capas que proponía el informe: `OnFailure=` de systemd para "el proceso murió", y el estado No Data de Grafana para "dejaron de llegar medidas". Se descarta la tercera capa (*dead man's switch* externo tipo Healthchecks) por sobreingeniería para el tamaño de este proyecto.
- **Arranque desde cero.** Depende de cinco piezas: el repositorio git (ficheros de Compose y configuración de Caddy versionada), Docker + Docker Compose + Caddy instalados en un Ubuntu limpio, el `.env` real, el repositorio de restic, y el certificado TLS repedido a USAL. El procedimiento paso a paso se redacta en [#17](https://github.com/bisite/FertLoops/issues/17), no aquí.

## Opciones consideradas

- **nginx** (recomendación original del informe). Descartado en favor de Caddy por preferencia explícita sobre mantener el proxy nativo y desacoplado; el informe no encontró ningún defecto técnico en nginx, así que este descarte es una elección de equipo, no un hallazgo.
- **Traefik.** No investigado ni por el informe ni en esta sesión. Queda como incógnita abierta; no bloquea esta decisión porque Caddy ya cubre el caso de uso sin necesidad de comparar.
- **Paquetes nativos como modelo general** (recomendación original del informe para todo el stack, no solo el proxy). Descartado por preferencia de homogeneidad operativa en Compose; ver «Por qué se invierte la recomendación del informe».
- **`sops` + `age` para secretos cifrados en el repositorio.** Descartado frente a mantener el `.env` real fuera del repositorio: menos software propio que aprender, coherente con la preferencia fijada de "estándares y convenciones antes que invención".
- **Dead man's switch externo (Healthchecks, autoalojado o en la nube).** Descartado explícitamente como sobreingeniería para las características de este proyecto.

## Consecuencias

- **El hueco de "el VPS entero se cayó" queda como riesgo aceptado.** Ni `OnFailure=` de systemd ni el estado No Data de Grafana detectan la caída de la máquina completa, porque ambos viven dentro de ella. Se descubre por comprobación manual, no por alerta automática. Decisión consciente, no omisión.
- **El proxy es la única superficie con parches automáticos.** Es también la única superficie expuesta directamente a internet, así que es donde más importa que no queden ventanas de actualización abiertas sin depender de que alguien recuerde actualizar un contenedor a mano.
- **La reconstrucción completa no es 100 % reproducible solo desde git.** Hace falta además el `.env` real (fuera del repositorio) y el certificado (fuera de este sistema, en USAL). Es una consecuencia directa de haber elegido la opción (a) de secretos frente a cifrarlos en el repositorio.
- **[#16](https://github.com/bisite/FertLoops/issues/16)** (estructura del repositorio, entorno de desarrollo y CI) hereda este modelo al decidir cómo se organizan los ficheros de Compose y la configuración de Caddy versionada.
- **[#14](https://github.com/bisite/FertLoops/issues/14)** (retención, agregación y copias de seguridad) hereda restic como mecanismo ya decidido; lo que queda por fijar ahí es cuánto se retiene y con qué resolución, no con qué herramienta se respalda.
- **[#15](https://github.com/bisite/FertLoops/issues/15)** hereda las dos capas de observabilidad de aquí como parte de "qué avisa el sistema", incluido el aviso de caducidad del certificado que este informe proponía como temporizador `systemd` + `openssl x509 -checkend` — pendiente de decidir en ese ticket junto con el resto de alarmas.
- **[#17](https://github.com/bisite/FertLoops/issues/17)** (runbook) debe incorporar, a partir de lo verificado en [#21](https://github.com/bisite/FertLoops/issues/21): permitir la salida de red del refresco de OCSP staple, probar empíricamente la recarga de Caddy tras sustituir el certificado con un reinicio completo como respaldo, y verificar la cadena servida (`openssl s_client -showcerts`) tras el primer despliegue real.

## Lo que sigue sin resolverse

- **Traefik no investigado.** No bloquea esta decisión, pero queda sin comparar si en el futuro Caddy resultara insuficiente.
- **Cifras de memoria del conjunto de servicios en 4 GB.** El informe ya señalaba que no hay fuente oficial para estimarlo; habrá que medirlo con la instalación real.
- **Recarga de nginx** ya no aplica al haber elegido Caddy; el hallazgo del informe sobre concatenación de intermedios en nginx queda como referencia histórica, no como pieza operativa.
