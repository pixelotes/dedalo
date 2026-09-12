"""Evaluador de expresiones cron e intervalos nativo y ultraligero para Dédalo.

Sustituye a librerías externas pesadas (como croniter + python-dateutil)
ahorrando ~5.4 MB de RAM en el proceso servidor.

Soporta:
- Intervalos: `@every 30m`, `@every 2h`, `@every 45s`
- Atajos estándar: `@hourly`, `@daily`, `@midnight`, `@weekly`, `@monthly`, `@yearly`
- Formato cron estándar de 5 campos: `minuto hora dia_del_mes mes dia_de_la_semana`
  con listas (`1,15,30`), rangos (`1-5`), pasos (`*/10`, `1-5/2`), comodines (`*`).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

SHORTCUTS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _parse_field(field_str: str, min_val: int, max_val: int, is_weekday: bool = False) -> set[int]:
    field_str = field_str.strip()
    if field_str == "*":
        return set(range(min_val, max_val + 1))

    result = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            subparts = part.split("/")
            step = int(subparts[1])
            if subparts[0] == "*":
                start, end = min_val, max_val
            elif "-" in subparts[0]:
                start, end = map(int, subparts[0].split("-"))
            else:
                start, end = int(subparts[0]), max_val
            result.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-"))
            result.update(range(start, end + 1))
        else:
            val = int(part)
            if is_weekday and val == 7:
                val = 0  # 7 es domingo en cron estándar, igual que 0
            result.add(val)
    return {v for v in result if min_val <= v <= max_val}


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parsea una expresión cron de 5 campos y devuelve los conjuntos de valores permitidos."""
    expr = SHORTCUTS.get(expr.strip().lower(), expr.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Expresión cron inválida (debe tener 5 campos): «{expr}»")

    minutes = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    days_m = _parse_field(parts[2], 1, 31)
    months = _parse_field(parts[3], 1, 12)
    days_w = _parse_field(parts[4], 0, 6, is_weekday=True)
    return minutes, hours, days_m, months, days_w


def matches_cron(expr: str, dt: datetime) -> bool:
    """Comprueba si un objeto datetime coincide con la expresión cron."""
    try:
        minutes, hours, days_m, months, days_w = parse_cron(expr)
    except Exception:
        return False

    weekday = (dt.weekday() + 1) % 7  # Python: 0=lunes...6=domingo -> Cron: 0=domingo, 1=lunes...
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in days_m
        and dt.month in months
        and weekday in days_w
    )


def should_run(schedule_expr: str, last_run: float | None, now_ts: float | None = None) -> bool:
    """Determina si un flujo con el schedule especificado debe dispararse ahora."""
    expr = (schedule_expr or "").strip()
    if not expr:
        return False

    now_ts = now_ts if now_ts is not None else time.time()
    now_dt = datetime.fromtimestamp(now_ts)

    # 1. Intervalo: @every 30m, 1h, 45s, etc.
    m_int = re.match(r"^(?:@every\s+)?(\d+)\s*([smh])$", expr, re.IGNORECASE)
    if m_int:
        amount = int(m_int.group(1))
        unit = m_int.group(2).lower()
        delta_sec = amount if unit == "s" else amount * 60 if unit == "m" else amount * 3600
        if last_run is None:
            return True
        return (now_ts - last_run) >= delta_sec

    # 2. Cron estándar (5 campos o atajos)
    if last_run is not None:
        last_dt = datetime.fromtimestamp(last_run)
        # Si ya se ejecutó en este mismo minuto exacto, no duplicar
        if (
            last_dt.year == now_dt.year
            and last_dt.month == now_dt.month
            and last_dt.day == now_dt.day
            and last_dt.hour == now_dt.hour
            and last_dt.minute == now_dt.minute
        ):
            return False

    return matches_cron(expr, now_dt)


def get_next_run(schedule_expr: str, base_dt: datetime | None = None) -> str | None:
    """Calcula la fecha y hora estimada de la próxima ejecución en formato YYYY-MM-DD HH:MM:SS."""
    expr = (schedule_expr or "").strip()
    if not expr:
        return None

    base = base_dt or datetime.now()
    now_ts = base.timestamp()

    m_int = re.match(r"^(?:@every\s+)?(\d+)\s*([smh])$", expr, re.IGNORECASE)
    if m_int:
        amount = int(m_int.group(1))
        unit = m_int.group(2).lower()
        delta_sec = amount if unit == "s" else amount * 60 if unit == "m" else amount * 3600
        next_dt = datetime.fromtimestamp(now_ts + delta_sec)
        return next_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        minutes, hours, days_m, months, days_w = parse_cron(expr)
    except Exception:
        return None

    # Iterar minuto a minuto hasta encontrar el próximo coincidente (máximo 366 días)
    curr = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = curr + timedelta(days=366)
    while curr < limit:
        weekday = (curr.weekday() + 1) % 7
        if (
            curr.minute in minutes
            and curr.hour in hours
            and curr.day in days_m
            and curr.month in months
            and weekday in days_w
        ):
            return curr.strftime("%Y-%m-%d %H:%M:%S")
        curr += timedelta(minutes=1)

    return None
