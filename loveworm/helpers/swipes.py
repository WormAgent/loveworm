"""Swipes: la configuración (manual y automática) y la cuenta de likes de cada día.

La automática reparte la meta diaria en tandas cortas con pausas al azar entre una y otra,
dentro de una franja horaria. Así no parece un bot y el navegador queda libre entre tandas
para refrescar chats y mandar mensajes (todo pasa por la misma pestaña).
"""
import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT = {
    "cantidad": 10,     # likes de la tanda manual
    "ratio": 72.5,      # de cada 100 perfiles, a cuántos les da like (si el filtro físico no decide)
    "pausa": 5,         # segundos entre perfiles (varía al azar)
    "auto": False,      # swipes automáticos todos los días
    "por_dia": 40,      # meta de likes por día, contando también los manuales
    "desde": 19,        # franja horaria en la que corre la automática
    "hasta": 23,
}
LIMITES = {"cantidad": (1, 500), "ratio": (0, 100), "pausa": (1, 60), "por_dia": (1, 300),
           "desde": (0, 23), "hasta": (1, 24)}

TANDA = 20                  # likes por tanda automática, como mucho
PAUSA_TANDAS = (15, 40)     # minutos entre tandas


def completar(opciones):
    cfg = dict(DEFAULT)
    for k, v in (opciones or {}).items():
        if k not in DEFAULT:
            continue
        if k == "auto":
            cfg[k] = bool(v)
            continue
        minimo, maximo = LIMITES[k]
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"swipes.{k} no es un número")
        if not minimo <= v <= maximo:
            raise ValueError(f"swipes.{k} tiene que estar entre {minimo} y {maximo}")
        cfg[k] = v if k in ("ratio", "pausa") else int(v)
    if cfg["desde"] >= cfg["hasta"]:
        raise ValueError("La franja de swipes tiene que terminar después de empezar")
    return cfg


def en_franja(cfg, ahora=None):
    hora = (ahora or datetime.now()).hour
    return cfg["desde"] <= hora < cfg["hasta"]


class Registro:
    """Likes y dislikes de hoy en data/swipes.json. Se reinicia solo al cambiar el día."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        # próxima tanda automática (epoch); no se guarda, al reiniciar arranca enseguida si toca
        self.proxima = 0.0
        # hasta cuándo Tinder no deja dar likes (epoch), si avisó
        self.sin_likes_hasta = 0.0

    def hoy(self):
        if self.data.get("fecha") != date.today().isoformat():
            return {"likes": 0, "dislikes": 0}
        return {"likes": self.data.get("likes", 0), "dislikes": self.data.get("dislikes", 0)}

    def sumar(self, likes, dislikes):
        actual = self.hoy()
        historial = self.data.get("historial", {})
        self.data = {"fecha": date.today().isoformat(), "likes": actual["likes"] + likes,
                     "dislikes": actual["dislikes"] + dislikes, "historial": historial}
        historial[self.data["fecha"]] = {"likes": self.data["likes"], "dislikes": self.data["dislikes"]}
        # alcanza con las últimas dos semanas
        for viejo in sorted(historial)[:-14]:
            historial.pop(viejo)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def historial(self):
        h = dict(self.data.get("historial", {}))
        h[date.today().isoformat()] = self.hoy()
        dias = [(date.today() - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
        return [{"fecha": f, **h.get(f, {"likes": 0, "dislikes": 0})} for f in dias]

    def programar_siguiente(self):
        self.proxima = time.time() + random.uniform(*PAUSA_TANDAS) * 60
