"""Bot worker: owns the single Chrome session and runs every browser job on one thread."""
import json
import queue
import random
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from loveworm.helpers import citas, estilo, fisico, swipes
from loveworm.helpers.cupos import MAX_ACTIVAS, Cupos
from loveworm.helpers.instagram_web import InstagramWeb, extract_handles
from loveworm.helpers.llm import LLMError, analyze_person, bloque_perfil, draft_message, generar_guia
from loveworm.helpers.tinder_api import TinderApi

# bump when the analysis schema changes so every person gets re-analyzed
ANALYSIS_VERSION = "persona-v2"
# cada cuántos minutos se vuelven a traer los chats de Tinder solos
REFRESCO_MIN = 15

# techo de envíos automáticos por día, para que el bot no se suelte
AUTO_SEND_MAX_DIA = 12

DEFAULT_SETTINGS = {
    "instructions": (
        "Escribí corto y natural, en español rioplatense, como en un chat de verdad. "
        "Nada de frases armadas, piropos genéricos ni muchos emojis. En el primer mensaje, "
        "comentá algo concreto de su bio; si no tiene bio, un saludo simple con una pregunta. "
        "Sos poco empujador: proponé salir recién cuando haya ida y vuelta real, una sola vez "
        "y con un día concreto."
    ),
    "model": "haiku",
    # when on, claude drafts openers for matches created after drafts_since and replies for chats it opened
    "auto_drafts": False,
    # when on, the bot sends the draft by itself instead of waiting for the Enviar click
    "auto_send": False,
    # cuántos chats automatiza el bot a la vez, y cuántos mensajes puede mandar solo por día
    "max_activas": MAX_ACTIVAS,
    "tope_diario": AUTO_SEND_MAX_DIA,
    # reglas de escritura que se aplican a todo lo que se envía (ver helpers/estilo.py)
    "estilo": estilo.DEFAULT_ESTILO,
    "drafts_since": None,
    # set once the instagram tab logged in; refreshes then also read instagram
    "instagram": False,
    # filtro por características físicas antes de dar like (ver helpers/fisico.py)
    "fisico": fisico.DEFAULT,
    # lo que Claude tiene que saber de vos: qué hacés, qué te gusta, cómo sos
    "sobre_mi": "",
    # swipes manuales y automáticos diarios (ver helpers/swipes.py)
    "swipes": swipes.DEFAULT,
}
MODELS = ("haiku", "sonnet", "opus")

# paused: instagram web no longer serves DMs through its api and reading them on screen is not
# reliable yet. While off, nothing opens instagram and the dashboard hides its panel.
INSTAGRAM_ENABLED = False

# instagram is read profile by profile on screen, so keep each run short
IG_MAX_PER_RUN = 30
IG_REREAD_SECONDS = 24 * 3600

# pausa antes de mandar solo, para no disparar en el mismo segundo en que claude terminó
PAUSA_ENVIO = (8, 30)


def _error_de_sesion(msg):
    """Límite de uso o sesión de Claude vencida: conviene pausar todo en vez de marcar error
    en cada chat, que es lo que pasaría si cada llamada falla por su cuenta."""
    bajo = (msg or "").lower()
    return any(p in bajo for p in ("limit", "authenticate", "oauth", "session expired", "login required"))


def _age(birth_date):
    if not birth_date:
        return None
    born = datetime.fromisoformat(birth_date.replace("Z", "+00:00")).date()
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _photos(person):
    urls = [p.get("url") for p in person.get("photos") or []]
    return [u for u in urls if u and u.startswith("https://")]


def _now_iso():
    # same format as tinder's created_date, so both compare as strings
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _chat_key(entry):
    msgs = entry.get("messages") or []
    key = f"{len(msgs)}|{msgs[-1]['sent'] if msgs else ''}"
    ig = _ig_messages(entry)
    # only chats linked to instagram get the extra part, so existing keys stay valid.
    # messages read from the page have no date, so the last text stands in for it
    return key + (f"|ig{len(ig)}|{ig[-1]['text'][:40]}" if ig else "")


def _draft_key(entry):
    """Un borrador viejo no sirve si llegaron mensajes nuevos o cambió el estado de la cita."""
    return _chat_key(entry) + "|" + citas.leer(entry)["estado"]


def _ig_messages(entry):
    return (entry.get("instagram") or {}).get("messages") or []


def _interest(person):
    analysis = person.get("analysis")
    return analysis["interes"] if analysis and person.get("analysis_status") == "ok" else -1


