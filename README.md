# WorkWorm 🪱

Tablero local que busca ofertas de trabajo, las ordena según tu perfil y, si lo activás, postula y sigue las conversaciones por correo con ayuda de Claude.

> **English:** WorkWorm is a local, Windows-only job-search dashboard (Streamlit + SQLite). It collects offers from several job boards and LinkedIn alerts, scores them against your profile and can run an autopilot that tailors your CV, applies by email or web form and follows up with recruiters through Gmail, using the Claude Code CLI. The UI and prompts are in Spanish.

## Qué hace

- **Junta ofertas** de Indeed (vía JobSpy), Get on Board, Computrabajo, Bumeran/ZonaJobs, Greenhouse y Lever (por empresa), Himalayas, Remotive, Jobicy y las alertas de LinkedIn que te llegan a Gmail.
- **Las puntúa** según cargo, seniority, tecnologías y ubicación. El puntaje es determinista y se configura en `config.json → scoring`.
- **Filtra** por palabras del título, empresas, años de experiencia, nube (opcional) y reglas escritas en lenguaje natural.
- **Adapta el CV y la carta** a cada aviso sin inventar experiencia (`tailor.py`).
- **Piloto automático**: postula por correo o completando el formulario en un navegador aparte, contesta a reclutadores, agenda entrevistas y manda un seguimiento si no hay respuesta.
- **Ordena tu bandeja**: etiqueta y archiva en Gmail los correos de la búsqueda.

Todo corre en tu computadora. No hay servidor ni cuenta de WorkWorm.

## ⚠️ Antes de usarlo

- Con el piloto automático activo, el agente **manda correos y postulaciones en tu nombre**. Revisá `profile.md`, las reglas de exclusión y el límite diario antes de activarlo.
- Algunas fuentes se leen con scraping de páginas públicas (Indeed, Computrabajo, Bumeran). Pueden dejar de funcionar si cambian los sitios.
- `linkedin_bot.py` abre avisos de LinkedIn en el navegador del agente para encontrar el link externo de postulación. Nunca postula en LinkedIn, pero automatizar LinkedIn puede ir contra sus términos de uso. Usalo bajo tu responsabilidad.
- Lo que haga el agente es responsabilidad de quien lo usa. El software se entrega «tal cual» (ver [LICENSE](LICENSE)).

## Requisitos

La versión actual funciona **solo en Windows 10/11**.

