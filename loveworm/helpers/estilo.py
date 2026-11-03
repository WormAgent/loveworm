"""Estilo de escritura de los mensajes: cada regla se puede prender o apagar desde la configuración."""
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
