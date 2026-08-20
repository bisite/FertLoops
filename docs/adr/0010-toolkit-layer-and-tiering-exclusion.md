---
status: accepted
---

# TimescaleDB Toolkit como capa aparte

`timescaledb_toolkit` es una **extensión distinta** de `timescaledb`, con su propio paso de instalación. Vive en `db/migrations/extensions/timescaledb-toolkit/` como fuente independiente y no plegada dentro de la capa `timescaledb`, porque se puede querer hipertablas y compresión sin estas hiperfunciones. La migración es un único `CREATE EXTENSION`: no cambia la forma de ninguna tabla.

Desbloquea dos funciones bien ajustadas a este dominio:

- **`time_weight()`** — media ponderada en el tiempo. Importa porque el muestreo no es uniforme: el Gateway publica cada 60 s aunque el Device muestree cada 10 s, y tras un corte llegan lotes con huecos. Un `AVG(value)` plano desponderaría hacia los periodos que casualmente se muestrearon más.
- **`lttb()`** — submuestreo para gráficas, para dibujar la forma de un histórico largo sin transportar cada punto.

Las recetas están en el `QUERIES.md` de ese directorio, junto a un patrón basado en `heartbeat_agg` para «¿este sensor sigue reportando?». Ese patrón es deliberadamente una **consulta** sobre las marcas `observed_at` y no una columna almacenada, por el mismo razonamiento que la ausencia de campo de estado en `device` ([ADR-0006](0006-what-core-deliberately-does-not-model.md)).

## No se adopta el almacenamiento por niveles

Mover automáticamente los *chunks* viejos a almacenamiento de objetos es **exclusivo de los planes de pago de la nube del fabricante**; no existe equivalente autoalojado. Una migración que fallase en silencio fuera de ese producto no encaja aquí, y además el almacén corre en un contenedor sobre un VPS propio ([ADR-0001](0001-timescaledb-container-as-measurement-store.md)), donde la funcionalidad no está disponible en absoluto.

## Consecuencias

- **La imagen elegida sí trae el Toolkit: comprobado.** `CREATE EXTENSION` falla si la biblioteca compartida no está instalada, y en instalaciones autoalojadas no siempre viene, así que era una incógnita que había que cerrar. Sobre la imagen que fija [ADR-0001](0001-timescaledb-container-as-measurement-store.md) la migración se aplica y deja `timescaledb_toolkit` en la versión 1.23.0. La advertencia sigue valiendo para cualquier otra imagen.
- **Esta capa es opcional de verdad y hoy no la consume nada.** Sin código que ejecute consultas, `time_weight()` y `lttb()` son recetas en un fichero. Aplicarla no rompe nada, pero tampoco aporta hasta que exista un panel o un exportador que las use.
- **Deshacerla falla si algo depende de ella.** `DROP EXTENSION` no se aplica si queda alguna vista o función que use un tipo del Toolkit.
