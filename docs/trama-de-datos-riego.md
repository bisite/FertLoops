# Trama ESP-32 ←→ RASPBERRY

En ese documento se describe la trama de datos usada entre el ESP32 y la Raspberry.

## Trama de datos completa

La Raspberry es capaz de solicitar datos al ESP32.
Tras recibir en la serial una string `"read\r\n"`, el dispositivo Slave retorna la siguiente trama.

```json
{
  "devID": "A4:CF:12:34:56:78",
  "Timestamp": "27/07/2026 10:52:55",
  "Control": {
    "Sample_per_minute": 6,
    "Inv": {
      "On": 1,
      "Freq": 50
    },
    "Restart": 0,
    "Valve": 56,
    "Debug": {
      "main": 5,
      "settings": 5,
      "adc_ads1263": 5,
      "wifi_app": 5,
      "dns_captive": 5,
      "webserver_app": 5,
      "rtc_ds1307": 5,
      "th_sensor": 5,
      "valve": 5,
      "pulse_counter": 5,
      "rs485_max3485": 5,
      "inverter_modbus": 5,
      "serial_protocol": 5
    }
  },
  "Data": {
    "pH": 7,
    "CE": 12345.67,
    "Solar": 845.65,
    "Volume": 20,
    "THC": {
      "T": 22.55,
      "H": 57.89,
      "C": 12345.67
    },
    "TH": {
      "T": 22.23,
      "H": 64.25
    },
    "Errors": {
      "ADC": 0,
      "Pulses": 0,
      "I2C": 0,
      "Inverter": 0,
      "Inverter_State": 0
    }
  }
}
```

## Campos de datos (read only)

Hay campos en el JSON que son de solo lectura.

| Campo | Descripción |
| --- | --- |
| `devID` | MAC del ESP32, formateado como `"AA:BB:CC:DD:EE:FF"` |
| `Timestamp` | Fecha y hora actual, leída del RTC (formato `"DD/MM/AAAA HH:MM:SS"`) |
| `Data.pH` | Dato del sensor de pH (0.0–14.0) `[N/A]` |
| `Data.CE` | Dato del sensor de conductividad (0.0–100000) `[ppm]` |
| `Data.Solar` | Dato del sensor de radiación solar (0.0–2000.0) `[W/m²]` |
| `Data.Volume` | Dato del contador de pulsos convertido en Litros (0–Inf) `[L]` |
| `Data.THC.T` | Dato de Temperatura del suelo (−20.0–150.0) `[°C]` |
| `Data.THC.H` | Dato de Humedad del suelo (0.0–100.0) `[%]` |
| `Data.THC.C` | Dato de Conductividad del suelo (0.0–20.0) `[mS/cm]` |
| `Data.TH.T` | Dato de Temperatura del aire (−20.0–150.0) `[°C]` |
| `Data.TH.H` | Dato de Humedad relativa del aire (0.0–100.0) `[%]` |
| `Errors.ADC` | Código de error en el ADC |
| `Errors.Pulses` | Código de error en el contador de pulsos |
| `Errors.I2C` | Código de error en la I2C |
| `Errors.Inverter` | Código de error del inversor |

### Tabla de errores

| Campo | Código | Descripción |
| --- | --- | --- |
| `Errors.ADC` | 0 | Sin error |
| | 1 | Fallo de inicialización |
| | 2 | Fallo de lectura |
| `Errors.Pulses` | 0 | Sin error |
| | 1 | Fallo de inicialización |
| | 2 | Fallo de lectura |
| `Errors.I2C` | 0 | Sin error |
| | 1 | Fallo de inicialización |
| | 2 | Fallo de lectura RTC |
| | 3 | Fallo de lectura TH |
| | 4 | Fallo de lectura RTC y TH |
| `Errors.Inverter` | 0 | Sin error |
| | 1 | Fallo de inicialización |
| | 2 | Fallo de comunicación |
| | 3 | Fallo de lectura de frecuencia |
| | 4 | Fallo de lectura de corriente |
| | 5 | Fallo de lectura de tensión |
| `Errors.Inverter_State` | 0 | Standby |
| | 1 | Forward running |
| | 2 | Reverse running |
| | 4 | Over-current (OC) |
| | 5 | DC over-current (OE) |
| | 6 | Input phase loss (PF1) |
| | 7 | Frequency overload (OL1) |
| | 8 | Under-voltage (LU) |
| | 9 | Overheat (OH) |
| | 10 | Motor overload (OL2) |
| | 11 | Interference (Err) |
| | 13 | External malfunction (ESP) |
| | 14 | Err3 |
| | 15 | Err2 |
| | 17 | Err4 |
| | 18 | OC1 |
| | 19 | PF0 |
| | 20 | Analog disconnected protection (AErr) |
| | 21 | EP3 |
| | 22 | Under-load (EP) |
| | 23 | PP |
| | 24 | Pressure control protection (Np) |
| | 25 | PID parameters set incorrectly (Err5) |
| | 45 | Communication timeout (CE) |
| | 49 | Watchdog fault (Err6) |
| | 52 | oPEn fault |
| | 54 | STO |
| | 55 | CE1 |
| | 72 | STO1 |

## Campos de control (read + write)

Hay campos que son read + write y con ellos la Raspberry es capaz de controlar algunos parámetros del ESP32.
Para controlar el dispositivo Slave, el Master envía la siguiente trama:

