import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# ==========================================================
# CONFIGURACIÓN
# ==========================================================
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "organizacionupcnanses-hub/actas-paritarias-anses")
OWNER, REPO_NAME = REPO_SLUG.split("/", 1)
BASE_PAGES_URL = f"https://{OWNER}.github.io/{REPO_NAME}/pdfs/"

PDF_DIR = Path("pdfs")
JSON_PATH = Path("actas.json")
PDF_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ActasCCT305Bot/1.0)"
}
TIMEOUT = 25
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB máx por archivo

# Fuentes a escanear (el usuario final nunca ve estas URLs)
FUENTES = [
    {"url": "https://ateanses.org.ar/actas-paritarias/", "categoria": "Paritarias", "paginar": True, "max_paginas": 12, "seguir_posts": False},
    {"url": "https://apops.org.ar/paritarias/", "categoria": "Paritarias", "paginar": True, "max_paginas": 10, "seguir_posts": True},
    {"url": "https://apops.org.ar/copeca/", "categoria": "COPECA", "paginar": True, "max_paginas": 5, "seguir_posts": True},
    {"url": "https://apops.org.ar/cymat/", "categoria": "CyMAT", "paginar": True, "max_paginas": 5, "seguir_posts": True},
    {"url": "https://apops.org.ar/ciot/", "categoria": "CIOT", "paginar": True, "max_paginas": 5, "seguir_posts": True},
    {"url": "https://secasfpi.org.ar/actas-paritarias/", "categoria": "Paritarias", "paginar": False, "max_paginas": 1, "seguir_posts": True},
    {"url": "https://secasfpi.org.ar/comunicados/", "categoria": "Paritarias", "paginar": False, "max_paginas": 1, "seguir_posts": True},
]

PALABRAS_EXCLUIDAS = [
    "afiliacion", "afiliate", "turismo", "hotel", "colonia", "camping",
    "cartilla", "subsidio", "chequera", "elecciones", "junta electoral",
    "balance", "estatuto", "boletin", "beneficio", "descuento", "revista"
]

PALABRAS_GREMIOS = [
    r"\bapops\b", r"\bsecasfpi\b", r"\bate\s+anses\b", r"\bate\b",
    r"\bde\s+apops\b", r"\bde\s+secasfpi\b", r"\bde\s+ate\b"
]

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12
}

session = requests.Session()
session.headers.update(HEADERS)


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower().strip()


def es_acta_valida(texto: str, url: str) -> bool:
    combinado = normalizar(f"{texto} {unquote(url)}")
    if any(p in combinado for p in PALABRAS_EXCLUIDAS):
        return False
    claves = ["acta", "paritaria", "copeca", "cymat", "ciot", "acuerdo", "cct", "305", "complementaria", "audiencia", "comision"]
    return any(k in combinado for k in claves)


def detectar_categoria(texto: str, url: str, default: str) -> str:
    comb = normalizar(f"{texto} {unquote(url)}")
    if "copeca" in comb:
        return "COPECA"
    if "cymat" in comb or "condiciones y medio ambiente" in comb:
        return "CyMAT"
    if "ciot" in comb or "igualdad de oportunidad" in comb:
        return "CIOT"
    return default


def extraer_fecha(texto: str, url: str) -> str:
    """Devuelve fecha YYYY-MM-DD si logra detectarla; si no, cadena vacía."""
    candidatos = [texto or "", unquote(url or "")]

    for fuente in candidatos:
        # 1. YYYY-MM-DD o YYYY/MM/DD
        m = re.search(r"\b(199\d|20[0-3]\d)[\-/_](\d{1,2})[\-/_](\d{1,2})\b", fuente)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 2. DD-MM-YYYY o DD/MM/YYYY o DD-MM-YY
        m = re.search(r"\b(\d{1,2})[\-/\._](\d{1,2})[\-/\._](199\d|20[0-3]\d|\d{2})\b", fuente)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000 if y <= 50 else 1900
            if 1995 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 3. "16 de junio de 2022" o "16 junio 2022"
        f_norm = normalizar(fuente)
        m = re.search(r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(MESES_ES.keys()) + r")\s+(?:de\s+|del\s+)?(199\d|20[0-3]\d)\b", f_norm)
        if m:
            d, mo, y = int(m.group(1)), MESES_ES[m.group(2)], int(m.group(3))
            if 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"

    # 4. Solo año
    for fuente in candidatos:
        m = re.search(r"\b(199[89]|20[0-3]\d)\b", fuente)
        if m:
            return f"{m.group(1)}-01-01"

    return ""