class Bot:

    def __init__(self, project_root):
        self.root = Path(project_root)
        self.cache_path = self.root / "data" / "dashboard_cache.json"
        self.lists_path = self.root / "data" / "dashboard_lists.json"
        self.settings_path = self.root / "data" / "dashboard_settings.json"
        self.envios_path = self.root / "data" / "envios.json"
        self.guia_path = self.root / "data" / "guia_estilo.json"
        self._guia_cache = None
        self._guia_lock = threading.Lock()
        self._guia_fallo = 0.0
        self.cache = self._load_cache()
        self.settings = self._load_settings()
        self.envios = self._load_envios()
        self.swipes = swipes.Registro(self.root / "data" / "swipes.json")
        # likes/dislikes de la sesión al arrancar el swipe en curso, para contar en vivo
        self._swipe_base = None
        # la política que mantiene 5 conversaciones vivas y rota al que no contesta
        self.cupos = Cupos(self.root / "data" / "cupos.json", max_activas=self.settings["max_activas"])

        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        # stop_event corta solo el swipe en curso; _apagado frena los hilos de fondo
        self.stop_event = threading.Event()
        self._apagado = threading.Event()
        self.llm = ThreadPoolExecutor(max_workers=2)

        self.session = None
        # window handle per site: the tinder and instagram apis each need their own page
        self.tabs = {}
        self.status = "apagado"
        self.current_job = None
        self.logs = deque(maxlen=200)
        self.active = []
        self.available = []
        self.last_refresh = None
        self.progress = None
        # set when claude hits the subscription usage limit; queued claude calls wait until resumed
        self.llm_paused = False
        self._load_lists()

        self._refresh_t = None  # se setea en el primer refresh; sin él no hay refresco automático
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._refresco_automatico, daemon=True).start()
        threading.Thread(target=self._swipes_automaticos, daemon=True).start()
        # analysis only needs the stored chats, so it starts without opening chrome
        self._schedule_llm()

    # --- public api (called from flask) ---

    def submit(self, job, **kwargs):
        if job == "stop":
            self.stop_event.set()
            self.log("Pedido de detener el swipe.")
            return
        self.jobs.put((job, kwargs))

    def reanalyze(self, match_id):
        with self.lock:
            entry = self.cache.get(match_id)
            if entry:
                entry.pop("analysis_key", None)
                entry.pop("analysis_error", None)
        self._schedule_llm()

    def generate_draft(self, match_id):
        with self.lock:
            entry = self.cache.get(match_id)
            if not entry:
                return
            entry.pop("draft", None)
            entry.pop("draft_error", None)
            entry.pop("draft_dismissed", None)
            entry["draft_requested"] = True
        self._schedule_llm()

    def discard_draft(self, match_id):
        """Descarta el borrador. Si el envío automático todavía no salió, lo cancela."""
        with self.lock:
            entry = self.cache.get(match_id)
            if entry:
                entry.pop("draft", None)
                entry.pop("draft_requested", None)
                entry["draft_dismissed"] = _draft_key(entry)
        self._save_cache()

    def fijar_cupo(self, match_id):
        """Le guarda el cupo aunque deje de responder."""
        self.cupos.fijar(match_id)
        self.log(f"Cupo fijado: {self.cache.get(match_id, {}).get('name', '?')}.")
        self._schedule_llm()
        self._enviar_borradores_pendientes()

    def desfijar_cupo(self, match_id):
        self.cupos.desfijar(match_id)
        self.log(f"Cupo sin fijar: {self.cache.get(match_id, {}).get('name', '?')} puede rotar.")

    def soltar_cupo(self, match_id):
        self.cupos.soltar(match_id)
        self._revisar_cupos()
        self._schedule_llm()

    def liberar_cupo(self, match_id):
        """La devuelve al pool, como si nunca hubiera estado activa."""
        self.cupos.liberar(match_id)
        self._revisar_cupos()
        self._schedule_llm()

    def resume_llm(self):
        self.llm_paused = False
        with self.lock:
            for entry in self.cache.values():
                entry.pop("analysis_error", None)
                entry.pop("draft_error", None)
        self.log("Claude reanudado.")
        self._schedule_llm()

    def get_settings(self):
        return dict(self.settings, estilo=estilo.completar(self.settings.get("estilo")),
                    estilo_reglas=estilo.descripciones(), models=MODELS,
                    fisico=fisico.completar(self.settings.get("fisico")),
                    fisico_atributos=fisico.descripciones(), guia=self._leer_guia(),
                    swipes=swipes.completar(self.settings.get("swipes")))

    def _leer_guia(self):
        """La guía ya guardada (sin generarla): estilo, hechos y fecha."""
        if self._guia_cache is None:
            try:
                self._guia_cache = json.loads(self.guia_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                self._guia_cache = {}
        return self._guia_cache

    def _guia(self):
        """Guía de estilo y datos sacados de tus chats. Se genera una sola vez con Claude (tarda un rato) y
        queda en data/guia_estilo.json; para rehacerla, el botón de la configuración."""
        with self._guia_lock:
            guia = self._leer_guia()
            if guia or time.time() - self._guia_fallo < 600:
                return guia
            with self.lock:
                textos = estilo.mensajes_propios(self.cache)
                stats = estilo.estadisticas(self.cache)
                tramos = estilo.ejemplos(self.cache, cantidad=20, parecidos=0)
            if len(textos) < 40:
                return {}
            self.log("Leyendo tus chats para aprender cómo escribís (se hace una sola vez)...")
            try:
                guia = generar_guia(textos, tramos, stats)
            except LLMError as e:
                self._guia_fallo = time.time()
                self.log(f"No se pudo armar la guía de estilo: {e}")
                return {}
            guia["fecha"] = _now_iso()
            self.guia_path.parent.mkdir(exist_ok=True)
            self.guia_path.write_text(json.dumps(guia, ensure_ascii=False, indent=2), encoding="utf-8")
            self._guia_cache = guia
            self.log("Guía de estilo lista.")
            return guia

    def regenerar_guia(self):
        with self._guia_lock:
            self._guia_cache = {}
            self._guia_fallo = 0.0
            try:
                self.guia_path.unlink()
            except FileNotFoundError:
                pass
        self.log("Guía de estilo borrada: se vuelve a armar con el próximo borrador.")

    def update_settings(self, instructions=None, model=None, auto_drafts=None, auto_send=None,
                        estilo_opts=None, max_activas=None, tope_diario=None, fisico_opts=None,
                        sobre_mi=None, swipes_opts=None):
        """Guarda solo lo que viene (None = se conserva), así cada control de la interfaz
        puede cambiar lo suyo sin pisar el resto."""
        if model is not None and model not in MODELS:
            raise ValueError("Modelo inválido")
        for nombre, valor, minimo, maximo in (("max_activas", max_activas, 1, 20), ("tope_diario", tope_diario, 0, 200)):
            if valor is not None and not minimo <= valor <= maximo:
                raise ValueError(f"{nombre} tiene que estar entre {minimo} y {maximo}")
        if auto_send:
            auto_drafts = True  # el envío automático sale de un borrador
        if auto_drafts and not self.settings["auto_drafts"]:
            # only matches that arrive from now on get an opener
            self.settings["drafts_since"] = _now_iso()
        encendio_auto = bool(auto_send) and not self.settings["auto_send"]
        nuevos = {"instructions": None if instructions is None
                  else instructions.strip() or DEFAULT_SETTINGS["instructions"],
                  "model": model, "auto_drafts": auto_drafts, "auto_send": auto_send,
                  "estilo": None if estilo_opts is None else estilo.completar(estilo_opts),
                  "max_activas": max_activas, "tope_diario": tope_diario,
                  "fisico": None if fisico_opts is None else fisico.completar(fisico_opts),
                  "sobre_mi": None if sobre_mi is None else sobre_mi.strip(),
                  "swipes": None if swipes_opts is None
                  else swipes.completar(dict(self.settings.get("swipes") or {}, **swipes_opts))}
        self.settings.update({k: v for k, v in nuevos.items() if v is not None})
        if auto_drafts is False:
            self.settings["auto_send"] = False  # sin borradores no hay nada que enviar
        self.cupos.max_activas = self.settings["max_activas"]
        self._save_settings()
        self.log("Configuración guardada.")
        if max_activas is not None:
            self._revisar_cupos()
        if encendio_auto:
            # los borradores que ya estaban escritos salen ahora, sin tener que regenerarlos
            self._enviar_borradores_pendientes()
        self._schedule_llm()

    def _save_settings(self):
        self.settings_path.parent.mkdir(exist_ok=True)
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def chat(self, match_id):
        with self.lock:
            entry = self.cache.get(match_id, {})
            ig = entry.get("instagram") or {}
            return {"name": entry.get("name", "?"), "messages": list(entry.get("messages") or []),
                    "instagram": {"username": ig.get("username"), "messages": list(ig.get("messages") or [])}}

    def state(self):
        with self.lock:
            people = [dict(a, **self._llm_fields(a["id"])) for a in self.active]
            new = [dict(a, **self._llm_fields(a["id"])) for a in self.available]
            stats = dict(self.session.session_data) if self.session else None
            agendas = citas.proximas(self.cache)
            cupos = self.cupos.resumen(self.cache)
        people.sort(key=lambda p: (_interest(p), p["last_sent"] or ""), reverse=True)
        return {
            "status": self.status,
            "job": self.current_job,
            "pending_jobs": self.jobs.qsize(),
            "logs": list(self.logs),
            "stats": stats,
            "last_refresh": self.last_refresh,
            "progress": self.progress,
            "llm_paused": self.llm_paused,
            "auto_drafts": self.settings["auto_drafts"],
            "auto_send": self.settings["auto_send"],
            "instagram": INSTAGRAM_ENABLED and self.settings["instagram"],
            "instagram_enabled": INSTAGRAM_ENABLED,
            "citas": agendas,
            "cupos": cupos,
            "envios_hoy": self._envios_hoy(),
            "envios_tope": self.settings["tope_diario"],
            "envios_ultimos": self.envios.get("ultimos", [])[:10],
            "swipes": self._swipes_estado(),
            "fisico": fisico.completar(self.settings.get("fisico")),
            "people": people,
            "new": new,
        }

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line)
        with self.lock:
            self.logs.append(line)

    # --- job loop ---

    def _loop(self):
        while True:
            job, kwargs = self.jobs.get()
            self.current_job = job
            try:
                getattr(self, f"_job_{job}")(**kwargs)
            except Exception as e:
                self.log(f"Error en '{job}': {e}")
                traceback.print_exc()
                if not self._browser_alive():
                    self.session = None
                    self.status = "apagado"
            finally:
                self.current_job = None

    def _browser_alive(self):
        try:
            return self.session is not None and bool(self.session.browser.current_url)
        except Exception:
            return False

    def _require_session(self):
        if not self._browser_alive():
            raise RuntimeError("El navegador no está abierto. Tocá 'Iniciar' primero.")

    def _tab(self, site):
        """Switches the browser to the tab of `site` ("tinder" or "instagram"), opening it if needed."""
        browser = self.session.browser
        handle = self.tabs.get(site)
        if handle not in browser.window_handles:
            browser.switch_to.new_window("tab")
            browser.get("https://tinder.com/app/recs" if site == "tinder" else "https://www.instagram.com/")
            time.sleep(4)
            self.tabs[site] = browser.current_window_handle
        else:
            browser.switch_to.window(handle)
        return browser

    def _job_start(self):
        if self._browser_alive():
            self.log("El navegador ya está abierto.")
            return
        # imported here so the dashboard loads even before chrome is set up
        from loveworm.session import Session

        self.status = "abriendo navegador"
        self.log("Abriendo Chrome...")
        self.session = Session(user_data=f"{self.root}/chrome_profile/")
        self.tabs = {"tinder": self.session.browser.current_window_handle}

        self.status = "esperando login"
        self.log("Si no estás logueado, iniciá sesión a mano en el Chrome (Log in -> Google).")
        self.session.login_manually()

        self.status = "listo"
        self.log("Sesión iniciada.")
        self.jobs.put(("refresh", {}))

    def _job_swipe(self, amount, ratio, sleep, auto=False):
        self._require_session()
        api = TinderApi(self._tab("tinder"))
        try:
            quedan, recarga = api.likes_restantes()
        except Exception as e:
            quedan, recarga = None, None
            self.log(f"No se pudo consultar cuántos likes te quedan: {str(e)[:150]}")
        if quedan == 0:
            self.swipes.sin_likes_hasta = recarga or time.time() + 3600
            self.log("Tinder no te deja dar más likes hasta las "
                     f"{time.strftime('%H:%M', time.localtime(self.swipes.sin_likes_hasta))}.")
            return
        if quedan is not None and quedan < amount:
            self.log(f"Te quedan {quedan} likes en Tinder: la tanda se achica a eso.")
            amount = quedan

        self.stop_event.clear()
        self.status = "swipeando"
        filtro = None
        if self.settings["fisico"].get("activo"):
            filtro = fisico.Filtro(driver=self.session.browser, root=self.root,
                                   opciones=self.settings["fisico"], log=self.log, api=api)
            self.log(f"Filtro físico en modo {filtro.cfg['modo']}: quiero {filtro.cfg['obligatorio'] or '—'}, "
                     f"evitar {filtro.cfg['excluir'] or '—'}.")
        self.log(f"Swipe{' automático' if auto else ''}: {amount} likes, {ratio}% de likes, pausa {sleep}s.")
        datos = self.session.session_data
        self._swipe_base = {"like": datos["like"], "dislike": datos["dislike"]}
        try:
            self.session.like(amount=amount, ratio=f"{ratio}%", sleep=sleep,
                              stop_event=self.stop_event, filtro=filtro)
        finally:
            base, self._swipe_base = self._swipe_base, None
            self.swipes.sumar(datos["like"] - base["like"], datos["dislike"] - base["dislike"])
            self.status = "listo"
            if auto:
                self.swipes.programar_siguiente()
            if filtro is not None:
                self.log(filtro.resumen())
        hoy = self._swipes_hoy()
        self.log(f"Swipe terminado. Hoy: {hoy['likes']} likes, {hoy['dislikes']} dislikes.")

    def _swipes_hoy(self):
        """Lo guardado de hoy más lo que va del swipe en curso."""
        hoy = self.swipes.hoy()
        base, session = self._swipe_base, self.session
        if base and session:
            hoy["likes"] += session.session_data["like"] - base["like"]
            hoy["dislikes"] += session.session_data["dislike"] - base["dislike"]
        return hoy

    def _swipes_espera(self, cfg):
        """Por qué la automática no swipea ahora mismo, o None si le toca."""
        ahora = time.time()
        if self.status == "swipeando":
            return "Swipeando."
        if self._swipes_hoy()["likes"] >= cfg["por_dia"]:
            return "Meta de hoy cumplida. Sigue mañana."
        if ahora < self.swipes.sin_likes_hasta:
            return ("Tinder te dejó sin likes. Sigue a las "
                    f"{time.strftime('%H:%M', time.localtime(self.swipes.sin_likes_hasta))}.")
        if not swipes.en_franja(cfg):
            return f"Corre entre las {cfg['desde']} y las {cfg['hasta']} h."
        if not self._browser_alive():
            return "El navegador está cerrado: no swipea hasta que lo inicies."
        if ahora < self.swipes.proxima:
            return f"Próxima tanda cerca de las {time.strftime('%H:%M', time.localtime(self.swipes.proxima))}."
        if self.status != "listo" or self.current_job or not self.jobs.empty():
            return "Esperando que el navegador termine lo que está haciendo."
        return None

    def _swipes_estado(self):
        cfg = swipes.completar(self.settings.get("swipes"))
        return {"cfg": cfg, "hoy": self._swipes_hoy(), "historial": self.swipes.historial(),
                "espera": self._swipes_espera(cfg) if cfg["auto"] else None}

    def _swipes_automaticos(self):
        """Cada día, dentro de la franja, da likes en tandas hasta llegar a la meta."""
        while not self._apagado.wait(30):
            cfg = swipes.completar(self.settings.get("swipes"))
            if not cfg["auto"] or self._swipes_espera(cfg) is not None:
                continue
            faltan = cfg["por_dia"] - self._swipes_hoy()["likes"]
            n = min(swipes.TANDA, faltan)
            # no volver a encolar mientras la tanda espera turno; al terminar se programa la siguiente
            self.swipes.proxima = time.time() + 3600
            self.log(f"Swipes automáticos: tanda de {n} likes (meta {cfg['por_dia']} por día).")
            self.jobs.put(("swipe", {"amount": n, "ratio": cfg["ratio"], "sleep": cfg["pausa"], "auto": True}))

    def guardar_swipes(self, opciones):
        """Guarda los valores del panel de swipes sin el ruido de update_settings."""
        antes = swipes.completar(self.settings.get("swipes"))
        self.settings["swipes"] = swipes.completar(dict(antes, **opciones))
        self._save_settings()
        if self.settings["swipes"]["auto"] and not antes["auto"]:
            self.swipes.proxima = 0.0  # recién prendida: la primera tanda sale enseguida si toca
            self.log("Swipes automáticos activados.")
        elif antes["auto"] and not self.settings["swipes"]["auto"]:
            self.log("Swipes automáticos desactivados.")

    def _person_fields(self, m):
        person = m.get("person") or {}
        return {
            "id": m["_id"],
            "name": person.get("name", "?"),
            "age": _age(person.get("birth_date")),
            "gender": person.get("gender"),
            "photos": _photos(person),
            "bio": person.get("bio", ""),
            "matched": m.get("created_date"),
        }

    def _refresco_automatico(self):
        """Pide un refresco cada REFRESCO_MIN minutos mientras el navegador esté abierto y sin nada en curso."""
        while not self._apagado.wait(30):
            t = self._refresh_t
            if (t is None or time.monotonic() - t < REFRESCO_MIN * 60
                    or self.status != "listo" or self.current_job or not self.jobs.empty()
                    or not self._browser_alive()):
                continue
            self._refresh_t = time.monotonic()  # no apilar pedidos mientras el job espera turno
            self.jobs.put(("refresh", {}))

    def _job_refresh(self):
        self._require_session()
        self._refresh_t = time.monotonic()  # también si falla: reintenta recién en el próximo ciclo
        self.status = "actualizando chats"
        api = TinderApi(self._tab("tinder"))
        try:
            active_raw = api.get_matches(messaged=True)
            available_raw = api.get_matches(messaged=False)
            self.log(f"{len(active_raw)} chats activos, {len(available_raw)} matches sin chat.")

            available = sorted((self._person_fields(m) for m in available_raw),
                               key=lambda a: a["matched"] or "", reverse=True)
            with self.lock:
                for a in available:
                    entry = self.cache.setdefault(a["id"], {})
                    entry.update(name=a["name"], bio=a["bio"], gender=a["gender"], matched=a["matched"])
                self.available = available

            # first load: show chats as they arrive instead of all at the end
            first_load = not self.active
            active = []
            fetched = 0
            for m in active_raw:
                fields = self._person_fields(m)
                person = m.get("person") or {}
                mid = m["_id"]
                with self.lock:
                    entry = dict(self.cache.get(mid, {}))

                # only re-download chats that changed since last refresh
                if entry.get("last_activity") != m.get("last_activity_date") or "messages" not in entry:
                    raw = api.get_messages(mid)
                    entry["messages"] = [{
                        "from_me": msg.get("from") != person.get("_id"),
                        "text": msg.get("message", ""),
                        "sent": msg.get("sent_date"),
                    } for msg in raw]
                    entry["last_activity"] = m.get("last_activity_date")
                    fetched += 1
                    time.sleep(0.4)

                entry.update(name=fields["name"], bio=fields["bio"], gender=fields["gender"], matched=fields["matched"])
                # a draft written before new messages arrived no longer fits the chat
                if entry.get("draft") and entry["draft"].get("for") != _draft_key(entry):
                    entry.pop("draft")
                with self.lock:
                    self.cache[mid] = entry

                msgs = entry["messages"]
                last = msgs[-1] if msgs else None
                active.append(dict(fields,
                                   message_count=len(msgs),
                                   last_text=last["text"] if last else "",
                                   last_from_me=last["from_me"] if last else None,
                                   last_sent=last["sent"] if last else m.get("last_activity_date")))

                self.progress = f"{len(active)}/{len(active_raw)}"
                if first_load and len(active) % 10 == 0:
                    with self.lock:
                        self.active = list(active)

            with self.lock:
                self.active = active
                self.last_refresh = datetime.now().isoformat(timespec="seconds")
            self._save_cache()
            self._save_lists()
            self.log(f"Chats actualizados ({fetched} descargados de nuevo).")
        finally:
            self.status = "listo"
            self.progress = None

        # la política decide quiénes quedan en juego antes de pedir borradores
        self._revisar_cupos()
        self._schedule_llm()
        # borradores que quedaron escritos de antes (por ejemplo, tras reiniciar el dashboard)
        self._enviar_borradores_pendientes()
        if INSTAGRAM_ENABLED and self.settings["instagram"]:
            self.jobs.put(("refresh_instagram", {}))

    def _revisar_cupos(self):
        resultado = self.cupos.revisar(self.cache)
        for mid in resultado["rotadas"]:
            self.log(f"Cupo liberado: {self.cache.get(mid, {}).get('name', '?')} "
                     f"({self.cupos.rotadas[mid]['motivo']}).")
        for mid in resultado["promovidas"]:
            self.log(f"Cupo nuevo: {self.cache.get(mid, {}).get('name', '?')}.")

    def _job_connect_instagram(self):
        self._require_session()
        api = InstagramWeb(self._tab("instagram"))
        self.status = "esperando login de Instagram"
        if not api.is_logged_in():
            self.log("Iniciá sesión en Instagram en la pestaña que se abrió en el Chrome del bot.")
            deadline = time.time() + 600
            while not api.is_logged_in():
                if time.time() > deadline:
                    self.status = "listo"
                    self.log("No se detectó el login de Instagram. Volvé a tocar 'Conectar Instagram'.")
                    return
                time.sleep(5)
        self.settings["instagram"] = True
        self._save_settings()
        self.status = "listo"
        self.log("Instagram conectado.")
        self.jobs.put(("refresh_instagram", {}))

    def _job_refresh_instagram(self):
        """Reads the instagram chat of people who shared their @ on tinder, profile by profile (read only)."""
        self._require_session()
        web = InstagramWeb(self._tab("instagram"))
        self.status = "actualizando instagram"
        try:
            if not web.is_logged_in():
                self.settings["instagram"] = False
                self._save_settings()
                self.log("Instagram no tiene la sesión iniciada. Tocá 'Conectar Instagram'.")
                return

            # most recent tinder chats first; each person is re-read at most once a day
            now = time.time()
            with self.lock:
                candidates = []
                for mid, e in self.cache.items():
                    msgs = e.get("messages") or []
                    ig = e.get("instagram") or {}
                    handles = extract_handles(m["text"] for m in msgs if not m["from_me"])
                    if handles and now - ig.get("read_at", 0) > IG_REREAD_SECONDS:
                        candidates.append((msgs[-1]["sent"] or "", mid, e.get("name", "?"), handles[0]))
            candidates.sort(reverse=True)
            candidates = candidates[:IG_MAX_PER_RUN]
            self.log(f"Instagram: leyendo {len(candidates)} perfiles (máximo {IG_MAX_PER_RUN} por vez).")

            linked = 0
            for i, (_, mid, name, handle) in enumerate(candidates, 1):
                self.progress = f"{i}/{len(candidates)}"
                try:
                    messages, note = web.read_chat(handle)
                except Exception as e:
                    messages, note = None, f"error: {str(e).splitlines()[0][:150]}"
                self.log(f"Instagram @{handle} ({name}): {note}")
                if messages is None:
                    # not marked as read, so it is retried on the next refresh
                    continue
                with self.lock:
                    self.cache[mid]["instagram"] = {"username": handle, "messages": messages, "read_at": now}
                linked += bool(messages)
                self._save_cache()

            self.log(f"Instagram: {linked} chats leídos de {len(candidates)} perfiles.")
        finally:
            self.status = "listo"
            self.progress = None
        self._schedule_llm()

    def _job_send(self, match_id, text, auto=False):
        self._require_session()
        with self.lock:
            nombre = (self.cache.get(match_id) or {}).get("name", "?")

        if auto:
            if self.cupos.estado(match_id) != "activa":
                self.log(f"Envío automático a {nombre} descartado: le liberaron el cupo.")
                return
            time.sleep(random.uniform(*PAUSA_ENVIO))
            # se vuelve a chequear DESPUÉS de la pausa: tenés esos segundos para arrepentirte
            # descartando el borrador, o por si llegó un mensaje nuevo en el medio
            with self.lock:
                actual = (self.cache.get(match_id, {}).get("draft") or {}).get("text")
            if actual != text:
                self.log(f"Envío automático a {nombre} cancelado: el borrador ya no está.")
                return

        TinderApi(self._tab("tinder")).send_message(match_id, text, estilo_opts=self.settings.get("estilo"))
        with self.lock:
            entry = self.cache.setdefault(match_id, {})
            entry.pop("draft", None)
            entry.pop("draft_requested", None)
            # chats opened or answered from the dashboard keep getting reply drafts
            entry["managed"] = True
            entry["ultimo_envio_auto"] = auto
            # hasta que conteste o llegue algo nuevo, no se le vuelve a escribir
            entry["esperando"] = _chat_key(entry)
        if auto:
            self._registrar_envio(match_id, nombre, text)
        self._save_cache()
        self.log(f"Mensaje {'automático ' if auto else ''}enviado a {nombre}.")
        self.jobs.put(("refresh", {}))

    def _job_close(self):
        if self._browser_alive():
            self.session.browser.quit()
        self.session = None
        self.status = "apagado"
        self.log("Navegador cerrado.")

    # --- claude: person analysis and message drafts ---

    def _llm_fields(self, match_id):
        entry = self.cache.get(match_id, {})
        analysis = entry.get("analysis")
        if analysis and entry.get("analysis_key") == f"{ANALYSIS_VERSION}|{_chat_key(entry)}":
            status = "ok"
        elif entry.get("analysis_error"):
            status = "error"
        elif entry.get("analysis_running"):
            status = "analizando"
        else:
            # queued, or waiting for claude to be resumed
            status = "pendiente"
        draft = entry.get("draft")
        ig = entry.get("instagram") or {}
        return {
            "instagram": {"username": ig["username"], "message_count": len(ig.get("messages") or []),
                          "last_sent": (ig.get("messages") or [{}])[-1].get("sent")} if ig.get("username") else None,
            "analysis": analysis,
            "analysis_status": status,
            "analysis_error": entry.get("analysis_error"),
            "cita": citas.leer(entry),
            "cupo": self.cupos.estado(match_id),
            "draft": draft["text"] if draft else None,
            "draft_pending": bool(entry.get("draft_queued")),
            "draft_error": entry.get("draft_error"),
            "managed": entry.get("managed", False),
            # ya le escribimos y no contestó: el bot espera sin insistir
            "esperando": entry.get("esperando") == _chat_key(entry),
            "ultimo_envio_auto": bool(entry.get("ultimo_envio_auto")),
        }

    def _wants_draft(self, mid, entry):
        msgs = entry.get("messages") or []
        if entry.get("draft") or entry.get("draft_error") or entry.get("draft_dismissed") == _draft_key(entry):
            return False
        if entry.get("draft_requested"):
            return True  # pedido a mano: no importa el cupo ni si ya le escribimos
        if self.cupos.estado(mid) != "activa":
            return False
        # ya le escribimos y todavía no contestó: nada de doble texto
        if entry.get("esperando") == _chat_key(entry):
            return False
        if not self.settings["auto_drafts"]:
            return False
        if not msgs:
            since = self.settings.get("drafts_since") or ""
            return (entry.get("matched") or "") >= since
        # contesta siempre que ella haya escrito último: los cupos ya acotan cuántos chats son
        return not msgs[-1]["from_me"]

    def _schedule_llm(self):
        if self.llm_paused:
            return
        tasks = []
        with self.lock:
            for mid, e in self.cache.items():
                msgs = e.get("messages") or []
                key = f"{ANALYSIS_VERSION}|{_chat_key(e)}"
                analizando = bool(e.get("analysis_queued") or e.get("analysis_running"))
                if msgs and e.get("analysis_key") != key and not e.get("analysis_queued") and not e.get("analysis_error"):
                    if not any(not m["from_me"] for m in msgs + _ig_messages(e)):
                        # never answered: no need to ask claude
                        e["analysis"] = {"perfil": e.get("bio", ""), "charla": "No respondió ningún mensaje.",
                                         "interes": 0, "senal": "No respondió"}
                        e["analysis_key"] = key
                        e["cita"] = citas.por_defecto(msgs)
                    else:
                        e["analysis_queued"] = True
                        analizando = True
                        tasks.append((self._analyze, mid))
                # el borrador espera al análisis, así lo escribe con la cita ya actualizada
                if self._wants_draft(mid, e) and not e.get("draft_queued") and not analizando:
                    e["draft_queued"] = True
                    tasks.append((self._draft, mid))
        for fn, mid in tasks:
            self.llm.submit(fn, mid)

    def _call_llm(self, match_id, kind, fn):
        """Runs fn() and handles the usage limit. kind is the cache key prefix: analysis or draft."""
        with self.lock:
            entry = self.cache[match_id]
            name = entry.get("name", "?")
        try:
            if self.llm_paused:
                return None
            with self.lock:
                entry[f"{kind}_running"] = True
            return fn()
        except LLMError as e:
            if _error_de_sesion(str(e)):
                # límite de uso o sesión vencida: queda pendiente y se frena toda la cola,
                # en vez de marcar error en cada chat
                if not self.llm_paused:
                    self.llm_paused = True
                    self.log(f"Claude pausado: {e}")
                    self.log("Arreglá la sesión del CLI y tocá 'Reanudar'.")
            else:
                with self.lock:
                    entry[f"{kind}_error"] = str(e)
                self.log(f"Claude falló con {name}: {e}")
            return None
        finally:
            with self.lock:
                entry.pop(f"{kind}_queued", None)
                entry.pop(f"{kind}_running", None)

    def _snapshot(self, match_id):
        with self.lock:
            e = self.cache[match_id]
            return (e.get("name", "?"), e.get("gender"), e.get("bio", ""), list(e.get("messages") or []),
                    list(_ig_messages(e)), _chat_key(e))

    def _analyze(self, match_id):
        try:
            name, gender, bio, msgs, ig_msgs, key = self._snapshot(match_id)
            result = self._call_llm(match_id, "analysis", lambda: analyze_person(
                name, gender, bio, msgs, ig_messages=ig_msgs, model=self.settings["model"]))
            if result:
                leido = result.pop("cita", None)
                with self.lock:
                    entry = self.cache[match_id]
                    entry["analysis"] = result
                    entry["analysis_key"] = f"{ANALYSIS_VERSION}|{key}"
                    entry["cita"] = citas.aplicar(entry.get("cita"), leido)
            self._save_cache()
        finally:
            # el borrador se programa recién ahora, con la cita ya actualizada
            self._schedule_llm()

    def _draft(self, match_id):
        name, gender, bio, msgs, _, key = self._snapshot(match_id)
        cita = citas.leer(self.cache.get(match_id, {}))
        guia = self._guia()
        with self.lock:
            ejemplos = estilo.ejemplos(self.cache, excluir=match_id, consulta=estilo.consulta_de(msgs))
            stats = estilo.estadisticas(self.cache)
        perfil = bloque_perfil(dict(guia, stats=stats), self.settings.get("sobre_mi", ""))
        text = self._call_llm(match_id, "draft", lambda: draft_message(
            name, gender, bio, msgs, self.settings["instructions"], model=self.settings["model"], cita=cita,
            estilo_opts=self.settings.get("estilo"), ejemplos=ejemplos, perfil=perfil))
        if text:
            with self.lock:
                entry = self.cache[match_id]
                entry["draft"] = {"text": text, "for": _draft_key(entry)}
                entry.pop("draft_requested", None)
            self._save_cache()

            if self._auto_envia(match_id):
                self.jobs.put(("send", {"match_id": match_id, "text": text, "auto": True}))
            else:
                motivo = ("envío automático apagado" if not self.settings["auto_send"]
                          else "tope diario alcanzado" if not self._puede_enviar_auto()
                          else "no está activa")
                self.log(f"Borrador de {name} queda para revisar ({motivo}).")

    def _auto_envia(self, match_id):
        return (self.settings["auto_send"] and self._puede_enviar_auto()
                and self.cupos.estado(match_id) == "activa" and self._browser_alive())

    def _enviar_borradores_pendientes(self):
        """Manda los borradores que ya estaban escritos y esperando turno.

        Sirve al prender el envío automático, o al reiniciar con borradores colgados.
        """
        with self.lock:
            pendientes = [(mid, (e.get("draft") or {}).get("text")) for mid, e in self.cache.items()
                          if (e.get("draft") or {}).get("text")]
        for mid, texto in pendientes:
            if self._auto_envia(mid):
                nombre = self.cache.get(mid, {}).get("name", "?")
                self.log(f"Se encola el borrador que estaba esperando de {nombre}.")
                self.jobs.put(("send", {"match_id": mid, "text": texto, "auto": True}))

    # --- tope diario de envíos automáticos ---

    def _envios_hoy(self):
        return self.envios.get("n", 0) if self.envios.get("fecha") == date.today().isoformat() else 0

    def _puede_enviar_auto(self):
        return self._envios_hoy() < self.settings["tope_diario"]

    def _registrar_envio(self, match_id, nombre, texto):
        hoy = date.today().isoformat()
        if self.envios.get("fecha") != hoy:
            self.envios = {"fecha": hoy, "n": 0, "ultimos": []}
        self.envios["n"] = self.envios.get("n", 0) + 1
        self.envios["ultimos"] = ([{"match_id": match_id, "nombre": nombre, "texto": texto,
                                    "fecha": datetime.now().isoformat(timespec="seconds")}]
                                  + self.envios.get("ultimos", []))[:50]
        self._save_envios()

    def _load_envios(self):
        try:
            return json.loads(self.envios_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_envios(self):
        self.envios_path.parent.mkdir(exist_ok=True)
        self.envios_path.write_text(json.dumps(self.envios, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- persistence ---

    def _load_cache(self):
        try:
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        for entry in cache.values():
            for k in ("analysis_queued", "draft_queued", "summary_queued", "analysis_running", "draft_running"):
                entry.pop(k, None)
            for k in ("analysis_error", "draft_error"):
                if _error_de_sesion(entry.get(k)):
                    entry.pop(k)
            # drop drafts that no longer match the chat (new messages arrived)
            if entry.get("draft") and entry["draft"].get("for") != _draft_key(entry):
                entry.pop("draft")
        return cache

    def _load_settings(self):
        settings = dict(DEFAULT_SETTINGS)
        try:
            settings.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return settings

    def _load_lists(self):
        """Last refresh's lists, so the dashboard shows them without opening chrome."""
        try:
            data = json.loads(self.lists_path.read_text(encoding="utf-8"))
            self.active, self.available = data["active"], data["available"]
            self.last_refresh = data.get("last_refresh")
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            # older caches have no saved lists: rebuild the active chats from the stored messages
            for mid, entry in self.cache.items():
                msgs = entry.get("messages")
                if msgs:
                    self.active.append({"id": mid, "name": entry.get("name", "?"), "message_count": len(msgs),
                                        "last_text": msgs[-1]["text"], "last_from_me": msgs[-1]["from_me"],
                                        "last_sent": msgs[-1]["sent"]})

        # lists saved by older versions had a single photo and the chat inline
        for p in self.active + self.available:
            p.setdefault("photos", [p["photo"]] if p.get("photo") else [])
            p.pop("photo", None)
            p.pop("messages", None)
            for k in ("age", "gender", "matched"):
                p.setdefault(k, None)
            p.setdefault("bio", "")

    def _save_lists(self):
        with self.lock:
            data = json.dumps({"active": self.active, "available": self.available,
                               "last_refresh": self.last_refresh}, ensure_ascii=False)
        self.lists_path.write_text(data, encoding="utf-8")

    def _save_cache(self):
        with self.lock:
            data = json.dumps(self.cache, ensure_ascii=False)
        self.cache_path.parent.mkdir(exist_ok=True)
        self.cache_path.write_text(data, encoding="utf-8")