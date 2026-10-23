"""Bot worker: owns the single Chrome session and runs every browser job on one thread."""
import json
import queue
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from loveworm.helpers.instagram_web import InstagramWeb, extract_handles
from loveworm.helpers.llm import LLMError, analyze_person, draft_message
from loveworm.helpers.tinder_api import TinderApi

# bump when the analysis schema changes so every person gets re-analyzed
ANALYSIS_VERSION = "persona-v1"

DEFAULT_SETTINGS = {
    "instructions": (
        "Escribí corto y natural, en español rioplatense, como en un chat de verdad. "
        "Nada de frases armadas, piropos genéricos ni muchos emojis. En el primer mensaje, "
        "comentá algo concreto de su bio; si no tiene bio, un saludo simple con una pregunta. "
        "Si la charla viene bien, proponé seguirla por Instagram o tomar algo."
    ),
    "model": "haiku",
    # when on, claude drafts openers for matches created after drafts_since and replies for chats it opened
    "auto_drafts": False,
    "drafts_since": None,
    # set once the instagram tab logged in; refreshes then also read instagram
    "instagram": False,
}
MODELS = ("haiku", "sonnet", "opus")

# paused: instagram web no longer serves DMs through its api and reading them on screen is not
# reliable yet. While off, nothing opens instagram and the dashboard hides its panel.
INSTAGRAM_ENABLED = False

# instagram is read profile by profile on screen, so keep each run short
IG_MAX_PER_RUN = 30
IG_REREAD_SECONDS = 24 * 3600


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
        self.cache = self._load_cache()
        self.settings = self._load_settings()

        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.stop_event = threading.Event()
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

        threading.Thread(target=self._loop, daemon=True).start()
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
        with self.lock:
            entry = self.cache.get(match_id)
            if entry:
                entry.pop("draft", None)
                entry.pop("draft_requested", None)
                entry["draft_dismissed"] = _chat_key(entry)
        self._save_cache()

    def resume_llm(self):
        self.llm_paused = False
        with self.lock:
            for entry in self.cache.values():
                entry.pop("analysis_error", None)
                entry.pop("draft_error", None)
        self.log("Claude reanudado.")
        self._schedule_llm()

    def get_settings(self):
        return dict(self.settings, models=MODELS)

    def update_settings(self, instructions, model, auto_drafts):
        if model not in MODELS:
            raise ValueError("Modelo inválido")
        if auto_drafts and not self.settings["auto_drafts"]:
            # only matches that arrive from now on get an opener
            self.settings["drafts_since"] = _now_iso()
        self.settings.update(instructions=instructions.strip() or DEFAULT_SETTINGS["instructions"],
                             model=model, auto_drafts=auto_drafts)
        self._save_settings()
        self.log("Configuración guardada.")
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
            "instagram": INSTAGRAM_ENABLED and self.settings["instagram"],
            "instagram_enabled": INSTAGRAM_ENABLED,
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

    def _job_swipe(self, amount, ratio, sleep):
        self._require_session()
        self._tab("tinder")
        self.stop_event.clear()
        self.status = "swipeando"
        self.log(f"Swipe: {amount} likes, ratio {ratio}%, sleep {sleep}s.")
        try:
            self.session.like(amount=amount, ratio=f"{ratio}%", sleep=sleep, stop_event=self.stop_event)
        finally:
            self.status = "listo"
        self.log("Swipe terminado.")

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

    def _job_refresh(self):
        self._require_session()
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
                if entry.get("draft") and entry["draft"].get("for") != _chat_key(entry):
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
        self._schedule_llm()
        if INSTAGRAM_ENABLED and self.settings["instagram"]:
            self.jobs.put(("refresh_instagram", {}))

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

    def _job_send(self, match_id, text):
        self._require_session()
        TinderApi(self._tab("tinder")).send_message(match_id, text)
        with self.lock:
            entry = self.cache.setdefault(match_id, {})
            entry.pop("draft", None)
            entry.pop("draft_requested", None)
            # chats opened or answered from the dashboard keep getting reply drafts
            entry["managed"] = True
            name = entry.get("name", "?")
        self._save_cache()
        self.log(f"Mensaje enviado a {name}.")
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
            "draft": draft["text"] if draft else None,
            "draft_pending": bool(entry.get("draft_queued")),
            "draft_error": entry.get("draft_error"),
            "managed": entry.get("managed", False),
        }

    def _wants_draft(self, entry):
        msgs = entry.get("messages") or []
        if entry.get("draft") or entry.get("draft_error") or entry.get("draft_dismissed") == _chat_key(entry):
            return False
        if entry.get("draft_requested"):
            return True
        if not self.settings["auto_drafts"]:
            return False
        if not msgs:
            since = self.settings.get("drafts_since") or ""
            return (entry.get("matched") or "") >= since
        # reply only in chats it opened, and only when they wrote last
        return entry.get("managed", False) and not msgs[-1]["from_me"]

    def _schedule_llm(self):
        if self.llm_paused:
            return
        tasks = []
        with self.lock:
            for mid, e in self.cache.items():
                msgs = e.get("messages") or []
                key = f"{ANALYSIS_VERSION}|{_chat_key(e)}"
                if msgs and e.get("analysis_key") != key and not e.get("analysis_queued") and not e.get("analysis_error"):
                    if not any(not m["from_me"] for m in msgs + _ig_messages(e)):
                        # never answered: no need to ask claude
                        e["analysis"] = {"perfil": e.get("bio", ""), "charla": "No respondió ningún mensaje.",
                                         "interes": 0, "senal": "No respondió"}
                        e["analysis_key"] = key
                    else:
                        e["analysis_queued"] = True
                        tasks.append((self._analyze, mid))
                if self._wants_draft(e) and not e.get("draft_queued"):
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
            if "limit" in str(e).lower():
                # usage limit: leave it pending and stop the rest of the queue
                if not self.llm_paused:
                    self.llm_paused = True
                    self.log(f"Claude pausado, llegó al límite de uso: {e}")
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
        name, gender, bio, msgs, ig_msgs, key = self._snapshot(match_id)
        result = self._call_llm(match_id, "analysis", lambda: analyze_person(
            name, gender, bio, msgs, ig_messages=ig_msgs, model=self.settings["model"]))
        if result:
            with self.lock:
                self.cache[match_id]["analysis"] = result
                self.cache[match_id]["analysis_key"] = f"{ANALYSIS_VERSION}|{key}"
        self._save_cache()

    def _draft(self, match_id):
        name, gender, bio, msgs, _, key = self._snapshot(match_id)
        text = self._call_llm(match_id, "draft", lambda: draft_message(
            name, gender, bio, msgs, self.settings["instructions"], model=self.settings["model"]))
        if text:
            with self.lock:
                entry = self.cache[match_id]
                entry["draft"] = {"text": text, "for": key}
                entry.pop("draft_requested", None)
        self._save_cache()

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
                if "limit" in (entry.get(k) or "").lower():
                    entry.pop(k)
            # drop drafts that no longer match the chat (new messages arrived)
            if entry.get("draft") and entry["draft"].get("for") != _chat_key(entry):
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