def formatear_fecha_ar(fecha_iso: str) -> str:
    if not fecha_iso:
        return ""
    partes = fecha_iso.split("-")
    if len(partes) == 3:
        return f"{partes[2]}/{partes[1]}/{partes[0]}"
    return fecha_iso


def limpiar_titulo(texto: str, url: str, categoria: str, fecha_iso: str) -> str:
    raw = (texto or "").strip()
    if len(raw) < 5 or normalizar(raw) in {"descargar", "ver", "ver acta", "pdf", "descargar pdf", "abrir"}:
        raw = Path(urlparse(unquote(url)).path).stem
        raw = raw.replace("-", " ").replace("_", " ")

    # Quitar menciones a otros gremios para mantener formato institucional limpio
    limpio = raw
    for pat in PALABRAS_GREMIOS:
        limpio = re.sub(pat, "", limpio, flags=re.IGNORECASE)

    limpio = re.sub(r"\s*[\(\[\-\|–—]+\s*$", "", limpio)
    limpio = re.sub(r"^\s*[\)\]\-\|–—]+\s*", "", limpio)
    limpio = re.sub(r"\s{2,}", " ", limpio).strip()

    if len(limpio) < 6:
        fecha_txt = formatear_fecha_ar(fecha_iso)
        prefijo = "Acta Paritaria" if categoria == "Paritarias" else f"Acta {categoria}"
        limpio = f"{prefijo} {fecha_txt}".strip()

    return limpio[:140]


def slug_seguro(texto: str) -> str:
    s = normalizar(texto)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:75] or "acta"


def obtener_html(url: str):
    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"Aviso leyendo {url}: {e}")
    return None


def recolectar_enlaces_pdf():
    hallados = []
    urls_vistas = set()
    posts_visitados = set()

    for fuente in FUENTES:
        base_url = fuente["url"]
        max_pag = fuente["max_paginas"] if fuente["paginar"] else 1

        for pag in range(1, max_pag + 1):
            page_url = base_url if pag == 1 else urljoin(base_url, f"page/{pag}/")
            soup = obtener_html(page_url)
            if not soup:
                break

            encontro_algo_en_pagina = False
            posts_en_pagina = []

            for a in soup.find_all("a", href=True):
                href = urljoin(page_url, a["href"].strip())
                texto = a.get_text(" ", strip=True)
                href_lower = href.lower().split("?")[0]

                if href_lower.endswith(".pdf"):
                    if href not in urls_vistas and es_acta_valida(texto, href):
                        urls_vistas.add(href)
                        hallados.append({
                            "source_url": href,
                            "raw_title": texto,
                            "default_cat": fuente["categoria"],
                        })
                        encontro_algo_en_pagina = True
                elif fuente["seguir_posts"]:
                    # Detectar notas individuales de actas dentro del mismo dominio
                    mismo_dom = urlparse(href).netloc == urlparse(base_url).netloc
                    if mismo_dom and href not in posts_visitados and href != page_url:
                        comb = normalizar(f"{texto} {href}")
                        if any(k in comb for k in ["acta", "paritaria", "copeca", "cymat", "ciot"]) and "/page/" not in href:
                            posts_visitados.add(href)
                            posts_en_pagina.append((href, texto))
                            encontro_algo_en_pagina = True

            # Entrar a los posts detectados para extraer el link directo al .pdf
            for post_url, post_titulo in posts_en_pagina[:25]:
                time.sleep(0.25)
                post_soup = obtener_html(post_url)
                if not post_soup:
                    continue
                h1 = post_soup.find("h1")
                titulo_post = h1.get_text(" ", strip=True) if h1 else post_titulo

                for a in post_soup.find_all("a", href=True):
                    href = urljoin(post_url, a["href"].strip())
                    if href.lower().split("?")[0].endswith(".pdf"):
                        texto_link = a.get_text(" ", strip=True)
                        titulo_final = titulo_post if len(texto_link) < 10 else texto_link
                        if href not in urls_vistas and es_acta_valida(titulo_final, href):
                            urls_vistas.add(href)
                            hallados.append({
                                "source_url": href,
                                "raw_title": titulo_final,
                                "default_cat": fuente["categoria"],
                            })

            if not encontro_algo_en_pagina:
                break
            time.sleep(0.3)

    return hallados


