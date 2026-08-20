# Informes de investigación

Trabajo de lectura sobre fuentes primarias, hecho para alimentar una
decisión. **Un informe no es una decisión**: recomienda, y la elección se
toma después en el ADR correspondiente, que es el que manda si los dos
dicen cosas distintas.

| Informe | Alimentó | Decisión |
| --- | --- | --- |
| [Almacén de series temporales](almacen-series-temporales.md) | Elegir el almacén de medidas | [ADR-0001](../adr/0001-timescaledb-container-as-measurement-store.md) |
| [Transporte Pi ↔ VPS](transporte-pi-vps.md) | Elegir el transporte | [ADR-0002](../adr/0002-mqtt-mosquitto-bridge-as-pi-vps-transport.md) |
| [Despliegue y operación](despliegue-y-operacion.md) | Elegir el modelo de despliegue | [ADR-0003](../adr/0003-docker-compose-with-native-caddy-as-vps-deployment-model.md) |
| [Certificado en Caddy](certificado-caddy.md) | Verificar TLS sin ACME | [ADR-0003](../adr/0003-docker-compose-with-native-caddy-as-vps-deployment-model.md) |
| [Visualización de datos](visualizacion.md) | Elegir la herramienta de paneles | **sin decidir** — [#5](https://github.com/bisite/FertLoops/issues/5) |

En dos casos el ADR **invirtió** la recomendación del informe, y merece
saberlo antes de leerlos: ADR-0001 descartó PostgreSQL a secas que el
informe recomendaba, y ADR-0003 invirtió el modelo de paquetes nativos y
eligió Caddy en lugar de nginx. Los motivos están en cada ADR.