```json
{
  "Timestamp": "27/07/2026 10:52:55",
  "Control": {
    "Inv": {
      "On": 1,
      "Freq": 50
    },
    "Restart": 0,
    "Valve": 56,
    "Debug": {
      "serial_protocol": 5
    }
  }
}
```

### Descripción de los campos de control

| Campo | Descripción |
| --- | --- |
| `Timestamp` | Configura la fecha y hora del RTC (formato `"DD/MM/AAAA HH:MM:SS"`) |
| `Control.Inv.On` | Enciende o apaga el Inversor |
| `Control.Inv.Freq` | Frecuencia del Inversor (0–650 Hz) |
| `Control.Restart` | Fuerza un reinicio del ESP. El dispositivo Slave envía el último frame de datos seguido de `"Device restarting\r\n"` y luego reinicia el ESP |
| `Control.Valve` | Controla la apertura de la válvula (0–90) |
| `Control.Debug` | Controla el nivel de log (0–5) de uno o más TAGs, aplicado de inmediato. Se puede incluir cualquier subconjunto de TAGs, no es necesario enviarlos todos |

#### Niveles de `Control.Debug` (`custom_log_level_t`)

| Código | Nivel |
| --- | --- |
| 0 | Ninguno (sin logs) |
| 1 | Error |
| 2 | Warning |
| 3 | Info |
| 4 | Debug |
| 5 | Verbose |

TAGs disponibles: `main`, `settings`, `adc_ads1263`, `wifi_app`, `dns_captive`, `webserver_app`, `rtc_ds1307`, `th_sensor`, `valve`, `pulse_counter`, `rs485_max3485`, `inverter_modbus`, `serial_protocol`.

TAG especial `"all"` (no distingue mayúsculas/minúsculas): aplica el nivel indicado a todos los TAGs disponibles a la vez, por ejemplo:

- `{"Control":{"Debug":{"all":0}}}` → Silencia todos los logs (0: Ninguno).
- `{"Control":{"Debug":{"all":5}}}` → Pone todos los TAGs en Verbose (5).

Se puede combinar `"all"` con overrides de TAGs individuales en la misma trama; el orden de evaluación es el de aparición en el JSON (los TAGs individuales que aparezcan después de `"all"` sobrescriben el valor que `"all"` les asignó).

### Respuesta del Slave a la trama de control

Si la trama recibida es un JSON válido **y** todos los valores están dentro de su rango permitido (`Inv.On`/`Restart`: 0 o 1; `Inv.Freq`: 0–650; `Valve`: 0–90; `Debug.<TAG>`: 0–5 y `<TAG>` existente o `"all"`), el Slave la aplica y responde:

```
Ok\r\n
```

Si la línea recibida no es `"read"`, no es un JSON de control reconocible, o algún valor está fuera de rango, el Slave no aplica ningún cambio (toda la trama se rechaza) y responde:

```
Invalid command "<línea recibida>"\r\n
```

### Ejemplos de tramas de control

1. Encender el inversor a 45 Hz (no toca Valve/Restart/Debug):
   `{"Control":{"Inv":{"On":1,"Freq":45}}}`
   → Respuesta: `Ok\r\n`

2. Apagar el inversor:
   `{"Control":{"Inv":{"On":0}}}`
   → Respuesta: `Ok\r\n`

3. Sólo mover la válvula a 30°:
   `{"Control":{"Valve":30}}`
   → Respuesta: `Ok\r\n`

4. Sólo cambiar el nivel de log de un TAG (Warning en adelante para el inversor):
   `{"Control":{"Debug":{"inverter_modbus":2}}}`
   → Respuesta: `Ok\r\n`

5. Cambiar varios TAGs de Debug a la vez, junto con la válvula:
   `{"Control":{"Valve":60,"Debug":{"main":3,"webserver_app":1,"pulse_counter":0}}}`
   → Respuesta: `Ok\r\n`

6. Silenciar todos los TAGs a la vez con `"all"`:
   `{"Control":{"Debug":{"all":0}}}`
   → Respuesta: `Ok\r\n`

7. Poner todos los TAGs en Verbose, excepto `"wifi_app"` que se deja en Warning (`"wifi_app"` sobrescribe el valor asignado por `"all"` al aparecer después):
   `{"Control":{"Debug":{"all":5,"wifi_app":2}}}`
   → Respuesta: `Ok\r\n`

8. Forzar un reinicio del ESP:
   `{"Control":{"Restart":1}}`
   → Respuesta: se envía el último frame de datos, luego `"Device restarting\r\n"`, y el ESP reinicia.

9. Trama de solo lectura (no es control, pide el frame de datos actual):
   `read`
   → Respuesta: el frame JSON completo de datos (ver ejemplo al inicio de este documento).

10. Ejemplo de trama **rechazada** por valor fuera de rango (Freq admite 0–650, Valve 0–90):
    `{"Control":{"Inv":{"On":1,"Freq":900}}}`
    → Respuesta: `Invalid command "{"Control":{"Inv":{"On":1,"Freq":900}}}"\r\n`

11. Ejemplo de trama **rechazada** por un TAG de Debug inexistente:
    `{"Control":{"Debug":{"no_existe":3}}}`
    → Respuesta: `Invalid command "{"Control":{"Debug":{"no_existe":3}}}"\r\n`

12. Ejemplo de trama **rechazada** por un nivel de Debug fuera de rango (válido 0–5):
    `{"Control":{"Debug":{"valve":9}}}`
    → Respuesta: `Invalid command "{"Control":{"Debug":{"valve":9}}}"\r\n`
