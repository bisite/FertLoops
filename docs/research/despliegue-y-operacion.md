# Despliegue y operación en el VPS

Informe de investigación para el ticket [#6](https://github.com/bisite/FertLoops/issues/6) del mapa de wayfinding ([#1](https://github.com/bisite/FertLoops/issues/1)). Fuentes consultadas el 30 de julio de 2026. Se distingue explícitamente lo documentado de lo que es razonamiento propio, y se señala lo que no se ha podido verificar en esta sesión.

Recordatorio de reparto de responsabilidades: la provisión del VPS, el sistema operativo, la VPN y **la emisión y renovación del certificado** son del equipo de infraestructura de USAL. Este informe trata de **consumir bien** lo que nos dan.

## Recomendaciones por área

| Área | Recomendación |
| --- | --- |
| Modelo de despliegue | **Paquetes nativos con unidades systemd**; Podman con quadlets solo para lo que no esté empaquetado |
| Proxy inverso y TLS | **nginx**, con la cadena concatenada en el orden documentado |
| Copias de seguridad | **restic**, con verificación rodante y **una restauración real periódica**, no solo `check` |
| Observabilidad mínima | **Tres capas**: `OnFailure=` de systemd, estado No Data de Grafana, y un *dead man's switch* externo |

---

## 1. Modelo de despliegue: paquetes nativos y systemd

### El argumento decisivo es la actualización desatendida

`unattended-upgrades` actualiza lo instalado por APT, y su configuración por defecto incluye los orígenes de seguridad y de actualizaciones:

> `Unattended-Upgrade::Allowed-Origins { "${distro_id} ${distro_codename}-security"; "${distro_id} ${distro_codename}-updates"; }`

Se controla con dos ficheros —`/etc/apt/apt.conf.d/20auto-upgrades` (activa el trabajo periódico) y `/etc/apt/apt.conf.d/50unattended-upgrades` (qué se actualiza y qué se excluye)— y tiene un detalle que hay que conocer: para que el reinicio automático funcione «you not only need to set `Unattended-Upgrade::Automatic-Reboot "true"`, but you also need to have the `update-notifier-common` package installed». Sin ese paquete no reinicia, aunque esté configurado.

**Los contenedores no heredan esto.** Podman ofrece su propio mecanismo con quadlets: un fichero `.container` se convierte en unidad de systemd («These files are read during boot (and when `systemctl daemon-reload` is run) and generate corresponding regular systemd service unit files»), y con `AutoUpdate=registry` —que «Requires a fully-qualified image reference (e.g., quay.io/podman/stable:latest) to be used to create the container»— el temporizador `podman-auto-update.timer` comprueba el registro, descarga la imagen nueva y reinicia el servicio.

Funciona, pero **es un modelo distinto**: en lugar de parches de seguridad de la distribución, se sigue la etiqueta móvil de una imagen de un tercero. Para un sistema que va a quedar desatendido meses, «lo que publique el mantenedor de la imagen en `:latest`» es una superficie de cambio menos predecible que «los parches de seguridad de Ubuntu». Y hay una limitación añadida: «Quadlet units do not support running as a non-root user by defining the User, Group, or DynamicUser systemd options».

### Lo demás también empuja en la misma dirección

*(Razonamiento propio.)* Con paquetes nativos, los registros van a journald y se consultan igual para todos los servicios; el estado se mira con `systemctl status`, que es lo único que un recién llegado ya sabe hacer; y no hay una capa de red y de volúmenes que entender antes de arreglar nada. Sobre el criterio de «explicable en una tarde», gana claramente.

**Recomendación:** paquetes nativos y unidades systemd para todo lo que esté empaquetado en Ubuntu (que, según el informe de [#3](https://github.com/bisite/FertLoops/issues/3), incluye PostgreSQL, y según [#4](https://github.com/bisite/FertLoops/issues/4), Mosquitto). Podman con quadlets queda como recurso para el componente que no esté empaquetado, no como modelo general.

## 2. Proxy inverso y terminación TLS con certificado emitido a mano

### nginx: el requisito de la cadena está documentado sin ambigüedad

Esto es lo que más equivocaciones causa con un certificado entregado a mano, y la documentación de nginx es explícita:

> «Specifies a `file` with the certificate in the PEM format for the given virtual server. If intermediate certificates should be specified in addition to a primary certificate, they should be specified in the same file in the following order: the primary certificate comes first, then the intermediate certificates.»

Es decir: **un único fichero, primero el certificado del servidor y después los intermedios**. Un fichero con solo el certificado de servidor produce el clásico fallo que aparece en unos clientes y no en otros. Detalles adicionales útiles:

- `ssl_certificate_key` apunta al fichero de la clave privada; la clave puede ir en el mismo fichero que el certificado, aunque no es buena práctica. La página no documenta requisitos de permisos: es responsabilidad nuestra restringirlos.
- `ssl_trusted_certificate` sirve para verificar respuestas OCSP y **no se envía a los clientes**, «In contrast to the certificate set by `ssl_client_certificate`».
- Si se activa `ssl_stapling` (por defecto `off`), hace falta que el certificado del emisor sea conocido: si `ssl_certificate` no incluye los intermedios, hay que ponerlos en `ssl_trusted_certificate`, y además configurar `resolver`.
- Desde 1.11.0 se puede declarar `ssl_certificate` varias veces para servir RSA y ECDSA a la vez, con la limitación de que solo OpenSSL ≥ 1.0.2 admite cadenas separadas por certificado.

### Caddy: su principal virtud es la que no podemos usar

Caddy admite certificado propio con `tls <cert_file> <key_file>` («Specifying just one is invalid»), y el certificado debe tener SAN que coincidan con la dirección del sitio. **Pero no he podido confirmar en su documentación que aportar un certificado propio desactive por completo la gestión automática ACME para ese sitio**, y la página consultada no cubre qué ocurre al sustituir el fichero del certificado ni cómo recargar sin cortar servicio.

Recomendar Caddy en un escenario **sin ACME** —cuando ACME es precisamente su razón de ser— y encima con ese punto sin verificar, sería recomendar lo desconocido. Queda descartado por ahora, no por defecto técnico sino por falta de verificación.

**Traefik** no se ha llegado a investigar en esta sesión: incógnita abierta.

### Aviso de caducidad

*(Receta propia, no producto documentado.)* Como la renovación es manual y de USAL, el riesgo real es que el certificado caduque sin que nadie se dé cuenta. Un temporizador de systemd que ejecute `openssl x509 -checkend <segundos> -noout -in <cert>` y falle cuando queden menos de, digamos, 21 días, combinado con el `OnFailure=` de la sección 4, da un aviso sin instalar nada nuevo. Conviene decidirlo en [#15](https://github.com/bisite/FertLoops/issues/15) junto con el resto de las alertas.

## 3. Copias de seguridad y restauración verificada

### restic

Distingue dos niveles de comprobación:

- `check` a secas valida «Structural consistency and integrity, e.g. snapshots, trees and pack files», sin leer los ficheros de datos, porque hacerlo «requires reading a copy of every pack file in the repository».
- `check --read-data` lee todos los *pack files* y verifica su integridad, con la advertencia de que «it might incur higher bandwidth costs than usual».
- `check --read-data-subset` permite verificación **rodante**: por grupos (`n/t`), por porcentaje aleatorio (`x%`) o por tamaño (`nS`).

Ese último punto es el que hace a restic cómodo aquí: se puede verificar, por ejemplo, un 10 % del repositorio cada noche y cubrirlo entero cada diez días, sin la factura de leerlo todo cada vez. La documentación es tajante sobre lo que significa un fallo: «If `check` reports an error in the repository, then you must repair the repository. As long as a repository is damaged, restoring some files or directories will fail.»

### borgbackup

Equivalente en capacidad. Su `check` separa comprobación de repositorio (cabeceras de segmento, CRC — detecta *bit rot*) y de archivo (consistencia de metadatos y presencia de los *chunks* referenciados), y `--verify-data` hace lo mismo que `--read-data` de restic:

> «The `--verify-data` option will perform a full integrity verification (as opposed to checking the CRC32 of the segment) of data, which means reading the data from the repository, decrypting and decompressing it. It is a complete cryptographic verification and hence very time consuming, but will detect any accidental and malicious corruption.»

Y un aviso que conviene copiar tal cual en el runbook: «`--repair` is a **POTENTIALLY DANGEROUS FEATURE** and might lead to data loss!», recomendando copiar el repositorio antes de intentar repararlo.

### El matiz que importa más que la elección de herramienta

**Ni `restic check --read-data` ni `borg check --verify-data` demuestran que una restauración sirva.** Verifican que los bytes guardados son los que se guardaron; no que el volcado de PostgreSQL que contienen se pueda cargar y consultar. El ticket pedía explícitamente cómo se *prueba* una restauración, y la respuesta honesta es que hay que hacerla: restaurar periódicamente el último volcado a una base de datos desechable y ejecutar una consulta de comprobación (por ejemplo, contar filas del último día y comparar con el original). Eso es un temporizador de systemd más un script de veinte líneas, y es la única prueba que vale.

**Recomendación:** restic, por el binario único y por `--read-data-subset`, más una restauración real periódica verificada con una consulta. Para PostgreSQL, volcados lógicos con `pg_dump` como material a respaldar (detalle a fijar en [#14](https://github.com/bisite/FertLoops/issues/14)).

## 4. Observabilidad mínima: tres capas que no se solapan

Cada capa detecta un fallo que las otras no ven:

### a) El proceso se murió → `OnFailure=` de systemd

> `OnFailure=`: «A space-separated list of one or more units that are activated when this unit enters the "failed" state.»

Con `OnFailureJobMode=` (por defecto `replace`) se controla cómo se encola la unidad de aviso. La forma habitual es una unidad plantilla `notify@.service` que manda un correo o un webhook, referenciada como `OnFailure=notify@%n.service` desde cada servicio.

**Matiz importante** *(razonamiento propio, apoyado en las dos citas siguientes)*: `OnFailure=` se activa cuando la unidad **entra en estado `failed`**, no en cada intento fallido. Con `Restart=on-failure`, un servicio que se cae y se levanta solo no llega a `failed`, y por tanto **no avisa**; solo lo hace cuando agota el límite de arranques, que se configura con `StartLimitIntervalSec=`/`StartLimitBurst=` («Units which are started more than burst times within an interval time span are not permitted to start any more»). Normalmente eso es lo que queremos —no avisar por un reinicio aislado— pero hay que saberlo, porque un servicio que se reinicia cada diez minutos indefinidamente puede no generar ni una alerta. La documentación consultada **no aclara explícitamente** esa interacción; conviene comprobarla empíricamente antes de confiar en ella.

### b) Dejan de llegar medidas → estado No Data de Grafana

Documentado en el informe de [#5](https://github.com/bisite/FertLoops/issues/5): Grafana tiene estados No Data y Error de primera clase y dispara `DatasourceNoData`. Cubre el caso en el que todo está «arriba» pero no entra dato, que es el fallo caro durante un ensayo. A esto se suma, gratis, el mensaje retenido `$SYS/broker/connection/<remote_clientid>/state` del bridge de Mosquitto documentado en [#4](https://github.com/bisite/FertLoops/issues/4).

### c) El servidor entero se calló → *dead man's switch* externo

Las dos capas anteriores viven **dentro** del VPS: si el VPS se apaga, no avisan. Un *dead man's switch* invierte la lógica: algo en el VPS hace un *ping* periódico a un servicio externo, y es **la ausencia del ping** lo que dispara la alarma.

Healthchecks implementa ese modelo: el cliente hace peticiones HTTP («pings») a una URL única —por UUID (`https://hc-ping.com/<uuid>`) o por *slug*— con puntos `/start` y `/fail` opcionales; el servicio «keeps silent» mientras todo va bien y «raises an alert as soon as a ping does not arrive on time», con un *grace time* configurable por comprobación. Tiene integraciones de correo, webhooks, SMS, Slack, PagerDuty y otras, y su documentación incluye sección de autoalojamiento con Docker.

*(Razonamiento propio.)* Para este proyecto encaja bien porque el *ping* puede colgarse del propio trabajo que ya tiene que ejecutarse (por ejemplo, el que verifica la copia de seguridad), de modo que un solo indicador cubre «el VPS está vivo **y** las copias se están haciendo».

### Secretos y configuración con gente rotando

No investigado en esta sesión más allá de constatar que systemd ofrece mecanismos de credenciales. Queda como incógnita; la parte de decisión corresponde a [#13](https://github.com/bisite/FertLoops/issues/13) y [#16](https://github.com/bisite/FertLoops/issues/16).

## Lo que no se ha podido determinar con fuentes primarias

- **Recarga de nginx sin cortar conexiones** tras sustituir el certificado: no se consultó la página de control de nginx en esta sesión. Es un comportamiento bien conocido, pero no citado aquí; verificar antes de escribir el runbook.
- **Si `tls <cert> <key>` desactiva por completo ACME en Caddy** para ese sitio, y cómo recarga tras cambiar el fichero. Es el motivo por el que Caddy queda descartado de momento.
- **Traefik**: no investigado.
- **Interacción exacta entre `OnFailure=`, `Restart=` y `StartLimitBurst=`**: la documentación no la explicita. Comprobación empírica pendiente.
- **Licencia de Healthchecks y límites de su nivel gratuito**: la documentación consultada confirma que es autoalojable pero no indica licencia ni límites del servicio alojado.
- **Reinicio de servicios tras actualizar bibliotecas** con `unattended-upgrades` (el caso `needrestart`): la página consultada no lo cubre.
- **Cifras de memoria** del conjunto de servicios en 4 GB: no hay fuente oficial para ninguno de los componentes; habrá que medirlo con la instalación real.

## Fuentes

- Ubuntu, actualizaciones automáticas de seguridad: <https://help.ubuntu.com/community/AutomaticSecurityUpdates>
- Podman, `podman-systemd.unit(5)` (quadlets): <https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html>
- nginx, `ngx_http_ssl_module`: <https://nginx.org/en/docs/http/ngx_http_ssl_module.html>
- Caddy, directiva `tls`: <https://caddyserver.com/docs/caddyfile/directives/tls>
- restic, trabajo con repositorios (`check`): <https://restic.readthedocs.io/en/stable/045_working_with_repos.html>
- borgbackup, `check`: <https://borgbackup.readthedocs.io/en/stable/usage/check.html>
- systemd, `systemd.unit(5)`: <https://man7.org/linux/man-pages/man5/systemd.unit.5.html>
- Healthchecks, documentación: <https://healthchecks.io/docs/>
