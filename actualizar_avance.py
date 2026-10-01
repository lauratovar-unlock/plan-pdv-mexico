"""
Actualiza el avance real de index.html con los datos de la pestaña del mes
("Summary Oct" o "Summary Octubre", la que tenga el formato correcto).

Lo dispara cron-job.org con un repository_dispatch (evento
"actualizar-avance"); ver .github/workflows/actualizar-avance.yml.

Reescribe en index.html solo tres líneas de datos:
  - AGENTS: agentes activos y su meta (columnas entre B y TOTAL, filas
    "Objetivo M0/M1/M2").
  - AVANCE: lo que lleva cada agente [M0, M1, M2] (primer bloque de filas
    "M0"/"M1"/"M2").
  - AVANCE_AL: último día hábil completo (ayer hábil), igual que los bots.

Si la planilla tiene errores de fórmula (#N/A, #REF!...) no toca nada: la
página queda como estaba y el log lo explica.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

SPREADSHEET_ID = "13nW6Pi83FM7e5N-Ctg8O5dbCEE1hvW2MNpuQwj62z3M"
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre",
    12: "Diciembre",
}
COHORTES = ["M0", "M1", "M2"]
AGENTES_EXCLUIDOS = {"Valentina Molina"}
NOMBRES_CORREGIDOS = {"Irina Sstanley": "Irina Stanley"}


# ── Lectura de la planilla ───────────────────────────────────────────────
def sesion_google():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}"}


def api(headers, path, **params):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def leer_pestana(headers, hoy):
    titulos = {
        s["properties"]["title"]
        for s in api(headers, "", fields="sheets.properties.title")["sheets"]
    }
    mes = MESES_ES[hoy.month]
    for nombre in (f"Summary {mes}", f"Summary {mes[:3]}"):
        if nombre not in titulos:
            continue
        rows = api(headers, f"/values/'{nombre}'!A1:AF250").get("values", [])
        if rows and rows[0] and rows[0][0].strip() == "Día" and fila(rows, "% Total") is not None:
            return nombre, rows
    raise ValueError(f"No encontré la pestaña del mes ({mes}).")


# ── Parseo (misma lógica que metas.py de los bots) ───────────────────────
def fila(rows, etiqueta):
    for i, r in enumerate(rows):
        if r and r[0].strip().startswith(etiqueta):
            return i
    return None


def celda(r, i):
    return r[i] if i < len(r) else ""


def numero(v):
    try:
        return int(round(float(str(v).replace(",", "").replace("%", "").strip())))
    except ValueError:
        return 0


def normalizar(nombre):
    nombre = NOMBRES_CORREGIDOS.get(nombre, nombre)
    return nombre.title() if nombre.islower() else nombre


def parse_agentes(rows):
    header = [(c or "").strip() for c in rows[0]]
    fin = header.index("TOTAL")
    actual = {c: rows[fila(rows, c)] for c in COHORTES}
    meta = {c: rows[fila(rows, f"Objetivo {c}")] for c in COHORTES}

    errores = []
    for c in COHORTES:
        for etiqueta, r in ((c, actual[c]), (f"Objetivo {c}", meta[c])):
            malos = [i for i in range(1, fin) if header[i] and str(celda(r, i)).strip().startswith("#")]
            if malos:
                errores.append(f"fila '{etiqueta}': {celda(r, malos[0])} en {len(malos)} agente(s)")
    if errores:
        raise ValueError("La planilla tiene errores de fórmula: " + "; ".join(errores))

    agentes = []
    for i in range(1, fin):
        nombre = header[i]
        if not nombre or nombre in AGENTES_EXCLUIDOS:
            continue
        m = [numero(celda(meta[c], i)) for c in COHORTES]
        if not any(m):
            continue
        agentes.append((normalizar(nombre), m, [numero(celda(actual[c], i)) for c in COHORTES]))
    return agentes


def ultimo_habil_completo(hoy):
    """Último día hábil antes de hoy dentro del mes, o None si no hay."""
    d = hoy - timedelta(days=1)
    while d.month == hoy.month:
        if d.weekday() < 5:
            return d
        d -= timedelta(days=1)
    return None


# ── Escritura de index.html ──────────────────────────────────────────────
def reemplazar(html, patron, nuevo):
    html2, n = re.subn(patron, lambda _: nuevo, html, count=1, flags=re.S)
    if n != 1:
        raise ValueError(f"No encontré en index.html: {patron}")
    return html2


def actualizar_html(html, agentes, avance_al):
    lista = ",\n".join(f"  [{json.dumps(n, ensure_ascii=False)},{json.dumps(m)}]" for n, m, _ in agentes)
    avance = json.dumps({n: a for n, _, a in agentes}, ensure_ascii=False, separators=(",", ":"))
    html = reemplazar(html, r"const AGENTS = \[.*?\n\];", f"const AGENTS = [\n{lista}\n];")
    html = reemplazar(html, r"const AVANCE = [^\n]*;[^\n]*", f"const AVANCE = {avance};")
    html = reemplazar(html, r"const AVANCE_AL = [^\n]*;[^\n]*", f'const AVANCE_AL = "{avance_al.isoformat()}";')
    return html


def main():
    hoy = datetime.now(TZ).date()
    # Se lee la planilla siempre, aunque no haya avance para mostrar, así cada
    # corrida confirma que la llave de Google funciona.
    try:
        nombre, rows = leer_pestana(sesion_google(), hoy)
        print(f"Planilla leída: pestaña '{nombre}'.")
        agentes = parse_agentes(rows)
    except ValueError as e:
        print(f"No actualizo la página: {e}")
        return
    avance_al = ultimo_habil_completo(hoy)
    if avance_al is None:
        print("Todavía no pasó ningún día hábil del mes: no hay avance para mostrar.")
        return
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    nuevo = actualizar_html(html, agentes, avance_al)
    if nuevo == html:
        print("Sin cambios.")
        return
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(nuevo)
    print(f"Actualizado desde '{nombre}': {len(agentes)} agentes, avance al {avance_al}.")


if __name__ == "__main__":
    sys.exit(main())
