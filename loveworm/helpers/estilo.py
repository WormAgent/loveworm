"""Estilo de escritura de los mensajes: cada regla se puede prender o apagar desde la configuración."""
import math
import random
import re
import unicodedata

# nombre -> (activa por defecto, descripción corta para la interfaz, instrucción para el prompt)
REGLAS = {
    "minusculas": (True, "Todo en minúsculas", "todo en minúsculas"),
    "sin_tildes": (True, "Sin tildes (la ñ se conserva)", "sin tildes"),
    "sin_puntos": (True, "Sin puntos ni suspensivos al cerrar frases", "sin puntos"),
    "una_pregunta": (True, "Preguntas con un solo ? al final, sin ¿",
                     "las preguntas con un solo \"?\" al final (nunca \"¿\")"),
}
DEFAULT_ESTILO = {nombre: activa for nombre, (activa, _, _) in REGLAS.items()}

_TILDES = re.compile("[̀-ͯ]")


def completar(opciones):
    """Opciones guardadas + las reglas que falten, con valores booleanos."""
    return {n: bool((opciones or {}).get(n, DEFAULT_ESTILO[n])) for n in REGLAS}


def descripciones():
    return {n: desc for n, (_, desc, _) in REGLAS.items()}


def instruccion(opciones):
    """Línea para el prompt del borrador; vacía si no hay reglas activas."""
    activas = [REGLAS[n][2] for n, on in completar(opciones).items() if on]
    if not activas:
        return ""
    return "\n\nESTILO DE ESCRITURA (manda sobre todo lo anterior)\n" + "".join(f"- {a}\n" for a in activas)


def _palabras(texto):
    return set(re.findall(r"[a-zñ0-9]{3,}", _sin_tildes(texto.lower())))


def consulta_de(msgs):
    """Lo último que dijo ella (los mensajes seguidos del final del chat); si el último es tuyo, los últimos 3."""
    ultimos = []
    for m in reversed(msgs):
        if m.get("from_me"):
            break
        ultimos.append(m.get("text") or "")
    if not ultimos:
        ultimos = [m.get("text") or "" for m in msgs[-3:]]
    return " ".join(reversed(ultimos[:4]))


def escritos_por_bot(entry):
    """Textos que mandó LoveWorm en ese chat (borradores), para no aprender de ellos."""
    return {t.strip() for t in entry.get("enviados_bot") or []}


def es_tuyo(m, bot):
    """Mensaje que escribiste vos, no el bot."""
    return bool(m.get("from_me")) and (m.get("text") or "").strip() not in bot


def _ventanas(cache, excluir, tramo):
    """Tramos de `tramo` mensajes seguidos que terminan con un mensaje tuyo y tienen al menos 2 tuyos.

    Se saltean los tramos con mensajes del bot: si no, cuanto más se usa, más se copia a sí mismo.
    """
    ventanas = []
    for mid, entry in cache.items():
        if mid == excluir:
            continue
        bot = escritos_por_bot(entry)
        msgs = [m for m in (entry.get("messages") or []) if (m.get("text") or "").strip()]
        if bot:
            # se corta en los mensajes del bot, así ningún tramo los incluye
            msgs = [m if not (m.get("from_me") and m["text"].strip() in bot) else None for m in msgs]
        interes = (entry.get("analysis") or {}).get("interes") or 0
        nombre = entry.get("name") or "Ella"
        for i in range(len(msgs) - tramo + 1):
            bloque = msgs[i:i + tramo]
            if None in bloque:
                continue
            if bloque[-1].get("from_me") and sum(1 for m in bloque if m.get("from_me")) >= 2:
                ella = " ".join(m["text"] for m in bloque if not m.get("from_me"))
                ventanas.append({"mid": mid, "i": i, "nombre": nombre, "bloque": bloque,
                                 "interes": interes, "ella": _palabras(ella)})
    return ventanas


