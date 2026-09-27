# FairFill

Combustible para flotas de camiones en Argentina: dónde y cuánto cargar en cada viaje, y qué
cargas no cierran. Tiene tres partes:

- **`planner/`**: planificador de cargas por viaje (programación dinámica sobre precios reales de Energía).
- **`detector/`**: control de cargas que cruza tarjeta de combustible, GPS y sensor de nivel.
- **`fleetsim/`**: simulador de una flota que genera los datos que una empresa de transporte
  tendría en sus sistemas (tarjeta de combustible, TelePASE, GPS), con anomalías inyectadas y etiquetadas.

El simulador existe porque no hay datos públicos de flotas reales. Para que lo simulado se parezca
a la realidad, la geografía, las estaciones, los precios y las cabinas de peaje salen de fuentes reales.

## Simulador

## Uso

```bash
pip install -r requirements.txt
python -m fleetsim                 # usa config.toml
python -m fleetsim --refresh-data  # vuelve a descargar precios y peajes
```

Si existe la variable de entorno `ORS_API_KEY`, las rutas se piden a OpenRouteService con el perfil
`driving-hgv` y las dimensiones de cada camión. Si no, se usa el servidor demo de OSRM (perfil auto)
con un factor de tiempo para camión. Las rutas quedan en caché en `data/cache/routes/`.

## Fuentes reales