def cargar_catalogo_existente():
    if JSON_PATH.exists():
        try:
            data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("actas"), list):
                return data["actas"]
        except Exception as e:
            print(f"No se pudo leer actas.json previo: {e}")
    return []


def descargar_y_procesar():
    existentes = cargar_catalogo_existente()

    # Índices para no repetir descargas ni duplicar PDFs idénticos
    por_source = {}
    hashes_existentes = set()
    archivos_existentes = set()

    for item in existentes:
        archivo_local = PDF_DIR / item.get("archivo", "")
        if item.get("archivo") and archivo_local.exists():
            if item.get("source_url"):
                por_source[item["source_url"]] = item
            if item.get("sha256"):
                hashes_existentes.add(item["sha256"])
            archivos_existentes.add(item["archivo"])

    actas_finales = list(por_source.values())
    # Conservar también actas agregadas a mano que no tengan source_url
    for item in existentes:
        if not item.get("source_url") and item.get("archivo") and (PDF_DIR / item["archivo"]).exists():
            actas_finales.append(item)

    candidatos = recolectar_enlaces_pdf()
    print(f"Enlaces PDF candidatos encontrados: {len(candidatos)}")

    nuevas = 0
    for cand in candidatos:
        src = cand["source_url"]
        if src in por_source:
            continue

        try:
            resp = session.get(src, timeout=TIMEOUT, stream=True)
            if resp.status_code != 200:
                continue

            contenido = resp.content
            if len(contenido) < 500 or len(contenido) > MAX_PDF_BYTES:
                continue
            # Validar firma real de archivo PDF
            if not contenido[:5].startswith(b"%PDF-"):
                continue

            digest = hashlib.sha256(contenido).hexdigest()
            if digest in hashes_existentes:
                continue

            categoria = detectar_categoria(cand["raw_title"], src, cand["default_cat"])
            fecha_iso = extraer_fecha(cand["raw_title"], src)
            titulo = limpiar_titulo(cand["raw_title"], src, categoria, fecha_iso)
            anio = fecha_iso[:4] if fecha_iso else "Sin fecha"

            prefijo_fecha = fecha_iso if fecha_iso else "sin-fecha"
            nombre_base = f"{prefijo_fecha}-{slug_seguro(titulo)}"
            nombre_archivo = f"{nombre_base}.pdf"
            contador = 2
            while nombre_archivo in archivos_existentes or (PDF_DIR / nombre_archivo).exists():
                nombre_archivo = f"{nombre_base}-{contador}.pdf"
                contador += 1

            (PDF_DIR / nombre_archivo).write_bytes(contenido)
            hashes_existentes.add(digest)
            archivos_existentes.add(nombre_archivo)

            registro = {
                "id": digest[:12],
                "titulo": titulo,
                "categoria": categoria,
                "fecha": formatear_fecha_ar(fecha_iso),
                "fecha_iso": fecha_iso or "0000-00-00",
                "anio": anio,
                "archivo": nombre_archivo,
                "url": f"{BASE_PAGES_URL}{nombre_archivo}",
                "sha256": digest,
                "source_url": src,
            }
            actas_finales.append(registro)
            por_source[src] = registro
            nuevas += 1
            print(f"[+] Guardada: {nombre_archivo}")
            time.sleep(0.2)

        except Exception as e:
            print(f"Error descargando {src}: {e}")

    # Ordenar de la más reciente a la más antigua
    actas_finales.sort(key=lambda x: (x.get("fecha_iso", "0000-00-00"), x.get("titulo", "")), reverse=True)

    salida = {
        "ultima_actualizacion": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "total": len(actas_finales),
        "actas": actas_finales,
    }

    JSON_PATH.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Listo. Actas nuevas: {nuevas} | Total en catálogo: {len(actas_finales)}")


if __name__ == "__main__":
    descargar_y_procesar()
