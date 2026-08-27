# Sherlock Web UI (fork de Tony Blanco)

Interfaz web propia sobre [Sherlock](https://github.com/sherlock-project/sherlock):
busca un nombre/usuario/email en **492 redes y plataformas** a la vez, con
búsqueda por variantes, exportación CSV/PDF, historial, filtro de falsos
positivos y visor de sitios soportados.

**Despliegue en vivo:** <https://sherlock-web-6e0h.onrender.com>
(plan free de Render — la instancia duerme tras 15 min sin tráfico; la
primera petición tras un descanso tarda ~40s en arrancar)

> ℹ️ Este proyecto se opera **100% en la nube**: no se usa servidor local.
> Todo cambio llega a producción con un `git push` a `master`.

---

## Arquitectura

```
┌────────────┐   push/merge a master   ┌─────────────────────────────┐
│   GitHub   │ ───────────────────────▶│  Render (Docker, free tier) │
│ TonyBlanco │   webhook de Render     │  build → deploy → live      │
│  /sherlock │                         │  https://sherlock-web-6e0h… │
└────────────┘                         └─────────────────────────────┘
        ▲                                        │
        │ workflow semanal (lunes 06:00 UTC)     │ subprocess:
        │ .github/workflows/                     │ python -m sherlock_project.sherlock
        │ sync-upstream-sites.yml                │ --local --print-all --csv
        ▼                                        │ (background thread + polling)
┌─────────────────────────┐                      ▼
│ sync_sites.py compara   │               /search (async)
│ con el upstream oficial │               /search-status/<user>
│ y abre un PR si hay     │               /results/<user> (caché)
│ sitios nuevos           │               /export/csv|pdf, /export/comparison/*
└─────────────────────────┘
```

Componentes:

| Pieza | Qué es |
|---|---|
| `web_app.py` | Flask + waitress (WSGI de producción, 6 hilos). Toda la API. |
| `templates/index.html` | UI única (vanilla JS): búsquedas, variantes, historial, export, dark mode, modal de sitios. |
| `Dockerfile` | Imagen de producción: Python 3.12-slim, dependencias de la web **y del paquete sherlock** (tomli, requests-futures, pandas…), usuario no-root. |
| `render.yaml` | Blueprint del servicio (plan free, Frankfurt, health check `/`). |
| `sync_sites.py` | Sincroniza `data.json` con el upstream: **añade** sitios nuevos, **nunca borra** los locales, backup automático. |
| `config/` | `false_positives.json` (lista servida por `/api/false-positives`) y `settings.json`. |
| `sherlock_project/` | El motor Sherlock original + `data.json` con 492 sitios (481 upstream + 11 propios). |

## Flujo de búsqueda (por qué es asíncrono)

El proxy de Render free corta las peticiones a los ~60s y una búsqueda
completa de 473 sitios tarda 60–110s. Por eso `POST /search` responde al
instante (`{"async": true}`), lanza el subprocess en un hilo de fondo y la UI
hace polling de `GET /search-status/<usuario>` hasta que termina. Los
resultados quedan en caché en disco (`cache/*.json`, TTL 8h) y las
re-búsquedas son instantáneas.

### Nota técnica clave (fix de OOM)

El `data.json` de cada sitio guarda su `request_future` durante toda la
búsqueda; cada `Future` retiene su `Response` completa (~1MB × 473 sitios ≈
470MB), lo que mataba el contenedor de 512MB a los ~90s. El fix
(`sherlock_project/sherlock.py`) libera la referencia tras procesar cada
sitio: el pico de RSS bajó de **535MB → 57MB** medido.

## Flujo upstream → PR → deploy (sync semanal)

1. **Detección** — Cada lunes 06:00 UTC (o con "Run workflow" manual en la
   pestaña Actions), GitHub Actions ejecuta
   `python sync_sites.py --update-existing` contra el `data.json` oficial de
   `sherlock-project/sherlock`.
2. **PR** — Si el upstream tiene sitios nuevos o entradas cambiadas, el
   workflow crea/actualiza la rama `chore/sync-upstream-sites` y abre un PR
   titulado `Sync upstream sites (fecha)` con el resumen del diff. Si no hay
   cambios, no hace nada ("Already in sync").
3. **Revisión** — El PR se revisa a mano: el diff debe mostrar **solo
   añadidos** (los sitios propios — Facebook, Weibo, Threads, Tagged… —
   jamás se eliminan) y opcionalmente entradas refrescadas.
4. **Merge** — Al aceptar el PR, GitHub hace un push del merge a `master`.
5. **Deploy automático** — El webhook de Render detecta el push a `master`
   (`autoDeploy: yes`, trigger `commit`) y reconstruye la imagen Docker.
   Unos 2–3 minutos después la nueva lista está viva: el contador del header
   y `/api/sites` reflejan el total nuevo.

**Evidencia de la cadena push→deploy (verificada en producción):** cada
commit pusheado en esta sesión (`c053bfa`, `570f044`, `d613514`, `401b9df`,
`2b1d849`, `5d65023`, `6f799ca`, `c680ab8`, `5b5b14b`, `7cd4f57`, `21b881c`,
`82dfd64`, `09ab468`) produjo exactamente un deploy en Render, incluido el
que añadió los 11 sitios a `data.json` (el header pasó a mostrar 492 sin
intervención manual). Un merge de PR a `master` es el mismo evento `push`,
por lo que el paso 5 no requiere configuración adicional.

Detalles que conviene saber:

- Los pushes a ramas **distintas de master** (como la rama del PR) **no**
  disparan deploy: Render solo sigue `master`.
- Cada deploy arranca un contenedor nuevo → la caché de resultados en disco
  se vacía (se reconstruye sola con la primera búsqueda de cada usuario).
- Si un deploy falla (p. ej. sintaxis rota), Render mantiene la versión
  anterior viva y el anterior deploy queda como `live`.

## Endpoints

| Ruta | Uso |
|---|---|
| `GET /` | La UI (el contador de sitios se renderiza una vez por proceso). |
| `POST /search` | Búsqueda async; responde `{"async": true}` o el resultado cacheado. |
| `GET /search-status/<u>` | Polling de la búsqueda async (404 si el contenedor se reinició a mitad). |
| `GET /results/<u>` | Resultados cacheados (camino rápido del historial). |
| `POST /search-variants` / `GET /search-progress/<id>` / `GET /search-variants-result/<id>` | Búsqueda por variantes de nombre/email con progreso en vivo. |
| `POST /search-multi` | Varios usernames a la vez. |
| `GET /export/csv/<u>`, `GET /export/pdf/<u>` | Exportación de resultados. |
| `GET /export/comparison/csv|pdf/<nombre>` | Matriz de comparación entre variantes. |
| `GET /api/sites` | **Salud del despliegue**: lista completa de sitios (nombre, URL, NSFW, tipo de check). 500 con el error si `data.json` está corrupto. |
| `GET /api/debug` | Filesystem del contenedor, validez de `data.json`, RSS de la app y de sus subprocessos. |
| `GET /api/false-positives` | Lista de sitios con falsos positivos conocidos (la consume el filtro de la UI). |
| `GET /health` | Liveness probe ultraligero (sin I/O). |

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `PORT` | `5000` | Puerto de escucha (Render lo inyecta). |
| `SHERLOCK_MAX_WORKERS` | `6` (en la imagen) | Concurrencia HTTP del subprocess. Con el fix de memoria, 6 cabe de sobra en 512MB. |

## Mantenimiento

- **Sitios nuevos upstream:** automáticos vía PR cada lunes (ver flujo arriba).
- **Workflows heredados:** `update-site-list.yml` del upstream está
  desactivado en forks con una guarda (`if: github.repository == …`) porque
  necesita secrets de los mantenedores. `sync-upstream-sites.yml` es el
  sustituto propio.
- **Modificar la lista a mano:** editar `sherlock_project/resources/data.json`
  y pushear; el deploy actualiza el total y `/api/sites` automáticamente.
