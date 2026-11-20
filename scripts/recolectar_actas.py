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
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================================
# CONFIGURACIÓN
# ==========================================================
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "organizacionupcnanses-hub/actas-paritarias-anses")
OWNER, REPO_NAME = REPO_SLUG.split("/", 1)
BASE_PAGES_URL = f"https://{OWNER}.github.io/{REPO_NAME}/pdfs/"

PDF_DIR = Path("pdfs")
JSON_PATH = Path("actas.json")
CACHE_PATH = PDF_DIR / ".cache_fuentes.json"
PDF_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}
TIMEOUT = 25
MAX_PDF_BYTES = 20 * 1024 * 1024   # 20 MB máximo por PDF
MAX_NUEVAS_POR_CORRIDA = 150       # Evita que el primer push sea demasiado pesado
MAX_SEGUNDOS = 600                 # 10 minutos máximo por ejecución

# Dominios WordPress para consultar su biblioteca de PDFs vía API oficial
DOMINIOS_WP = [
    "https://ateanses.org.ar",
    "https://apops.org.ar",
    "https://secasfpi.org.ar",
]

# Páginas HTML a escanear además de la API
FUENTES_HTML = [
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
    "balance", "estatuto", "boletin", "beneficio", "descuento", "revista",
    "denuncia", "folleto", "flyer", "invitacion", "formulario", "planilla",
    "ficha", "reglamento electoral", "declaracion jurada", "instructivo",
    "capacitacion", "curso", "beca", "sorteo", "torneo", "futbol"
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


def get_seguro(url: str, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    try:
        return session.get(url, **kwargs)
    except requests.exceptions.SSLError:
        kwargs["verify"] = False
        return session.get(url, **kwargs)
    except Exception as e:
        print(f"Aviso en {url}: {e}")
        return None


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower().strip()


def es_acta_valida(texto: str, url: str) -> bool:
    combinado = normalizar(f"{texto} {unquote(url)}")
    if any(p in combinado for p in PALABRAS_EXCLUIDAS):
        return False
    claves = [
        "acta", "paritaria", "copeca", "cymat", "ciot",
        "acuerdo", "cct", "305", "complementaria", "audiencia",
        "nomenclador", "reencasillamiento"
    ]
    return any(k in combinado for k in claves)


def detectar_categoria(texto: str, url: str, default: str = "Paritarias") -> str:
    comb = normalizar(f"{texto} {unquote(url)}")
    if "copeca" in comb:
        return "COPECA"
    if "cymat" in comb or "condiciones y medio ambiente" in comb:
        return "CyMAT"
    if "ciot" in comb or "igualdad de oportunidad" in comb or "violencia" in comb:
        return "CIOT"
    return default


def extraer_fecha(texto: str, url: str, fallback_iso: str = "") -> str:
    nombre_pdf = Path(urlparse(unquote(url or "")).path).stem
    candidatos = [texto or "", nombre_pdf, unquote(url or "")]

    for fuente in candidatos:
        # 1. YYYY-MM-DD o YYYY_MM_DD
        m = re.search(r"\b(199\d|20[0-3]\d)[\-_.](\d{1,2})[\-_.](\d{1,2})\b", fuente)
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
        m = re.search(
            r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(MESES_ES.keys()) + r")\s+(?:de\s+|del\s+)?(199\d|20[0-3]\d)\b",
            f_norm,
        )
        if m:
            d, mo, y = int(m.group(1)), MESES_ES[m.group(2)], int(m.group(3))
            if 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"

    # 4. Solo año en título o nombre del archivo (ignorando /uploads/2026/...)
    for fuente in [texto or "", nombre_pdf]:
        m = re.search(r"\b(199[89]|20[0-3]\d)\b", fuente)
        if m:
            return f"{m.group(1)}-01-01"

    if fallback_iso and re.match(r"^\d{4}-\d{2}-\d{2}$", fallback_iso):
        return fallback_iso

    return ""


def formatear_fecha_ar(fecha_iso: str) -> str:
    if not fecha_iso or fecha_iso == "0000-00-00":
        return ""
    partes = fecha_iso.split("-")
    if len(partes) == 3:
        return f"{partes[2]}/{partes[1]}/{partes[0]}"
    return fecha_iso


def limpiar_titulo(texto: str, url: str, categoria: str, fecha_iso: str) -> str:
    raw = BeautifulSoup(texto or "", "html.parser").get_text(" ", strip=True)
    if len(raw) < 5 or normalizar(raw) in {"descargar", "ver", "ver acta", "pdf", "descargar pdf", "abrir", "click aqui", "aquí"}:
        raw = Path(urlparse(unquote(url)).path).stem
        raw = raw.replace("-", " ").replace("_", " ")

    limpio = raw
    for pat in PALABRAS_GREMIOS:
        limpio = re.sub(pat, "", limpio, flags=re.IGNORECASE)

    limpio = re.sub(r"\.pdf$", "", limpio, flags=re.IGNORECASE)
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


def extraer_pdfs_de_html(html_text: str, base_url: str):
    """Encuentra PDFs en <a>, <iframe>, <embed>, <object> y URLs incrustadas en scripts."""
    encontrados = []
    soup = BeautifulSoup(html_text, "html.parser")

    for tag, attr in [("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data")]:
        for el in soup.find_all(tag):
            val = (el.get(attr) or "").strip()
            if not val:
                continue
            full = urljoin(base_url, val)
            if ".pdf" in full.lower():
                # Si viene envuelto en Google Docs Viewer (?url=...)
                m_gdoc = re.search(r"url=(https?://[^&]+\.pdf)", unquote(full), re.IGNORECASE)
                pdf_url = m_gdoc.group(1) if m_gdoc else full.split("#")[0]
                texto = el.get_text(" ", strip=True) if tag == "a" else ""
                encontrados.append((pdf_url, texto))

    # También busca URLs .pdf en texto plano o JSON de Elementor / plugins
    html_unesc = html_text.replace("\\/", "/")
    for match in re.findall(r'https?://[^\s"\'<>\\]+\.pdf(?:\?[^\s"\'<>\\]*)?', html_unesc, re.IGNORECASE):
        encontrados.append((match.split("#")[0], ""))

    return encontrados


def recolectar_desde_wp_api(urls_vistas: set):
    hallados = []
    for base in DOMINIOS_WP:
        print(f"--- Consultando API WordPress en {base} ---")
        # 1. Biblioteca de PDFs (/wp-json/wp/v2/media)
        for pag in range(1, 12):
            api_url = f"{base}/wp-json/wp/v2/media?mime_type=application/pdf&per_page=100&page={pag}"
            resp = get_seguro(api_url)
            if not resp or resp.status_code != 200:
                if pag == 1:
                    print(f"  Media API status: {resp.status_code if resp else 'sin respuesta'}")
                break
            try:
                items = resp.json()
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break

            for it in items:
                src = (it.get("source_url") or "").strip()
                titulo = (it.get("title", {}) or {}).get("rendered", "")
                fecha_pub = (it.get("date") or "")[:10]
                if src and src not in urls_vistas and es_acta_valida(titulo, src):
                    urls_vistas.add(src)
                    hallados.append({
                        "source_url": src,
                        "raw_title": titulo,
                        "default_cat": detectar_categoria(titulo, src, "Paritarias"),
                        "fallback_date": fecha_pub,
                    })
            if len(items) < 100:
                break
            time.sleep(0.2)

        # 2. Posts y páginas con PDFs incrustados (ej: archivos en /archivo/Paritarias/...)
        for endpoint in ["posts", "pages"]:
            for pag in range(1, 6):
                api_url = f"{base}/wp-json/wp/v2/{endpoint}?per_page=50&page={pag}&search=paritaria"
                resp = get_seguro(api_url)
                if not resp or resp.status_code != 200:
                    break
                try:
                    items = resp.json()
                except Exception:
                    break
                if not isinstance(items, list) or not items:
                    break

                for it in items:
                    titulo_post = (it.get("title", {}) or {}).get("rendered", "")
                    contenido = (it.get("content", {}) or {}).get("rendered", "")
                    fecha_pub = (it.get("date") or "")[:10]
                    for pdf_url, link_txt in extraer_pdfs_de_html(contenido, base):
                        titulo_usar = link_txt if len(link_txt) >= 10 else titulo_post
                        if pdf_url not in urls_vistas and es_acta_valida(titulo_usar, pdf_url):
                            urls_vistas.add(pdf_url)
                            hallados.append({
                                "source_url": pdf_url,
                                "raw_title": titulo_usar,
                                "default_cat": detectar_categoria(titulo_usar, pdf_url, "Paritarias"),
                                "fallback_date": fecha_pub,
                            })
                if len(items) < 50:
                    break
                time.sleep(0.2)

    print(f"Candidatos vía WordPress API: {len(hallados)}")
    return hallados


def recolectar_desde_html(urls_vistas: set):
    hallados = []
    posts_visitados = set()

    for fuente in FUENTES_HTML:
        base_url = fuente["url"]
        max_pag = fuente["max_paginas"] if fuente["paginar"] else 1
        print(f"--- Escaneando HTML {base_url} ---")

        for pag in range(1, max_pag + 1):
            page_url = base_url if pag == 1 else urljoin(base_url, f"page/{pag}/")
            resp = get_seguro(page_url)
            if not resp or resp.status_code != 200:
                if pag == 1:
                    print(f"  HTML status: {resp.status_code if resp else 'sin respuesta'}")
                break

            html_text = resp.text
            encontro_algo = False

            for pdf_url, texto in extraer_pdfs_de_html(html_text, page_url):
                if pdf_url not in urls_vistas and es_acta_valida(texto, pdf_url):
                    urls_vistas.add(pdf_url)
                    hallados.append({
                        "source_url": pdf_url,
                        "raw_title": texto,
                        "default_cat": fuente["categoria"],
                        "fallback_date": "",
                    })
                    encontro_algo = True

            if fuente["seguir_posts"]:
                soup = BeautifulSoup(html_text, "html.parser")
                posts_en_pagina = []
                for a in soup.find_all("a", href=True):
                    href = urljoin(page_url, a["href"].strip())
                    texto = a.get_text(" ", strip=True)
                    mismo_dom = urlparse(href).netloc == urlparse(base_url).netloc
                    if mismo_dom and href not in posts_visitados and href != page_url and ".pdf" not in href.lower():
                        comb = normalizar(f"{texto} {href}")
                        if any(k in comb for k in ["acta", "paritaria", "copeca", "cymat", "ciot"]) and "/page/" not in href:
                            posts_visitados.add(href)
                            posts_en_pagina.append((href, texto))
                            encontro_algo = True

                for post_url, post_titulo in posts_en_pagina[:20]:
                    time.sleep(0.15)
                    p_resp = get_seguro(post_url)
                    if not p_resp or p_resp.status_code != 200:
                        continue
                    p_soup = BeautifulSoup(p_resp.text, "html.parser")
                    h1 = p_soup.find("h1")
                    titulo_h1 = h1.get_text(" ", strip=True) if h1 else post_titulo

                    for pdf_url, link_txt in extraer_pdfs_de_html(p_resp.text, post_url):
                        titulo_final = link_txt if len(link_txt) >= 12 else titulo_h1
                        if pdf_url not in urls_vistas and es_acta_valida(titulo_final, pdf_url):
                            urls_vistas.add(pdf_url)
                            hallados.append({
                                "source_url": pdf_url,
                                "raw_title": titulo_final,
                                "default_cat": fuente["categoria"],
                                "fallback_date": "",
                            })

            if not encontro_algo:
                break
            time.sleep(0.2)

    print(f"Candidatos adicionales vía HTML: {len(hallados)}")
    return hallados


def cargar_estado():
    actas_previas = []
    if JSON_PATH.exists():
        try:
            data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("actas"), list):
                actas_previas = data["actas"]
        except Exception:
            pass

    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    return actas_previas, cache


def descargar_y_procesar():
    inicio = time.time()
    existentes, cache_fuentes = cargar_estado()

    actas_finales = []
    hashes_existentes = set()
    archivos_existentes = set()
    fuentes_ya_bajadas = set(cache_fuentes.keys())

    # Conservar actas válidas existentes
    for item in existentes:
        archivo = item.get("archivo", "")
        ruta_local = PDF_DIR / archivo
        if not archivo or not ruta_local.exists():
            continue
        # Limpiar archivos basura de corridas anteriores (como instructivos de denuncia)
        if not es_acta_valida(item.get("titulo", ""), archivo):
            try:
                ruta_local.unlink()
            except Exception:
                pass
            continue

        if item.get("source_url"):
            fuentes_ya_bajadas.add(item["source_url"])
            cache_fuentes[item["source_url"]] = archivo
        if item.get("sha256"):
            hashes_existentes.add(item["sha256"])
        archivos_existentes.add(archivo)

        # Guardar versión limpia sin exponer source_url en el JSON público
        actas_finales.append({
            "id": item.get("id") or (item.get("sha256", "")[:12]),
            "titulo": item.get("titulo", ""),
            "categoria": item.get("categoria", "Paritarias"),
            "fecha": item.get("fecha", ""),
            "fecha_iso": item.get("fecha_iso", "0000-00-00"),
            "anio": item.get("anio", "Sin fecha"),
            "archivo": archivo,
            "url": f"{BASE_PAGES_URL}{archivo}",
            "sha256": item.get("sha256", ""),
        })

    urls_vistas = set()
    candidatos = recolectar_desde_wp_api(urls_vistas) + recolectar_desde_html(urls_vistas)
    print(f"TOTAL candidatos PDF encontrados: {len(candidatos)}")

    nuevas = 0
    for cand in candidatos:
        if nuevas >= MAX_NUEVAS_POR_CORRIDA:
            print(f"Tope de {MAX_NUEVAS_POR_CORRIDA} PDFs nuevos alcanzado; el resto se baja en la próxima corrida.")
            break
        if (time.time() - inicio) > MAX_SEGUNDOS:
            print("Tiempo máximo alcanzado; guardando progreso.")
            break

        src = cand["source_url"]
        if src in fuentes_ya_bajadas:
            continue

        resp = get_seguro(src, stream=True)
        if not resp or resp.status_code != 200:
            continue

        try:
            contenido = resp.content
        except Exception:
            continue

        if len(contenido) < 500 or len(contenido) > MAX_PDF_BYTES:
            continue
        if not contenido[:5].startswith(b"%PDF-"):
            continue

        digest = hashlib.sha256(contenido).hexdigest()
        if digest in hashes_existentes:
            fuentes_ya_bajadas.add(src)
            cache_fuentes[src] = "duplicado"
            continue

        categoria = detectar_categoria(cand["raw_title"], src, cand["default_cat"])
        fecha_iso = extraer_fecha(cand["raw_title"], src, cand.get("fallback_date", ""))
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
        fuentes_ya_bajadas.add(src)
        cache_fuentes[src] = nombre_archivo

        actas_finales.append({
            "id": digest[:12],
            "titulo": titulo,
            "categoria": categoria,
            "fecha": formatear_fecha_ar(fecha_iso),
            "fecha_iso": fecha_iso or "0000-00-00",
            "anio": anio,
            "archivo": nombre_archivo,
            "url": f"{BASE_PAGES_URL}{nombre_archivo}",
            "sha256": digest,
        })
        nuevas += 1
        print(f"[+] ({nuevas}) {nombre_archivo}")
        time.sleep(0.15)

    actas_finales.sort(key=lambda x: (x.get("fecha_iso", "0000-00-00"), x.get("titulo", "")), reverse=True)

    salida = {
        "ultima_actualizacion": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "total": len(actas_finales),
        "actas": actas_finales,
    }

    CACHE_PATH.write_text(json.dumps(cache_fuentes, ensure_ascii=False, indent=2), encoding="utf-8")
    JSON_PATH.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Listo. Actas nuevas: {nuevas} | Total en catálogo: {len(actas_finales)}")


if __name__ == "__main__":
    descargar_y_procesar()