| Dato | Fuente |
|---|---|
| Estaciones, bandera, coordenadas y precio de gasoil | [Precios en Surtidor – Res. 314/2016](http://datos.energia.gob.ar/dataset/precios-en-surtidor), Secretaría de Energía (CC-BY-4.0) |
| Cabinas de peaje | OpenStreetMap, `barrier=toll_booth` vía Overpass (© colaboradores de OSM, ODbL) |
| Recorridos | OSRM demo u OpenRouteService, sobre datos de OSM |

## Salida (`output/<run_name>/`)

Lo que vería la empresa:

| Archivo | Equivale a |
|---|---|
| `trucks.csv` | Ficha técnica de cada unidad |
| `trips.csv` | Sistema de despacho (un registro por tramo) |
| `fuel_transactions.csv` | Resumen de la tarjeta de combustible, con odómetro tipeado por el chofer (incluye errores de tipeo) |
| `toll_crossings.csv` | Resumen de TelePASE |
| `gps.csv.gz` | Telemetría: posición, velocidad, encendido, odómetro, sensor de nivel de combustible (con ruido y pings perdidos) |
| `stations.csv` | Catálogo de estaciones con el precio base usado y de dónde sale |
| `routes.geojson` | Rutas usadas, para mapear |

La verdad, que un detector **no** debe usar para entrenar sin supervisión:

| Archivo | Contenido |
|---|---|
| `_ground_truth/anomalies.csv` | Anomalías inyectadas: `sobrecarga_tanque`, `carga_duplicada`, `sifonado`, `tarjeta_fuera_de_ruta` |
| `_ground_truth/truck_params.csv`, `driver_params.csv` | Eficiencia real de cada camión y cada chofer |
| `_ground_truth/price_increases.csv` | Fechas y magnitud de los aumentos por bandera |

## Cómo se simula

- **Consumo:** ficha técnica interpolada según la carga × eficiencia oculta del camión × eficiencia oculta del chofer × ruido por viaje.
- **Cargas:** debajo del 35% del tanque, el chofer para en la próxima estación del corredor (hasta 2 km de la ruta), con preferencia por la bandera de la tarjeta de flota.
- **Precios:** si la estación informó un precio en los últimos 60 días, se usa ese; si no, la mediana reciente de su bandera y provincia. Sobre eso se aplican aumentos escalonados: una bandera líder aumenta y las demás la siguen con demora. En septiembre de 2026 casi toda la red YPF tiene precios de más de un año, por eso existe esta estimación.
- **Peajes:** una cabina de OSM a menos de 200 m de la ruta cuenta como cruce; los nodos a menos de 2 km se agrupan en una sola cabina.
- **Jornada:** tope de horas de manejo por día, descanso, tiempo de descarga y días en base.

## Detector

```bash
python -m detector                     # sobre output/base
python -m detector --run output/seed7  # sobre otra simulación
python -m detector --no-eval           # sin leer la verdad, como en producción
```

Solo lee los datos de la empresa; `_ground_truth/` se usa únicamente en `detector/evaluate.py`.
Escribe `alerts.csv` (fraude y procedimiento, con explicación y pesos en riesgo), `info_events.csv`
(lo que parecía raro pero tiene explicación), `refuel_events.csv`, `sensor_calibration.csv`,
`consumption_model.csv`, `alerts_isolation_forest.csv` y `detector_report.json`.

### Cómo funciona

1. **Limpieza del sensor:** marca las lecturas trabadas (valores repetidos) y estima el ruido de cada camión.
2. **Cargas físicas:** detecta subidas de nivel con el camión detenido (`matching.refuel_events`).
3. **Emparejamiento:** asigna cada transacción a una carga física por lugar (≤ 3 km) y tiempo
   (hasta 14 h antes, por estaciones que informan tarde; hasta 30 min después, por relojes desfasados).
   Si la carga es de otro camión de la flota, es una tarjeta compartida.
4. **Calibración por camión:** ganancia del sensor y tolerancia, con las cargas de una sola transacción.
5. **Reglas y verificación por consumo:**

| Alerta | Pregunta | Anomalía |
|---|---|---|
| `tarjeta_lejos_del_camion` | ¿Algún camión de la flota estuvo en la estación? | Tarjeta usada por otro |
| `litros_no_reflejados` | ¿Subió el sensor lo que se facturó? | Sobrecarga |
| `carga_repetida` | ¿Entró al tanque lo facturado entre varias cargas seguidas? (descontando el equipo de frío) | Carga duplicada |
| `carga_mayor_al_consumo` | Con el sensor saturado: ¿lo facturado coincide con lo consumido desde el último tanque lleno? Regresión de Huber por camión: litros ~ km + t·km + horas de frío | Sobrecarga |
| `caida_con_motor_apagado` | ¿Bajó el promedio del nivel en una parada, más de lo que consume el frío? Test de dos medias | Robo |
| `tarjeta_compartida` | Procedimiento, no fraude | — |

### Resultados

Escenario `realista` (`python -m fleetsim --scenario realista`): sensores descalibrados, con
deriva y trabados; equipos de frío; tarjetas compartidas; estaciones que informan tarde; relojes
desfasados; sobrecargas desde 3% del tanque y robos graduales. La semilla 42 se usó para
desarrollar; 7, 123 y 2024 no se miraron.

| | Transacciones (P / R) | Isolation Forest (P / R) | Robos (P / R) | Tarjeta compartida |
|---|---|---|---|---|
| Base, 4 semillas | 1,00 / 0,77–0,92 | 0,83–1,00 / 0,76–0,82 | 1,00 / 0,92–1,00 | — |
| Realista, semilla 42 | 1,00 / 0,85 | 0,27 / 0,23 | 1,00 / 0,83 | 8/8 |
| Realista, 3 semillas no vistas | 0,87–0,96 / 0,72–0,93 | 0,31–0,40 / 0,28–0,43 | 1,00 / 0,84–1,00 | 12/12 |

Detector v1 (comparación antes/después de cada transacción) sobre el escenario realista:
precisión 0,22 en transacciones. La mayoría de los falsos positivos eran estaciones que
informaban tarde, cargas del equipo de frío y tarjetas compartidas.

Lecciones:
- En datos limpios, un modelo genérico parece competitivo; con fenómenos legítimos que se
  parecen a anomalías, el Isolation Forest cae a ~30% de precisión porque no puede saber que
  una carga del equipo de frío es normal. Entender el proceso vale más que el algoritmo.
- El recall pendiente está en sobrecargas chicas y en camiones con pocos pares
  "tanque lleno a tanque lleno" para ajustar el modelo de consumo.
- **Esto sigue siendo un simulador:** los fenómenos están modelados como yo los imaginé.
  Datos reales van a traer otros.

## Planificador de cargas

```bash
python -m planner --origen Córdoba --destino "Buenos Aires" --camion CAM-001 --carga-kg 25000 --litros-iniciales 250
python -m planner --origen Córdoba --destino "Rafaela, Santa Fe" --camion CAM-008 --solo-bandera YPF
python -m planner ... --comparar-sin-peajes   # requiere ORS_API_KEY
```

Para un viaje indica en qué estaciones cargar y cuántos litros, con la hora estimada de cada
parada, los peajes del camino y el ahorro frente a lo que suele hacer un chofer (cargar al bajar
del 35% en la primera estación, de la bandera preferida si hay). Escribe el plan en
`output/planes/<origen>_<destino>_<camión>.json` y un mapa en `.html`.

- **Lugares:** los del config o cualquier localidad vía la API [Georef](https://apis.datos.gob.ar/georef/api/localidades) ("Localidad, Provincia").
- **Consumo:** el modelo aprendido por el detector para ese camión (`consumption_model.csv` de la corrida indicada con `--run`), o la ficha técnica si no hay; más el equipo de frío si toma del tanque principal y va cargado; más un margen de 10%.
- **Precios:** los vigentes de Energía, con el descuento de la bandera preferida. Los que no se informan hace más de 60 días se estiman y llevan una prima de riesgo de 3% al decidir.
- **Optimización:** programación dinámica exacta sobre la estación más barata de cada tramo de 20 km. Usa el resultado clásico de que en el óptimo se llena el tanque o se carga justo para llegar a otra parada. Tiene en cuenta el costo de cada parada (tiempo y desvío) y el valor del combustible que sobra al llegar. Verificado contra fuerza bruta en `tests/test_planner.py`.
- **Resultados de ejemplo** (con precios del 25/09/2026): ahorro de 1–2% en la mayoría de los viajes y de 8% hacia Buenos Aires, donde hay estaciones con precio confirmado más bajo.

Limitaciones: con OSRM la ruta es de auto y no se puede pedir una ruta sin peajes (el servidor
demo no lo permite). Casi todas las paradas elegidas son YPF, cuyo precio se estima con apenas
~14 estaciones que informan al día: el plan es tan bueno como esa estimación. Tampoco considera
horarios de atención de las estaciones ni el tope de horas de manejo.

## API

```bash
pip install -r requirements.txt
python -m fleetsim --scenario realista && python -m detector --run output/realista   # datos de la flota y alertas
uvicorn api.main:app --reload                                                      # http://localhost:8000/docs
```

Backend en FastAPI sobre la misma lógica que las herramientas de terminal (`planner/service.py`).
Al arrancar carga una sola vez las estaciones con sus precios, las cabinas de peaje y el ruteador.
La documentación interactiva queda en `/docs`.

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/salud` | Estado: proveedor de rutas, estaciones y cabinas cargadas, corrida activa |
| `GET` | `/lugares?q=` | Autocompletado de localidades (config + Georef) |
| `GET` | `/camiones` · `/camiones/{id}` | Ficha técnica y consumo aprendido por el detector |
| `POST` | `/planes` | Plan de un viaje: ruta, paradas (dónde y cuánto cargar), peajes, costos y ahorro frente a la política habitual |
| `GET` | `/alertas?tipo=&camion_id=&desde=&hasta=` | Alertas del detector, las más recientes primero |
| `GET` | `/alertas/resumen` | Total, pesos en riesgo y conteos por tipo y por camión |

Ejemplo:

```json
POST /planes
{"origen": "Córdoba", "destino": "Buenos Aires", "camion_id": "CAM-001", "carga_kg": 25000, "litros_iniciales": 250}
```

Errores: `404` camión inexistente, `422` datos inválidos o lugar no encontrado, `502` falla del
ruteo (por ejemplo, `evitar_peajes` sin `ORS_API_KEY`), `503` falta generar la corrida.
Variables de entorno: `FAIRFILL_RUN` (corrida a usar; por defecto `output/realista`),
`FAIRFILL_CORS` (orígenes permitidos; por defecto `http://localhost:5173`, el de Vite),
`ORS_API_KEY` (rutas para camión).

Los tests de la API (`tests/test_api.py`) no usan la red: inyectan estaciones, peajes y rutas falsos.

## Supuestos y limitaciones

Los valores marcados como `SUPUESTO` en `config.toml` no están verificados. Los más importantes:

- **Tarifas de peaje:** no hay dataset público. Son aproximaciones a partir de notas de prensa sobre la Res. 248/2026, con la misma tarifa en todas las cabinas. Reemplazar con los cuadros oficiales por empresa operadora.
- **Sentido de cobro:** se cobra en ambos sentidos. Algunas cabinas reales cobran en uno solo.
- **Detección de cabinas:** una cabina de una ruta paralela o transversal a menos de 200 m puede contarse por error.
- **OSRM** usa el perfil de auto: no respeta restricciones de altura ni de peso. Con ORS sí, en la medida en que OSM las tenga cargadas.
- **Consumo:** no depende de la pendiente ni de la velocidad.
- **Normativa de jornada de conducción:** no verificada.

## Licencia

Código bajo licencia [MIT](LICENSE). Los datos que descarga tienen sus propias licencias: precios de la Secretaría de Energía (CC-BY-4.0) y OpenStreetMap (ODbL).
