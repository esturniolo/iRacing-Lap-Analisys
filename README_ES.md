# iRacing Lap Analyzer

> También disponible en inglés: [README.md](README.md)

Una aplicación de escritorio para analizar tu telemetría de iRacing. Carga tus archivos `.ibt`, consulta todos tus tiempos en una tabla, compara sesiones visualmente en gráficas y lleva un historial de tu rendimiento a lo largo del tiempo.

---

## Índice

- [¿Qué hace?](#qué-hace)
- [Descarga](#descarga)
- [¿Es segura? ¿Necesito instalar algo?](#es-segura-necesito-instalar-algo)
- [Cómo usarla](#cómo-usarla)
  - [Procesar tu propia telemetría](#procesar-tu-propia-telemetría)
  - [Abrir archivos IBT individuales](#abrir-archivos-ibt-individuales)
  - [Cargar la telemetría de un amigo](#cargar-la-telemetría-de-un-amigo)
  - [La pestaña Tabla](#la-pestaña-tabla)
  - [La pestaña Gráficas](#la-pestaña-gráficas)
- [¿Dónde están mis archivos de telemetría?](#dónde-están-mis-archivos-de-telemetría)
- [Preguntas frecuentes](#preguntas-frecuentes)

---

## ¿Qué hace?

- Lee archivos de telemetría `.ibt` de iRacing y extrae los datos vuelta a vuelta
- Muestra tiempos de vuelta, tiempos de sector, incidencias fuera de pista y vueltas en pit en una tabla
- Resalta tu mejor vuelta y colorea los deltas entre vueltas
- Guarda el historial de tus sesiones como archivos CSV para que puedas seguir tu progreso en el tiempo
- Permite comparar múltiples sesiones en gráficas (evolución de tiempos, comparación de sesiones, incidencias por vuelta)
- Soporta cargar los archivos `.ibt` de un amigo o compañero de equipo para comparar lado a lado

---

## Descarga

1. Ve a la página de [Releases](../../releases)
2. Descarga `iRacing-Lap-Analyzer.exe` del release más reciente
3. Ponlo en cualquier carpeta de tu PC — ya está listo

---

## ¿Es segura? ¿Necesito instalar algo?

**No requiere instalación.** El `.exe` es completamente portable — incluye Python y todas las dependencias empaquetadas dentro. Solo haz doble clic y ejecuta.

Windows puede mostrar una advertencia de SmartScreen la primera vez ("Windows protegió su equipo"). Esto ocurre porque la aplicación no está firmada con un certificado de pago. Haz clic en **Más información → Ejecutar de todas formas** para continuar.

La aplicación **no** se conecta a internet, no modifica ningún archivo de iRacing y solo lee los archivos `.ibt` que tú le indicas.

---

## Cómo usarla

### Procesar tu propia telemetría

Este es el flujo principal. Lee todos tus archivos `.ibt` y guarda los resultados como CSV en disco, construyendo un historial con el tiempo.

1. Haz clic en **Browse folder** bajo *My Telemetry* y selecciona la carpeta donde iRacing guarda tu telemetría (ver [¿Dónde están mis archivos de telemetría?](#dónde-están-mis-archivos-de-telemetría))
2. Opcionalmente cambia la *Output folder* donde se guardarán los CSV (por defecto: `Documents/iRacing-Lap-Analysis`)
3. Marca o desmarca **Only complete laps** según si quieres incluir vueltas sin tiempo registrado
4. Haz clic en **⚙ Process**
5. La aplicación escaneará todos los archivos `.ibt`, extraerá los datos y guardará un CSV por combinación de circuito+coche. Las sesiones ya guardadas se omiten automáticamente, así que puedes volver a hacer clic en Process tras tu próxima sesión y solo se añaden los datos nuevos.

### Abrir archivos IBT individuales

Haz clic en **Open IBT(s)…** bajo *My Telemetry* para seleccionar uno o más archivos `.ibt` directamente. Los datos se cargan en memoria y se muestran al instante — no se escribe ningún CSV en disco.

### Cargar la telemetría de un amigo

Haz clic en **Open IBT(s)…** bajo *External Telemetry* para cargar archivos `.ibt` de un amigo o compañero. Sus sesiones aparecen etiquetadas con `[EXT]` y se pueden comparar lado a lado en las pestañas Tabla y Gráficas.

### La pestaña Tabla

- Selecciona una sesión de la lista para ver todas sus vueltas
- Columnas: número de vuelta, tiempos de sector (S1, S2…), tiempo de vuelta, delta respecto a la vuelta anterior, incidencias fuera de pista, pit
- La **mejor vuelta** se resalta en morado
- Las **vueltas de pit** aparecen en gris
- Las celdas de **delta** están codificadas por colores: verde = más rápido, amarillo/naranja/rojo = más lento
- La barra de estadísticas en la parte inferior muestra la mejor vuelta, el promedio de vueltas limpias, el total de vueltas y el total de incidencias
- El panel derecho muestra una sesión externa (si se ha cargado) para comparación directa

### La pestaña Gráficas

Selecciona una o más sesiones (Ctrl+Click o Shift+Click para múltiples) y elige un tipo de gráfica:

- **Lap time evolution** — gráfica de línea con los tiempos de vuelta por número de vuelta, con tooltips al pasar el cursor
- **Session comparison** — gráfica de barras comparando los mejores tiempos entre sesiones
- **Off-tracks per lap** — gráfica de barras mostrando dónde ocurrieron las incidencias

---

## ¿Dónde están mis archivos de telemetría?

iRacing guarda los archivos `.ibt` aquí por defecto:

```
C:\Users\TuNombre\Documents\iRacing\telemetry\
```

Puedes confirmar la ruta en iRacing: *Options → Drive → Telemetry*.

Asegúrate de que la grabación de telemetría esté activada en las opciones de iRacing.

---

## Preguntas frecuentes

**La app se abre pero no pasa nada cuando hago clic en Process.**
Asegúrate de haber seleccionado una carpeta de telemetría y que contenga archivos `.ibt`.

**Aparece "No .ibt files found".**
Verifica que la carpeta seleccionada es la correcta (ver [¿Dónde están mis archivos de telemetría?](#dónde-están-mis-archivos-de-telemetría)).

**Mi sesión no aparece después de procesar.**
Si la sesión ya fue procesada antes, se omite para evitar duplicados. Elimina el CSV correspondiente en la carpeta de salida y vuelve a procesar.

**Los tiempos de sector están vacíos.**
Los tiempos de sector dependen de los datos de splits incluidos en el archivo `.ibt`. Algunos circuitos o tipos de sesión pueden no tener sectores grabados.

**Windows dice que la app no es segura.**
Haz clic en **Más información → Ejecutar de todas formas**. La aplicación no es malware — ver [¿Es segura?](#es-segura-necesito-instalar-algo).
