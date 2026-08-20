# FertLoops

Sistema de fertirrigación de bucle cerrado para un módulo del invernadero del
CIALE (USAL). Un **Device** por mesa de drenaje lee los sensores y gobierna la
válvula y el inversor; un **Gateway** (Raspberry Pi) los agrega y sostiene una
cola local durante los cortes de enlace; un **VPS** almacena las medidas y las
presenta.

Hoy el repositorio contiene sobre todo **decisiones de arquitectura y el esquema
de la base de datos**. Todavía no hay código de aplicación.

## Por dónde empezar

| Si quieres… | Lee |
| --- | --- |
| Entender el vocabulario del dominio | [`CONTEXT.md`](CONTEXT.md) — **empieza aquí** |
| Saber por qué algo es como es | [`docs/adr/`](docs/adr/README.md) — una decisión por ADR |
| Poner en marcha la base de datos | [`db/migrations/`](db/migrations/README.md) |
| Ver el trabajo de lectura tras las decisiones | [`docs/research/`](docs/research/README.md) |
| Saber qué falta por decidir | [mapa de arquitectura](https://github.com/bisite/FertLoops/issues/1) |

`CONTEXT.md` es el punto de entrada de verdad: define con precisión los términos
que el resto de la documentación usa sin volver a explicarlos.

## Fuentes primarias

Dos documentos que **no son decisiones nuestras** y que mandan sobre cualquier
otra cosa escrita aquí:

- [`docs/fertloops-propuesta.md`](docs/fertloops-propuesta.md) — la propuesta del
  proyecto: objetivos, alcance y sensores monitorizados.
- [`docs/trama-de-datos-riego.md`](docs/trama-de-datos-riego.md) — el contrato
  UART acordado con el equipo de electrónica, **congelado y sin versionado**.

## Convenciones

Prosa en español; identificadores, rutas, órdenes, comentarios de código y
mensajes de commit en inglés. Ver [`AGENTS.md`](AGENTS.md).
