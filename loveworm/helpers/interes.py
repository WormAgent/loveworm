"""Señales duras de interés, calculadas del chat sin llamar al modelo.

Devuelve un score de 0 a 100 en la misma escala que el "interes" que calcula Claude, para que
los dos se puedan mezclar. Solo mira lo que ya está guardado: quién escribió último, cuánto
tardó en contestar, cuánto escribe y qué propuso.

Suelto, contra el cache guardado:
    .\\.venv\\Scripts\\python.exe -m loveworm.helpers.interes
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# --- detectores de texto, solo sobre lo que escribio ella ---

_SALIR = re.compile(
    r"\b(salimos|salir|vernos|nos vemos|juntamos|juntarse|tomar algo|tomarnos algo|"
    r"una cerveza|un cafe|un café|cita|encontrarnos|cuando salimos|pasas? por|"
    r"paso por|vamos a|te parece si)\b",
    re.IGNORECASE,
)
_IG = re.compile(r"(?<![\w.])@([a-z0-9._]{3,30})|(?:\b(?:ig|insta|instagram)\b)", re.IGNORECASE)
_NUMERO_TOKEN = re.compile(r"\+?\d[\d\s\-\.]{6,}\d")
_CONTACTO = re.compile(r"\b(whats?app|wsp|wpp|telefono|teléfono|celu|numero|número)\b", re.IGNORECASE)

# mezcla con el interes que ya calcula Claude, cuando existe
PESO_DURO = 0.65
PESO_LLM = 0.35


def _ts(valor):
    """Fecha ISO del cache a datetime con zona. None si no se puede leer."""
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha


def _parece_numero(texto):
    for token in _NUMERO_TOKEN.findall(texto or ""):
        if 8 <= len(re.sub(r"\D", "", token)) <= 15:
            return True
    return False


def _medio(valores):
    return sum(valores) / len(valores) if valores else 0.0


def _latencia_media_min(mensajes):
    """Minutos promedio entre un mensaje mio y la respuesta de ella."""
    huecos = []
    for antes, despues in zip(mensajes, mensajes[1:]):
        if antes.get("from_me") and not despues.get("from_me"):
            a, b = _ts(antes.get("sent")), _ts(despues.get("sent"))
            if a and b and b >= a:
                huecos.append((b - a).total_seconds() / 60)
    return _medio(huecos) if huecos else None


def analizar(mensajes, ig_mensajes=None, ahora=None):
    """Señales duras del chat. mensajes: [{"from_me", "text", "sent"}] de más viejo a más nuevo."""
    ahora = ahora or datetime.now(timezone.utc)
    mensajes = mensajes or []
    ig_mensajes = ig_mensajes or []

    suyos = [m for m in mensajes if not m.get("from_me")]
    mios = [m for m in mensajes if m.get("from_me")]
    suyos_ig = [m for m in ig_mensajes if not m.get("from_me")]

    textos_suyos = [m.get("text") or "" for m in suyos + suyos_ig]
    textos_mios = [m.get("text") or "" for m in mios]

    ultimo = mensajes[-1] if mensajes else None
    ultimo_de = None if not ultimo else ("yo" if ultimo.get("from_me") else "ella")
    ultimo_ts = _ts(ultimo.get("sent")) if ultimo else None
    horas_desde_ultimo = round((ahora - ultimo_ts).total_seconds() / 3600, 1) if ultimo_ts else None

    latencia = _latencia_media_min(mensajes)
    largo_suyo = _medio([len(t) for t in textos_suyos])
    largo_mio = _medio([len(t) for t in textos_mios])
    ratio_largo = round(largo_suyo / largo_mio, 2) if largo_mio else 0.0

    senales = {
        "mensajes_suyos": len(suyos) + len(suyos_ig),
        "mensajes_mios": len(mios),
        "ultimo_de": ultimo_de,
        "horas_desde_ultimo": horas_desde_ultimo,
        "latencia_media_min": round(latencia, 1) if latencia is not None else None,
        "ratio_largo": ratio_largo,
        "preguntas_suyas": sum(1 for t in textos_suyos if "?" in t),
        "ella_inicio": bool(mensajes) and not mensajes[0].get("from_me"),
        "dio_ig": any(_IG.search(t) for t in textos_suyos),
        "dio_numero": any(_parece_numero(t) for t in textos_suyos)
                      or any(_CONTACTO.search(t) for t in textos_suyos),
        "propuso_salir": any(_SALIR.search(t) for t in textos_suyos),
    }

    # --- score: solo tiene sentido si alguna vez contesto ---
    if not textos_suyos:
        senales["score"] = 0
        senales["senal"] = "No respondió"
        return senales

    score = 8.0  # contestó
    score += min(len(textos_suyos), 10) * 1.5
    score += min(senales["preguntas_suyas"], 4) * 4

    if ratio_largo >= 1.2:
        score += 12
    elif ratio_largo >= 0.7:
        score += 8
    elif ratio_largo >= 0.4:
        score += 4

    if latencia is not None:
        if latencia <= 30:
            score += 12
        elif latencia <= 180:
            score += 8
        elif latencia <= 720:
            score += 4
        elif latencia <= 2880:
            score += 1

    if ultimo_de == "ella":
        score += 8
    elif horas_desde_ultimo is not None:
        score += 4 if horas_desde_ultimo <= 48 else -4

    if senales["ella_inicio"]:
        score += 5
    if senales["dio_ig"]:
        score += 12
    if senales["dio_numero"]:
        score += 12
    if senales["propuso_salir"]:
        score += 15

    senales["score"] = max(0, min(int(round(score)), 100))
    senales["senal"] = _senal(senales)
    return senales


def _senal(s):
    """La señal de interés más fuerte, en pocas palabras."""
    if s["propuso_salir"]:
        return "Propuso salir"
    if s["dio_numero"]:
        return "Pasó el número"
    if s["dio_ig"]:
        return "Pasó el Instagram"
    if s["preguntas_suyas"] >= 3:
        return "Pregunta mucho"
    if s["ultimo_de"] == "yo" and (s["horas_desde_ultimo"] or 0) > 48:
        return "No responde"
    if s["ratio_largo"] >= 1.2:
        return "Escribe largo"
    if s["mensajes_suyos"] <= 2:
        return "Responde corto"
    return "Contesta"


def score_final(entry, ahora=None):
    """Score de interés en mí. Señales duras + lo que leyó Claude, si está disponible.

    entry: la entrada del cache (con "messages" e, idealmente, "analysis").
    """
    senales = analizar(entry.get("messages"), _ig(entry), ahora=ahora)
    llm = (entry.get("analysis") or {}).get("interes")
    if llm is None:
        return senales["score"]
    return int(round(PESO_DURO * senales["score"] + PESO_LLM * int(llm)))


def _ig(entry):
    return (entry.get("instagram") or {}).get("messages") or []


# --- prueba suelta: ordena tus chats actuales por interés ---

def _main():
    raiz = Path(__file__).resolve().parents[2]
    cache_path = raiz / "data" / "dashboard_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"No encontré {cache_path}. Abrí el dashboard una vez para que se genere.")
        return

    filas = []
    for mid, entry in cache.items():
        senales = analizar(entry.get("messages"), _ig(entry))
        filas.append((senales["score"], entry.get("name", "?"), senales))
    filas.sort(key=lambda f: f[0], reverse=True)

    print(f"{len(filas)} chats, de mayor a menor interés:\n")
    for score, nombre, s in filas:
        lat = f"{s['latencia_media_min']:.0f}min" if s["latencia_media_min"] is not None else "-"
        print(f"{score:>3}  {nombre[:22]:<22}  {s['senal']:<16}  "
              f"suyos={s['mensajes_suyos']:<3} míos={s['mensajes_mios']:<3} "
              f"lat={lat:<7} largo={s['ratio_largo']}")


if __name__ == "__main__":
    _main()