- Python 3.13 (la versión con la que se prueba).
- [Claude Code](https://claude.com/claude-code) instalado y con sesión iniciada: el agente usa `claude -p` con tu suscripción.
- Node.js 18 o superior (`npx` levanta Playwright MCP para completar formularios).
- Microsoft Edge (navegador del agente) y Microsoft Word (convierte el CV adaptado a PDF).
- Una cuenta de Gmail y un proyecto propio en Google Cloud para la API de Gmail.

## Instalación

```powershell
git clone https://github.com/WormAgent/workworm.git
cd workworm
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item profile.example.md profile.md
```

Después completá:

1. **`profile.md`**: tus datos, preferencias y límites. El agente lo lee antes de cada correo. Donde quede `COMPLETAR`, no inventa: te pregunta.
2. **`config.json`**: como mínimo `candidate_name`, `candidate_email`, `cvs` (rutas a tu CV en PDF y .docx), `search_terms`, `scoring` y `home`.

`config.json` y `profile.md` están en `.gitignore`: nunca se suben.

## Abrir el tablero

```powershell
.\start.ps1
```

Abrí `http://127.0.0.1:8501`. El tablero solo escucha en tu computadora. Para buscar ofertas, usá **Actualizar ofertas**.

Para que el tablero y el agente arranquen solos al iniciar sesión, registrá las tareas de Windows (`WorkWorm` y `WorkWorm - Tablero`):

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
```

## Conectar Gmail

1. En [Google Cloud Console](https://console.cloud.google.com/), creá un proyecto y habilitá **Gmail API**.
2. Configurá la pantalla de consentimiento OAuth como usuario externo y agregá tu correo como usuario de prueba.
3. Creá un cliente OAuth de tipo **Desktop app**, descargá el JSON y guardalo como `.secrets/credentials.json`.
4. En **Configuración** del tablero, pulsá **Autorizar Gmail en el navegador**. Los tokens quedan en `.secrets/`, fuera de Git.

WorkWorm usa dos conexiones (`config.json → gmail_accounts`):

- **`main`**: la cuenta con la que postulás y hablás con reclutadores.
- **`alerts`**: la que recibe las alertas de LinkedIn y manda los avisos de WorkWorm.

Pueden ser la misma cuenta. Se piden permisos de lectura, redacción y (en `main`) etiquetas. Durante la búsqueda no se envían correos.

Mientras la app de Google Cloud esté en modo **Testing**, Google invalida la autorización cada 7 días. Cuando pasa, el tablero muestra «Venció la autorización de Gmail» y el agente no lee ni manda correos hasta que la renueves en **Configuración → Gmail**. Para que no venza, en la pantalla de consentimiento OAuth pulsá **Publish app** (pasa a «In production»). No hace falta que Google la verifique para usarla vos: al autorizar vas a ver el aviso «Google no verificó esta app», elegí **Configuración avanzada → Ir a…**.

## Configuración

| Sección de `config.json` | Para qué sirve |
| --- | --- |
| `candidate_name`, `candidate_email`, `profile_path` | Quién sos y dónde está tu perfil. |
| `cvs` | Tus CV (PDF y .docx) por idioma. Se leen desde donde estén; no se copian al proyecto. |
| `search_terms` | Búsquedas que se lanzan en cada fuente. |
| `scoring` | Cargos buscados (`role_terms`), títulos principales, palabras de seniority, peso de cada skill y tecnologías que se muestran. |
| `home` | Tu país, ciudades, región, zona horaria y ubicaciones que no te sirven. Define la elegibilidad de las ofertas. |
| `indeed`, `getonbrd`, `computrabajo`, `bumeran`, `remote_boards`, `boards` | Fuentes y empresas de Greenhouse/Lever. Computrabajo y Bumeran son de Argentina. |
| `exclusions`, `cloud_filter` | Qué ofertas descartar. El filtro de nube (por ejemplo, solo Azure) está apagado por defecto. |
| `tailoring`, `language` | Reglas para adaptar CV y carta, e idioma. |
| `autopilot` | Horarios, límite diario, seguimientos y correo de avisos. |
| `mail_organizer` | Etiqueta y archivo de los correos de la búsqueda. |
| `lifeprime` | Integración opcional con un calendario vía MCP (apagada si no tiene `url`). |

La mayoría de estas opciones también se editan desde **Configuración** en el tablero.

## Cómo funciona el piloto automático

En cada ciclo (tarea `WorkWorm` o botón **Ejecutar ciclo ahora**), `agent.run()`:

1. **Busca ofertas** cada `collect_every_hours` horas y completa las descripciones que faltan.
2. **Postula** dentro de `apply_hours`, hasta el máximo diario. Claude decide si la oferta encaja, elige el CV y redacta el texto. Si el aviso tiene un correo, manda el CV desde Gmail. Si no, completa el formulario en un **Edge aparte** con perfil propio (`data/edge-profile`) y lo cierra al terminar. Si el sitio pide crear cuenta, captcha o un dato que no está en tu perfil, la oferta pasa a «Requiere tu atención».
3. **Sigue los correos** dentro de `reply_hours`: clasifica las respuestas y contesta en el mismo hilo. Acepta el horario de entrevista que propongan y responde con lo que dice `profile.md`. Ante una propuesta formal, un dato faltante o algo ambiguo, te avisa con un correo `[WorkWorm]`. Con `reply_to_recruiters: false` no contesta: le respondés vos.
4. **Hace un seguimiento** a los `followup_after_days` días sin respuesta, una sola vez. Con `followups: false` no manda seguimientos.

Las ofertas de LinkedIn no se autopostulan: quedan en «Postular en LinkedIn» con el link. Te postulás vos, pulsás «Ya me postulé» y el agente sigue la conversación por correo.

La primera vez, en **Configuración → Abrir Edge del agente**, iniciá sesión en los sitios donde vas a postular (Indeed, Get on Board, Computrabajo, Bumeran) y cerrá la ventana.

Estados: Nueva → CV enviado → En conversación / Requiere tu atención → Entrevista agendada → Propuesta, o Rechazada.

## Privacidad

Todo queda en tu computadora:

- `data/`: base SQLite con ofertas, postulaciones y correos; logs; CV adaptados; perfil del navegador del agente con sesiones iniciadas.
- `.secrets/`: credenciales y tokens OAuth de Gmail.
- `config.json` y `profile.md`: tus datos.

Las cuatro rutas están en `.gitignore`. No las publiques. El texto de las ofertas, tu perfil y tus CV sí se envían a Claude para decidir y redactar.

## Estructura

| Archivo | Rol |
| --- | --- |
| `app.py` | Tablero Streamlit. |
| `radar.py` | Fuentes, puntaje, base de datos y Gmail. |
| `agent.py` | Piloto automático: postulaciones, respuestas y seguimientos. |
| `tailor.py` | Adaptación del CV (.docx → PDF) y carta. |
| `llm.py` | Llamadas a `claude -p` con salida JSON validada. |
| `linkedin_bot.py` | Busca el link externo de avisos de LinkedIn. |
| `mail_organizer.py` | Etiqueta y archiva correos de la búsqueda. |
| `lifeprime.py` | Integración opcional de calendario. |
| `update.py`, `launcher.pyw`, `install_task.ps1`, `start.ps1` | Arranque y tareas de Windows. |

## Desarrollo

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Los tests no necesitan `config.json` ni `profile.md`: sin ellos usan `config.example.json`. Los aportes son bienvenidos. Abrí un issue antes de un cambio grande y agregá tests para lo que cambies.

## Licencia

[MIT](LICENSE)
