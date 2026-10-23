# 🪱 LoveWorm

Dashboard local para manejar Tinder: swipes con pausas, galería de tus matches ordenada por interés y
borradores de mensajes escritos por Claude que solo se envían cuando vos los aprobás.

## Qué hace

- **Bot de swipes**: likes y dislikes con un ratio y pausas al azar entre cada uno. Se puede detener en cualquier momento.
- **Galería**: una card por persona con todas sus fotos, cómo es según su bio y lo que contó, y de qué hablaron.
- **Orden por interés**: Claude le pone a cada persona un puntaje de 0 a 100. Arriba quedan quienes pasaron su
  Instagram o propusieron salir; abajo, quienes nunca respondieron.
- **Matches nuevos**: los matches sin chat, con su bio y un primer mensaje sugerido.
- **Borradores con aprobación**: Claude sugiere el primer mensaje o la respuesta siguiendo las instrucciones de la
  configuración. Nada se envía sin que toques **Enviar** y lo confirmes.

Instagram está en pausa: la web ya no expone los DMs por su API y la lectura por pantalla todavía no es confiable
(`INSTAGRAM_ENABLED` en `loveworm/dashboard/worker.py`).

## Cómo funciona

- Todo corre en tu máquina. El servidor escucha solo en `127.0.0.1:5000`.
- Abre un Chrome propio (`undetected-chromedriver`) con un perfil guardado en `chrome_profile/`. El login lo hacés
  vos a mano la primera vez; la contraseña nunca pasa por el código.
- Los chats y matches se leen con la API de Tinder Web usando esa sesión del navegador, con pausas entre requests,
  y solo se vuelven a bajar los chats que cambiaron.
- Los análisis y borradores usan el CLI de [Claude Code](https://claude.com/claude-code) (`claude -p`) con tu
  suscripción, sin herramientas y sin guardar sesión. Reciben solo el nombre, la bio y el texto del chat.
- Chats, análisis y configuración se guardan en `data/`, que no se sube al repo.

> ⚠️ Automatizar Tinder va contra sus términos de uso y puede terminar en el bloqueo de la cuenta. Usalo con
> moderación y bajo tu responsabilidad.

## Requisitos

- Windows, Python 3.12+ y Google Chrome
- [Claude Code](https://claude.com/claude-code) instalado y logueado (`claude` en el PATH)

## Uso

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m loveworm.dashboard
```

Abrí http://127.0.0.1:5000 y tocá **Iniciar**. Si no estás logueado, iniciá sesión en el Chrome que se abre
(Log in → Google). Después el dashboard trae tus chats y empieza a analizarlos.

`abrir_dashboard.ps1` levanta el servidor si no está corriendo y abre el dashboard; sirve para un acceso directo.

## Estructura

```
loveworm/
  dashboard/       servidor Flask, worker del bot y la página (templates/index.html)
  helpers/
    tinder_api.py  lectura de chats y envío de mensajes con la sesión del navegador
    llm.py         análisis de personas y borradores con `claude -p`
    instagram_web.py  lector de Instagram por pantalla (en pausa)
    ...            helpers de Selenium heredados de TinderBotz (login, swipes, perfil)
  session.py       sesión de Chrome y acciones del bot
```

## Créditos

Basado en [TinderBotz](https://github.com/frederikme/TinderBotz) de Frederik Mees (licencia MIT, ver `LICENSE.txt`).
