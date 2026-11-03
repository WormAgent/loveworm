"""Estilo de escritura de los mensajes: cada regla se puede prender o apagar desde la configuración."""
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


def ejemplos(cache, excluir=None, cantidad=8, tramo=5, por_chat=2):
    """Tramos de `tramo` mensajes seguidos de otros chats, para que el modelo copie cómo escribe el dueño.

    Se usan tramos y no pares porque se suele escribir en ráfagas ("hola" / "cómo estás").
    Solo entran tramos donde el dueño escribió al menos 2 mensajes. Se prefieren los chats
    con más interés de ella y se reparten entre chats distintos.
    """
    candidatos = []
    for mid, entry in cache.items():
        if mid == excluir:
            continue
        msgs = [m for m in (entry.get("messages") or []) if (m.get("text") or "").strip()]
        interes = (entry.get("analysis") or {}).get("interes") or 0
        nombre = entry.get("name") or "Ella"
        tramos = []
        for i in range(0, len(msgs) - tramo + 1, tramo):
            bloque = msgs[i:i + tramo]
            if sum(1 for m in bloque if m.get("from_me")) >= 2:
                tramos.append((nombre, bloque))
        if tramos:
            candidatos.append((interes, mid, tramos))

    candidatos.sort(key=lambda c: -c[0])
    elegidos = []
    for _, _, tramos in candidatos:
        elegidos += random.sample(tramos, min(por_chat, len(tramos)))
        if len(elegidos) >= cantidad:
            break
    elegidos = elegidos[:cantidad]
    if not elegidos:
        return ""

    partes = []
    for n, (nombre, bloque) in enumerate(elegidos, 1):
        lineas = "\n".join(f"{'Yo' if m.get('from_me') else nombre}: {m['text'].strip()}" for m in bloque)
        partes.append(f"Ejemplo {n}\n{lineas}")
    return ("EJEMPLOS DE CÓMO ESCRIBE (tramos reales de otros chats suyos; son solo para imitar su forma de "
            "escribir: tono, largo, muletillas, cortar en varios mensajes cortos. Podés reusar sus frases si "
            "encajan. No uses datos de esas personas. Las instrucciones del dueño del system prompt mandan sobre "
            "estos ejemplos: si un ejemplo hace algo que esas instrucciones prohíben, por ejemplo pedir el "
            "Instagram, no lo copies.)\n\n" + "\n\n".join(partes))


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
