# Certificado TLS manual en Caddy: ACME, recarga y formato de la cadena

Informe de investigación para el ticket [#21](https://github.com/bisite/FertLoops/issues/21), que retoma tres puntos sobre Caddy que el informe de [despliegue y operación](despliegue-y-operacion.md) (ticket [#6](https://github.com/bisite/FertLoops/issues/6)) dejó explícitamente sin verificar. Fuentes consultadas el 19 de agosto de 2026. Se distingue lo documentado (con cita textual y URL) de lo que es razonamiento propio o inferencia a partir del código fuente, y se señala lo que sigue sin poder determinarse.

Recordatorio del contexto: el certificado lo emite USAL a mano, sin ACME. Esto no cambia la decisión de fondo tomada en [#6](https://github.com/bisite/FertLoops/issues/6); este ticket solo cierra las tres incógnitas que quedaron abiertas sobre cómo se comporta Caddy en ese escenario.

## 1. ¿`tls <cert_file> <key_file>` desactiva ACME por completo, o queda algo automático de fondo?

### Lo documentado

La página de la directiva `tls` es escueta sobre el formulario manual:

> «`<cert_file>` and `<key_file>` are the paths to the certificate and private key PEM files. Specifying just one is invalid.»
> — <https://caddyserver.com/docs/caddyfile/directives/tls>

No dice nada sobre ACME ni sobre OCSP en esa misma página. Pero la página de HTTPS automático sí lo dice, y sin ambigüedad, en la lista de condiciones que impiden que Caddy active la gestión automática:

> «Any of the following will prevent automatic HTTPS from being activated, either in whole or in part: […] Manually loading certificates (unless `ignore_loaded_certificates` is set)»
> — <https://caddyserver.com/docs/automatic-https>

Es decir: **está documentado que cargar el certificado a mano desactiva el HTTPS automático (emisión y gestión ACME) para ese sitio**, salvo que se marque explícitamente `ignore_loaded_certificates`. No hemos podido obtener el texto exacto de la página de referencia de ese campo (`ignore_loaded_certificates`): la página se sirve con el contenido del campo generado dinámicamente y no se renderizó en la petición, así que solo tenemos el nombre y su efecto inferido por el contexto de la lista anterior.

### Lo que no dice la documentación de Caddy, pero sí el código fuente de certmagic

Caddy delega toda la gestión de certificados en su propia librería, certmagic. Su README es explícito sobre un punto que el ticket pedía verificar, el de OCSP:

> «Keep in mind that unmanaged certificates are (obviously) not renewed for you, so you'll have to replace them when you do. However, OCSP stapling is performed even for unmanaged certificates that qualify.»
> — <https://github.com/caddyserver/certmagic/blob/master/README.md>

"Unmanaged" es el término interno de certmagic para un certificado cargado a mano (el que resulta de `tls <cert_file> <key_file>`), frente a "managed" (obtenido y renovado por ACME). El código confirma la afirmación del README con dos comportamientos distintos:

- La renovación ACME se salta explícitamente para los certificados manuales, en `RenewManagedCertificates`:

  ```go
  for certKey, cert := range certCache.cache {
      if !cert.managed {
          continue
      }
  ```
  — <https://github.com/caddyserver/certmagic/blob/master/maintain.go> (función `RenewManagedCertificates`)

- Pero la actualización de OCSP staple, `updateOCSPStaples`, recorre **todos** los certificados de la caché sin filtrar por `cert.managed`, y solo descarta los caducados o "sintéticos": `if cert.Leaf == nil || cert.Expired() { continue }`. El resto —managed o unmanaged— entra en la cola de refresco de staple si su respuesta OCSP no está fresca. Esta función se llama en `maintainAssets`, una gorutina de fondo que arranca «once per cache» y que usa un temporizador propio para OCSP (`DefaultOCSPCheckInterval = 1 * time.Hour`) independiente del de renovación (`DefaultRenewCheckInterval = 10 * time.Minute`).
  — <https://github.com/caddyserver/certmagic/blob/master/maintain.go>

**Conclusión (razonamiento propio a partir del código, no de una página de documentación que lo enuncie así):** con un certificado cargado a mano, Caddy no intentará jamás emitirlo ni renovarlo por ACME —eso está desactivado y documentado—, pero **sí sigue habiendo una gorutina de fondo que, cada hora por defecto, intentará refrescar el OCSP staple de ese certificado si contiene una URL de respondedor OCSP** (el propio README lo llama «certificates that qualify»; el código no aclara del todo qué campo determina "qualify" más allá de que el staple no esté ya fresco). Esto implica una llamada de red saliente periódica al respondedor OCSP del emisor, algo a tener en cuenta si el VPS tiene salida a Internet restringida.

## 2. ¿Cómo recarga Caddy un certificado sustituido, sin cortar conexiones?

### Lo documentado

El mecanismo oficial es `caddy reload` (o el equivalente `POST /load` de la API de administración):

> «Gives the running Caddy instance a new configuration. This has the same effect as POSTing a document to the /load endpoint, but this command is convenient for simple workflows revolving around config files.»
> — <https://caddyserver.com/docs/command-line#caddy-reload>

Y la página de arquitectura explica por qué esto no corta el servicio: las recargas son atómicas y se solapan brevemente.

> «A config reload works by provisioning the new modules, and if all succeed, the old ones are cleaned up. For a brief period, two configs are operational at the same time.»
> — <https://caddyserver.com/docs/architecture>

> «No interruption to running services […] All reloads are atomic, consistent, isolated, and mostly durable ("ACID")»
> — <https://caddyserver.com/docs/architecture>

Sustituir solo el fichero en disco **no basta por sí solo**: si la configuración (Caddyfile o JSON) que se le vuelve a dar a `caddy reload` es textualmente idéntica a la que ya tiene cargada —lo habitual si solo cambia el contenido del fichero de certificado, no su ruta—, Caddy puede no reprovisionar nada. Para ese caso concreto existe la opción `--force`, y la documentación la vincula explícitamente a este escenario:

> «`--force` will cause a reload to happen even if the specified config is the same as what Caddy is already running. Can be useful to force Caddy to reprovision its modules, which can have side-effects, for example: reloading manually-loaded TLS certificates.»
> — <https://caddyserver.com/docs/command-line#caddy-reload>

La señal `SIGUSR1` está documentada como alternativa sin usar la API:

> «When a signal to reload config (SIGUSR1) is received, it acts like a forced config reload (i.e. reload anyway even if the config text is unchanged) which may reload dependent files like TLS certificates from disk. Signal-based config reloads are only enabled if Caddy is started with `caddy run` with a config file.»
> — <https://caddyserver.com/docs/command-line#caddy-reload>

(La misma página, en otro punto sobre por qué no hay que detener el servidor para reconfigurar, describe SIGUSR1 de forma algo más floja como que «has the same effect as caddy reload with the currently loaded config»; ambas frases están en la documentación oficial y no las hemos podido reconciliar del todo, pero la cita anterior —más específica sobre certificados— es la que responde a la pregunta del ticket.)

Sobre vigilancia de ficheros: la única opción de "watch" documentada es la bandera `--watch` de `caddy run`/`caddy start`, y vigila el **fichero de configuración**, no los certificados, y está marcada explícitamente como no apta para producción:

> «`--watch` will watch the config file and automatically reload it after it changes. ⚠️ This feature is intended for use only in local development environments!»
> — <https://caddyserver.com/docs/command-line#caddy-reload>

Esto confirma lo que planteaba el ticket: no hay ningún mecanismo documentado de vigilancia de fichero específico para certificados, ni en producción ni fuera de ella.

### Lo que dice el código y los reportes de la comunidad, y que matiza lo anterior

Aquí es donde aparece la discrepancia más importante para el runbook. Hay un *issue* abierto y sin resolver en el repositorio de Caddy, exactamente sobre este caso de uso, donde el propio autor principal (`mholt`) reconoce que la documentación de `--force` no se corresponde con el comportamiento real:

> «That might be because our cert cache aggressively reuses certificates across reloads, to make them more efficient. I marked this as a bug because it might be unexpected behavior, but to be fair, `--force` is only documented as reloading the Caddy config, not necessarily the certificates in external files. "Fixing" this "bug" will involve a potentially significant performance hit when `--force` is used, if lots of certificates are being loaded.»
> — <https://github.com/caddyserver/caddy/issues/6789> ("Caddy reload --force won't recache certificates", abierto)

Varios usuarios confirman en ese mismo hilo que tuvieron que reiniciar el contenedor/proceso completo porque `reload --force` no recogió el certificado sustituido. Hay además un *feature request* abierto, también sin resolver, pidiendo justo la vigilancia de fichero por `fsnotify` que la documentación no ofrece:

> «Would it make sense to change Caddy to use something like https://github.com/fsnotify/fsnotify to automatically reload TLS files when they change? If not, would it be possible to implement this as a plugin?»
> — <https://github.com/caddyserver/caddy/issues/6933> ("Automatic reload of TLS certificates from filesystem", abierto)

Y, en un *issue* ya cerrado sobre otro efecto colateral de las recargas, el mismo mantenedor confirma que el "sin interrupción" de la página de arquitectura tiene un matiz: durante una recarga puede haber un breve intervalo en el que un *handshake* TLS falle si el certificado de ese sitio todavía no se ha vuelto a leer de disco/almacenamiento, aunque las conexiones ya establecidas con la configuración vieja siguen atendiéndose sin cortarse:

> «When config is reloaded, the new server is started before the old one is stopped. The old one continues to serve requests until the new server is verified loaded and is ready to go. While this process doesn't wait for all the certificates to load, it does ensure that configuration is correct, listeners are ready, […]»
> — <https://github.com/caddyserver/caddy/issues/5589> ("Certificate cache is flushed when new config is loaded, causing downtime", cerrado)

**Conclusión:** el mecanismo oficialmente documentado es `caddy reload --force` (o `SIGUSR1`, con las mismas reservas). Pero, a fecha de esta consulta, hay un reporte abierto y no resuelto —confirmado por el propio autor como comportamiento real, aunque "no es exactamente un bug" porque no está prometido en la documentación— de que ese `--force` **no siempre recachea un certificado manual sustituido**. Para el runbook esto significa que no basta con documentar "ejecutar `caddy reload --force` tras sustituir el certificado": hay que **probarlo empíricamente con la versión de Caddy que se vaya a desplegar**, y tener como plan B un reinicio completo del servicio si la recarga forzada no recoge el cambio.

## 3. ¿Qué formato de fichero espera Caddy para el certificado con intermedios?

### Lo documentado

Ninguna de las páginas de referencia (la directiva `tls` del Caddyfile, ni la referencia JSON del módulo de carga de ficheros) menciona el orden de los certificados dentro del fichero. La directiva `tls` solo dice, como ya se citó en la sección 1, que `<cert_file>` es la ruta al fichero PEM del certificado, sin más detalle sobre su contenido.

### Lo que se puede determinar leyendo el código fuente de Caddy y de Go

La directiva `tls <cert_file> <key_file>` del Caddyfile construye, en el adaptador de configuración de Caddy, un par `CertKeyFilePair` que alimenta el módulo `caddytls.FileLoader` (id de módulo `tls.certificates.load_files`):

```go
fileLoader = append(fileLoader, caddytls.CertKeyFilePair{
    Certificate: certFilename,
    Key:         keyFilename,
    Tags:        []string{tag},
})
```
— <https://github.com/caddyserver/caddy/blob/master/caddyconfig/httpcaddyfile/builtins.go>

Ese módulo, al cargar los certificados, delega directamente en la librería estándar de Go:

```go
case "pem":
    cert, err = tls.X509KeyPair(certData, keyData)
```
— <https://github.com/caddyserver/caddy/blob/master/modules/caddytls/fileloader.go>

Y la implementación de `tls.X509KeyPair` en la librería estándar de Go recorre el fichero de certificado bloque a bloque y va añadiendo cada bloque `CERTIFICATE` **en el orden en que aparece en el fichero**, sin reordenar nada:

```go
for {
    certDERBlock, certPEMBlock = pem.Decode(certPEMBlock)
    if certDERBlock == nil {
        break
    }
    if certDERBlock.Type == "CERTIFICATE" {
        cert.Certificate = append(cert.Certificate, certDERBlock.Bytes)
    }
    ...
}
```
— <https://github.com/golang/go/blob/master/src/crypto/tls/tls.go> (función `X509KeyPair`)

Y la propia documentación de la librería estándar de Go especifica qué orden debe tener esa cadena para ser válida en el *handshake*:

> «A Certificate is a chain of one or more certificates, leaf first.»
> — <https://pkg.go.dev/crypto/tls#Certificate>

**Conclusión (encadenando las tres fuentes anteriores, no una afirmación única y explícita de Caddy):** Caddy no autodetecta ni reordena la cadena. El fichero que se pasa como `<cert_file>` debe ser **un único fichero PEM, con el certificado del servidor primero y los intermedios después**, exactamente la misma convención que ya documenta nginx (citada en el informe de [#6](despliegue-y-operacion.md)). No hay una vía separada para los intermedios en la directiva `tls` a secas: la única alternativa que ofrece la sintaxis del Caddyfile es la subdirectiva `load`, que carga desde una carpeta «PEM files that are certificate+key bundles» (mismo formato de *bundle*, no dos ficheros separados).

### Un matiz sin resolver en certmagic

Hay un *issue* abierto en el repositorio de certmagic, sin resolver a fecha de esta consulta, en el que un usuario reporta que, pasando una cadena completa y ordenada (hoja, intermedio, raíz) a `CacheUnmanagedCertificatePEMBytes`, el servidor solo terminaba sirviendo el primer certificado a algunas herramientas de comprobación SSL, aunque los navegadores lo aceptaban sin problema:

> «Despite passing the full certificate chain to certmagic, the server returns only the first certificate and omits intermediary and root certificates. Browsers are handling this correctly because they retrieve the missing certificates, but when I use SSL tools, they also throw errors.»
> — <https://github.com/caddyserver/certmagic/issues/308> ("CacheUnmanagedCertificatePEMBytes returns only the first certificate...", abierto)

El mantenedor no pudo reproducirlo con su propia cadena («Well that's the thing, I can generate my own cert chain and everything works as expected») y el hilo quedó pendiente de una cadena de prueba concreta del reportante, sin cerrarse. Esto no contradice la conclusión anterior —el caso general (hoja primero, intermedios después, en un solo fichero) funciona—, pero sí es una señal de que conviene **verificar la cadena servida con una herramienta externa (`openssl s_client -showcaseerts`, o un comprobador SSL en línea) tras desplegar el certificado de USAL**, y no fiarse solo de que el navegador lo acepte, precisamente porque los navegadores completan cadenas incompletas por su cuenta y pueden esconder un fichero mal formado.

## Lo que no se ha podido determinar con fuentes primarias

- El texto exacto del campo `ignore_loaded_certificates` en la referencia JSON: la página no devolvió el contenido generado dinámicamente en esta sesión. Su efecto se infiere del contexto de la lista de condiciones de `automatic-https`, no de una cita textual de esa página en concreto.
- Qué criterio exacto determina que un certificado manual «qualifies» para el mantenimiento de OCSP staple más allá de no estar caducado ni ser "sintético"; no se rastreó `stapleOCSP()` línea a línea.
- Si `caddy reload --force` o `SIGUSR1` recogen de forma fiable un certificado manual sustituido **en la versión concreta de Caddy que se vaya a instalar**: el código y la documentación consultados corresponden a la rama `master` del repositorio a fecha de hoy, y el propio *issue* #6789 sigue abierto sin una versión de referencia en la que esté arreglado o confirmado como comportamiento esperado. Esto exige una prueba empírica antes de escribir el runbook definitivo.
- La resolución del *issue* #308 de certmagic sobre cadenas con root incluida: sigue abierto, sin diagnóstico confirmado.
- Si existe alguna diferencia de comportamiento entre `tls <cert_file> <key_file>` y la subdirectiva `load <carpeta>` más allá de la forma de indicar la ruta: ambas acaban en el mismo tipo de módulo (`caddytls.FileLoader`/`FolderLoader`) usando el mismo `tls.X509KeyPair` según lo visto en `fileloader.go`, pero no se inspeccionó `folderloader.go` en detalle.

## Fuentes

- Caddy, directiva `tls` del Caddyfile: <https://caddyserver.com/docs/caddyfile/directives/tls>
- Caddy, HTTPS automático: <https://caddyserver.com/docs/automatic-https>
- Caddy, referencia JSON `ignore_loaded_certificates`: <https://caddyserver.com/docs/json/apps/http/servers/automatic_https/ignore_loaded_certificates/>
- Caddy, línea de comandos (`caddy reload`, `--force`, `SIGUSR1`, `--watch`): <https://caddyserver.com/docs/command-line#caddy-reload>
- Caddy, arquitectura (recargas de configuración): <https://caddyserver.com/docs/architecture>
- Caddy, código fuente, `caddyconfig/httpcaddyfile/builtins.go`: <https://github.com/caddyserver/caddy/blob/master/caddyconfig/httpcaddyfile/builtins.go>
- Caddy, código fuente, `modules/caddytls/fileloader.go`: <https://github.com/caddyserver/caddy/blob/master/modules/caddytls/fileloader.go>
- Caddy, *issue* #6789, "Caddy reload --force won't recache certificates" (abierto): <https://github.com/caddyserver/caddy/issues/6789>
- Caddy, *issue* #6933, "Automatic reload of TLS certificates from filesystem" (abierto): <https://github.com/caddyserver/caddy/issues/6933>
- Caddy, *issue* #5589, "Certificate cache is flushed when new config is loaded, causing downtime" (cerrado): <https://github.com/caddyserver/caddy/issues/5589>
- Caddy, *issue* #5224, "Caddy removes manually generated certificate / key" (cerrado): <https://github.com/caddyserver/caddy/issues/5224>
- certmagic, README: <https://github.com/caddyserver/certmagic/blob/master/README.md>
- certmagic, código fuente, `maintain.go`: <https://github.com/caddyserver/certmagic/blob/master/maintain.go>
- certmagic, código fuente, `certificates.go`: <https://github.com/caddyserver/certmagic/blob/master/certificates.go>
- certmagic, *issue* #308, "CacheUnmanagedCertificatePEMBytes returns only the first certificate..." (abierto): <https://github.com/caddyserver/certmagic/issues/308>
- Go, paquete `crypto/tls`, tipo `Certificate` y función `X509KeyPair`: <https://pkg.go.dev/crypto/tls#Certificate>
- Go, código fuente, `src/crypto/tls/tls.go`: <https://github.com/golang/go/blob/master/src/crypto/tls/tls.go>
