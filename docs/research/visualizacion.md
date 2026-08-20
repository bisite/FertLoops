# Herramienta de visualización de datos

Informe de investigación para el ticket [#5](https://github.com/bisite/FertLoops/issues/5) del mapa de wayfinding ([#1](https://github.com/bisite/FertLoops/issues/1)). Fuentes consultadas el 30 de julio de 2026. Se distingue explícitamente lo documentado de lo que es razonamiento propio, y se señala lo que no se ha podido verificar.

## Recomendación

**Grafana OSS**, configurado por ficheros versionados en el repositorio, no a mano por la interfaz.

Dos razones que pesan más que cualquier comparación de gráficas:

1. **Se puede reconstruir entero desde el repositorio.** Orígenes de datos, paneles y reglas de alerta son ficheros. Con estudiantes rotando, esto es la diferencia entre un sistema que se puede volver a levantar y una instalación que nadie se atreve a tocar porque su configuración solo existe en una base de datos.
2. **Detecta la *ausencia* de datos**, que es la alarma que este proyecto necesita de verdad: un gateway callado durante un ensayo es caro, y una gráfica plana no avisa.

Con una limitación importante que hay que tener en cuenta al decidir la superficie pública en [#12](https://github.com/bisite/FertLoops/issues/12): **los paneles compartidos públicamente no admiten variables de plantilla**, que es justo el mecanismo con el que se comparan zonas.

## Grafana OSS

### Configuración como código

Se aprovisiona por ficheros bajo un directorio `provisioning`, con un subdirectorio por tipo de recurso, y **todo esto está disponible en la edición OSS**:

- `provisioning/datasources/` — YAML. Recomendado con `editable: false`, de modo que nadie los cambie por la interfaz sin dejar rastro.
- `provisioning/dashboards/` — YAML que apunta a definiciones JSON de los paneles. «Each configuration file contains a list of `providers` that Grafana uses to load dashboards from the local filesystem.»
- `provisioning/alerting/` — reglas de alerta.
- `provisioning/plugins/` — YAML.

El comportamiento frente a ediciones manuales está documentado y es el que conviene: con `allowUiUpdates: true` los cambios hechos en la interfaz se persisten, pero «Grafana always overwrites the database dashboard with the one from the provisioning file» cuando el fichero cambia. Y hay borrado explícito: con `prune: true`, «Grafana also removes the provisioned data sources if you remove the provisioning file entirely».

Esto es exactamente lo que hace falta para que el repositorio sea la verdad y no un reflejo aproximado de lo que hay en el servidor.

### Detección de ausencia de datos

Grafana tiene estados **No Data** y **Error** de primera clase, soportados **solo para reglas gestionadas por Grafana** (no para reglas delegadas al origen de datos). El estado No Data se da cuando la consulta se ejecuta correctamente pero no devuelve ningún punto, y por defecto dispara una alerta `DatasourceNoData`. El comportamiento es configurable: se puede fijar la instancia a **Alerting**, **Normal**, **Error** o **Keep Last State**. Con periodo de espera (`pending period`) a 0 la transición es inmediata; si no, pasa por **Pending**.

Para este proyecto eso significa que «no ha llegado ninguna medida de la mesa 3 en 15 minutos» es una regla declarativa, versionable en `provisioning/alerting/`, sin escribir un vigilante propio. Es material directo para [#15](https://github.com/bisite/FertLoops/issues/15).

### Compartir fuera de la organización

La funcionalidad de paneles compartidos externamente («shared dashboards», antes «public dashboards») permite dar acceso sin que quien mira tenga cuenta en Grafana. Sus limitaciones documentadas son restrictivas y conviene leerlas antes de prometer nada de divulgación:

- **No admite variables de plantilla ni consultas que dependan de ellas.** Es la limitación que más nos afecta: un panel de comparación entre mesas de drenaje se construye con variables.
- No admite orígenes de datos de frontend, exemplars, Grafana Live ni streams en tiempo real, ni paneles de librería.
- De anotaciones solo admite el origen `-- Grafana --` con consulta «Annotations & Alerts».
- Los orígenes compatibles requieren `backend` y `alerting` habilitados; **PostgreSQL está entre los compatibles**, lo que encaja con la recomendación de [#3](https://github.com/bisite/FertLoops/issues/3).
- Es de solo lectura, y la documentación advierte que «Sharing your dashboard externally could result in a large number of queries to the data sources used» —el caché y la limitación de tasa que lo mitigan son de Enterprise.
- Aviso de seguridad relevante: las anotaciones por etiquetas pueden exponer datos de paneles no compartidos.
- La variante «solo para personas concretas» (invitación por correo) es de **Enterprise y Cloud**, y está en *private preview*.

Conclusión práctica: **si queremos una vista pública para divulgación, tiene que ser un panel deliberadamente sin variables**, distinto del panel de trabajo de los investigadores. Eso es una decisión de diseño para [#12](https://github.com/bisite/FertLoops/issues/12), no un impedimento.

### Licencia

Grafana está bajo **GNU Affero General Public License v3** (19 de noviembre de 2007), que es copyleft **de red**: obliga al operador de un servidor a ofrecer el código fuente de la versión modificada que esté ejecutando. Mientras lo despleguemos sin modificar el código, no añade obligaciones prácticas. Conviene dejarlo escrito para que nadie parchee Grafana y lo exponga en `fertloops.bisite.usal.es` sin darse cuenta de lo que eso implica.

### Lo que Grafana no va a resolver

*(Razonamiento propio, no documentado: verificar antes de cerrar [#12](https://github.com/bisite/FertLoops/issues/12).)* Grafana es una herramienta de lectura. El modo `supervised` del camino de control exige que **una persona confirme una acción de riego propuesta**, es decir una escritura con autoría y auditoría. Eso no es un panel. Existen plugins de botones y acciones, pero apoyarse en un plugin de terceros para la parte del sistema que abre válvulas es precisamente el tipo de dependencia que este proyecto quiere evitar. Es el argumento más fuerte a favor de que exista *algo* propio, aunque sea mínimo, y es la bifurcación que hay que resolver en [#12](https://github.com/bisite/FertLoops/issues/12).

## Alternativas

### Metabase

Herramienta de BI orientada a preguntas SQL y paneles. Encaja con una base de datos PostgreSQL, pero añade una pieza: **necesita su propia base de datos de aplicación**. Por defecto usa H2, y la documentación es explícita: «For production installations of Metabase we recommend that people replace the default H2 database with PostgreSQL», y «Avoid using this default database in production». Admite PostgreSQL, MySQL (≥ 8.4.0) o MariaDB (≥ 10.6.0) como base de aplicación, y el paso de H2 a PostgreSQL tiene «limited support» mediante un proceso de migración.

Valoración: es un producto sólido, pero para este caso significa mantener una base de datos adicional solo para los metadatos de la herramienta, y su terreno natural es la analítica de negocio, no la serie temporal con alarmas. Además, sus opciones de SSO (JWT, SAML, OIDC) son de pago, lo que puede importar en [#13](https://github.com/bisite/FertLoops/issues/13).

### Perses

**Descartado por incompatibilidad de orígenes de datos.** Es un proyecto *sandbox* de la CNCF, licencia Apache 2.0, con muy buena historia de *dashboard-as-code* (SDKs en Go y CUE, CLI, validación estática, librerías de CI/CD) —justo la filosofía que este proyecto quiere. Pero sus orígenes de datos son **Prometheus, Tempo, Loki y Pyroscope**: métricas, trazas, logs y perfiles. No hay origen SQL, así que no sirve para leer de PostgreSQL. Su modelo de datos «has reached a stable point», pero los SDKs «will likely evolve».

Merece la pena volver a mirarlo dentro de un par de años si añade orígenes SQL, porque su enfoque de configuración como código es superior al de Grafana.

### Apache Superset

**No verificado.** Se intentó consultar dos veces su documentación de arquitectura e instalación (páginas de *quickstart* y de arquitectura) y ambas devolvieron contenido vacío en esta sesión. No voy a describir sus requisitos de memoria en un informe pidiendo fuentes primarias, así que queda como incógnita explícita.

Lo único que puede afirmarse sin fuente es que su vía de instalación recomendada es basada en contenedores con varios servicios. Si alguien quiere considerarlo en serio, **hay que verificar antes cuántos procesos exige un despliegue de producción** (base de metadatos, caché, trabajadores asíncronos) y contrastarlo con los 4 GB del VPS compartidos con la base de datos y el proxy inverso.

## Comparativa

| | Grafana OSS | Metabase | Perses | Superset |
| --- | --- | --- | --- | --- |
| Lee de PostgreSQL | Sí | Sí | **No** | Sin verificar |
| Configuración como ficheros | Sí (OSS) | Parcial | Sí (su punto fuerte) | Sin verificar |
| Alerta por ausencia de datos | **Sí**, estado No Data | No es su terreno | No aplica | Sin verificar |
| Piezas nuevas que añade | 1 servicio | 1 servicio **+ su propia BD** | 1 servicio | Varias, sin verificar |
| Vista pública sin cuenta | Sí, **sin variables** | Limitado | — | Sin verificar |
| Licencia | AGPLv3 (copyleft de red) | Abierta + funciones de pago | Apache 2.0 | Sin verificar |
| Confirmar acciones (modo `supervised`) | No de forma nativa | No | No | Sin verificar |

## Consecuencias para otros tickets

- **[#12](https://github.com/bisite/FertLoops/issues/12) (superficie pública):** Grafana cubre bien a los investigadores del CIALE, pero (a) la vista pública tendría que ser un panel sin variables y (b) la confirmación de acciones del modo `supervised` no cabe de forma nativa. La bifurcación «Grafana autenticado» frente a «API y web propias» se puede plantear ya como «Grafana **más** una pieza propia mínima para las acciones», que es una tercera opción que no estaba sobre la mesa al redactar el ticket.
- **[#13](https://github.com/bisite/FertLoops/issues/13) (autenticación):** pendiente de verificar qué ofrece Grafana OSS en roles frente a Enterprise (ver incógnitas).
- **[#15](https://github.com/bisite/FertLoops/issues/15) (alertas):** el estado No Data más el mensaje retenido del bridge de Mosquitto documentado en [#4](https://github.com/bisite/FertLoops/issues/4) cubren entre los dos la detección de gateway callado sin código propio.
- **[#3](https://github.com/bisite/FertLoops/issues/3) (almacén):** compatible con la recomendación de PostgreSQL, que además está entre los orígenes admitidos para paneles compartidos.

## Lo que no se ha podido determinar con fuentes primarias

- **Requisitos y arquitectura de Apache Superset**: dos intentos de consulta devolvieron páginas vacías. Incógnita abierta.
- **Reparto exacto de roles y permisos entre Grafana OSS y Enterprise**: se sabe que existe aprovisionamiento de RBAC en Enterprise, pero no se ha verificado qué granularidad de roles ofrece la edición OSS. Importa para [#13](https://github.com/bisite/FertLoops/issues/13).
- **Requisitos de memoria de Grafana** en un VPS de 4 GB compartido: no se ha localizado una cifra oficial de requisitos mínimos.
- **Requisitos de Java y de memoria de Metabase**: la página de configuración de la base de datos de aplicación no los recoge.
- **Si algún plugin de acciones de Grafana es lo bastante sólido** para la confirmación del modo `supervised`: no investigado, y probablemente no convenga apoyarse en ello de todos modos.

## Fuentes

- Grafana, aprovisionamiento: <https://grafana.com/docs/grafana/latest/administration/provisioning/>
- Grafana, estados No Data y Error: <https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/nodata-and-error-states/>
- Grafana, gestión de datos ausentes: <https://grafana.com/docs/grafana/latest/alerting/guides/missing-data/>
- Grafana, paneles compartidos externamente: <https://grafana.com/docs/grafana/latest/dashboards/share-dashboards-panels/shared-dashboards/>
- Grafana, fichero `LICENSE`: <https://github.com/grafana/grafana/blob/main/LICENSE>
- Metabase, configuración de la base de datos de aplicación: <https://www.metabase.com/docs/latest/installation-and-operation/configuring-application-database>
- Metabase, instalación: <https://www.metabase.com/docs/latest/installation-and-operation/installing-metabase>
- Perses, `README.md`: <https://github.com/perses/perses/blob/main/README.md>