def ejemplos(cache, excluir=None, consulta="", cantidad=10, tramo=5, por_chat=2, parecidos=7):
    """Tramos reales de otros chats para que el modelo copie cómo escribe el dueño.

    Se usan tramos de 5 y no pares porque se suele escribir en ráfagas ("hola" / "cómo estás").
    Los `parecidos` primeros se eligen por parecido con lo último que dijo ella (palabras en común,
    las raras pesan más): así muestran cómo contestaste cosas similares. El resto son generales,
    de chats con más interés. Los más parecidos van al final, pegados a la charla actual.
    """
    ventanas = _ventanas(cache, excluir, tramo)
    if not ventanas:
        return ""

    palabras = _palabras(consulta)
    df = {}
    for v in ventanas:
        for p in v["ella"]:
            df[p] = df.get(p, 0) + 1
    for v in ventanas:
        comunes = palabras & v["ella"]
        v["score"] = sum(math.log(1 + len(ventanas) / df[p]) for p in comunes) / (1 + 0.1 * len(v["ella"]))
        v["azar"] = v["interes"] + random.random() * 40  # desempata y reparte los generales

    elegidos, usados = [], {}

    def tomar(v):
        previos = usados.setdefault(v["mid"], [])
        if len(previos) >= por_chat or any(abs(v["i"] - j) < tramo for j in previos):
            return False
        previos.append(v["i"])
        elegidos.append(v)
        return True

    similares = []
    for v in sorted((v for v in ventanas if v["score"] > 0), key=lambda v: -v["score"]):
        if len(similares) >= parecidos:
            break
        if tomar(v):
            similares.append(v)
    generales = []
    for v in sorted(ventanas, key=lambda v: -v["azar"]):
        if len(similares) + len(generales) >= cantidad:
            break
        if tomar(v):
            generales.append(v)

    orden = generales + similares[::-1]  # el más parecido queda último
    partes = []
    for n, v in enumerate(orden, 1):
        lineas = "\n".join(f"{'Yo' if m.get('from_me') else v['nombre']}: {m['text'].strip()}" for m in v["bloque"])
        partes.append(f"Ejemplo {n}\n{lineas}")
    return ("EJEMPLOS DE CÓMO ESCRIBE (tramos reales de otros chats suyos; los últimos son los más parecidos a "
            "esta charla). Son para imitar su forma de escribir: tono, largo, muletillas, cortar en varios "
            "mensajes cortos, errores y risas. Si en los ejemplos contesta con 3 palabras, contestá con 3 "
            "palabras. Podés reusar sus frases si encajan. No uses datos de esas personas. Las instrucciones "
            "del dueño mandan sobre estos ejemplos: si un ejemplo hace algo que esas instrucciones prohíben, "
            "por ejemplo pedir el Instagram, no lo copies.\n\n" + "\n\n".join(partes))


_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_RISA = re.compile(r"(?:ja|js|je|ji){2,}")


def mensajes_propios(cache):
    """Lo que escribiste vos en todos los chats, sin lo que mandó el bot."""
    textos = []
    for e in cache.values():
        bot = escritos_por_bot(e)
        textos += [m["text"].strip() for m in (e.get("messages") or [])
                   if es_tuyo(m, bot) and (m.get("text") or "").strip()]
    return textos


def estadisticas(cache):
    """Números reales de cómo escribe el dueño; vacío si hay pocos mensajes para que valgan algo."""
    textos = mensajes_propios(cache)
    if len(textos) < 30:
        return ""
    largos = sorted(len(t) for t in textos)
    n = len(textos)

    def pct(cond):
        return round(100 * sum(1 for t in textos if cond(t)) / n)

    return (f"Números reales de sus {n} mensajes: la mitad mide {largos[n // 2]} caracteres o menos y 9 de cada "
            f"10 miden {largos[int(n * 0.9)]} o menos. {pct(lambda t: bool(_RISA.search(t.lower())))}% lleva risa "
            f"(jaja, jsjs), {pct(lambda t: bool(_EMOJI.search(t)))}% lleva emoji, {pct(lambda t: '?' in t)}% "
            f"lleva pregunta y {pct(lambda t: t[0].islower())}% empieza en minúscula.")


def _sin_tildes(text):
    # la ñ se conserva: sacarle la virgulilla cambia la palabra (año -> ano)
    text = text.replace("ñ", "\0n").replace("Ñ", "\0N")
    text = _TILDES.sub("", unicodedata.normalize("NFD", text))
    return unicodedata.normalize("NFC", text.replace("\0n", "ñ").replace("\0N", "Ñ"))


def normalizar(text, opciones=None):
    op = completar(opciones)
    if op["sin_tildes"]:
        text = _sin_tildes(text)
    if op["minusculas"]:
        text = text.lower()
    if op["una_pregunta"]:
        text = re.sub(r"\?+", "?", text.replace("¿", ""))
    if op["sin_puntos"]:
        # puntos de cierre y suspensivos; se conservan los de arroba.usuario, 3.5 o dominios
        text = re.sub(r"\.+(?=\s|$)", "", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()
