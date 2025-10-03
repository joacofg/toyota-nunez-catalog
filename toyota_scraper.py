# toyota_scraper.py (VERSIÓN FINAL CORREGIDA)
import os, re, time, pandas as pd
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, Page

# ====== CONFIG ======
SHEET_ID   = os.environ.get("SHEET_ID", "1AoTq1ZeJLsyFnIFiqPZZXxjYdJocW2FvigtAqkOFvX4")
SHEET_TAB  = os.environ.get("SHEET_TAB", "ToyotaCatalogo")
SA_JSON    = os.environ.get("SA_JSON_PATH", "service_account.json")
BASE       = "https://www.toyota.com.ar"
MODELOS    = f"{BASE}/modelos"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_OUT    = os.path.join(SCRIPT_DIR, "toyota_catalogo.csv")

CATEGORIES = ["Autos","Pick-Up","SUV","Comercial","Deportivos","Híbridos"]

CATEGORY_MAP = {
    "yaris-hatchback": "Autos", "corolla": "Autos", "corolla-gr-sport": "Deportivos",
    "corolla-hybrid": "Híbridos", "corolla-cross": "SUV", "corolla-cross-gr-sport": "Deportivos",
    "corolla-cross-hybrid": "Híbridos", "rav4": "SUV", "land-cruiser-300": "SUV",
    "sw4": "SUV", "sw4-diamond": "SUV", "sw4-gr-sport": "Deportivos",
    "hiace-furgon": "Comercial", "hiace-commuter": "Comercial", "hiace-wagon": "Comercial",
    "gr86": "Deportivos", "gr-yaris": "Deportivos", "hilux-dxsr": "Pick-Up",
    "hilux-srvsrx": "Pick-Up", "hilux-gr-sport": "Deportivos", "crown": "Autos",
}

VERSION_OK = re.compile(
    r"\b(XLI|XEI|SEG|XLS\+?|DX|SRV\+?|SRV|SRX|SR|GR|GR-?Sport|HEV|Hybrid|Híbrido|"
    r"CVT|eCVT|AT|MT|AWD|4x2|4x4|1\.5|1\.8|2\.0|2\.4|2\.8)\b", re.I
)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def is_version_line(s: str) -> bool:
    s = clean(s)
    if not s: return False
    if "/" in s: return False
    if re.search(r"[.,;:]", s): return False
    if len(s.split()) > 9: return False
    return bool(VERSION_OK.search(s))

# ---------- utilidades de página ---------- #
def accept_cookies(page):
    for sel in ["#onetrust-accept-btn-handler", "button#onetrust-accept-btn-handler"]:
        try:
            el = page.locator(sel)
            if el.count() and el.first.is_visible(timeout=1500):
                el.first.click(timeout=1500)
                print("[INFO] Cookies aceptadas (OneTrust).")
                return
        except: pass
    for txt in ["Aceptar", "Aceptar todas", "Aceptar todo", "Accept all", "Aceptar cookies"]:
        try:
            page.get_by_role("button", name=re.compile(txt, re.I)).click(timeout=1500)
            print("[INFO] Cookies aceptadas por texto.")
            return
        except: pass

def auto_scroll(page, max_steps=36, step_px=1600, delay=0.25):
    last_h = 0
    for _ in range(max_steps):
        page.evaluate(f"window.scrollBy(0, {step_px});")
        time.sleep(delay)
        h = page.evaluate("document.documentElement.scrollHeight")
        if h == last_h: break
        last_h = h

# ---------- discovery ---------- #
def discover_models(page):
    print(f"[DISCOVERY] Navegando índice {MODELOS}")
    page.goto(MODELOS, timeout=120000, wait_until="domcontentloaded")
    page.set_default_timeout(45000)
    time.sleep(0.6)
    accept_cookies(page)
    auto_scroll(page, max_steps=40)
    out, seen_urls = [], set()

    all_anchors = page.locator("a[href^='/modelos/']").all()
    print(f"[DISCOVERY] Total a[href^='/modelos/'] encontrados: {len(all_anchors)}")

    for anchor in all_anchors:
        try:
            href = anchor.get_attribute("href") or ""
            u = urljoin(BASE, href)
            p = urlparse(u)
            
            if not (p.netloc.endswith("toyota.com.ar") and p.path.startswith("/modelos/")):
                continue
            if u in seen_urls:
                continue

            slug = p.path.strip('/').split("/")[-1]
            cat = CATEGORY_MAP.get(slug, "Desconocido")
            
            out.append((cat, u))
            seen_urls.add(u)
        except Exception:
            continue
            
    print(f"[DISCOVERY] Modelos únicos encontrados: {len(out)}")
    for t, u in out: print(" -", t, u)
    return out

# ---------- scraping de ficha ---------- #
# <<< MODIFICADO Y MEJORADO >>>
def get_model_name(page: Page, url: str) -> str:
    """Extrae el nombre del modelo de la página de forma robusta."""
    # 1. Intento con H1
    try:
        h1 = page.locator("h1").first
        if h1.is_visible(timeout=2000):
            name = clean(h1.inner_text())
            if name and len(name) < 35:
                return name
    except Exception:
        pass

    # 2. Intento con el título de la página
    try:
        title = page.title()
        # Limpia " | Toyota Argentina", etc.
        name = re.sub(r'\|.*', '', title, flags=re.I).strip()
        if name and "toyota" not in name.lower():
            return name
    except Exception:
        pass

    # 3. Fallback infalible con el URL
    try:
        slug = url.strip('/').split('/')[-1]
        name = slug.replace('-', ' ').title()
        return name
    except Exception:
        pass
        
    return "Desconocido"

def get_price(page):
    # (El código de get_price no necesita cambios)
    try:
        blk = page.locator(
            "xpath=(//*[contains(normalize-space(),'Precio sugerido al público') "
            "and not(ancestor::*[contains(.,'Legales')])])[1]"
            "/ancestor::*[self::div or self::section][1]"
        )
        txt = blk.inner_text(timeout=2500)
        m = re.search(r"\$\s*([\d\.\,]+)", txt)
        if m: return m.group(1)
    except: pass
    try:
        node = page.locator(
            "xpath=(//*[contains(normalize-space(),'Precio sugerido al público')])[1]"
            "/following::*[contains(.,'$')][1]"
        )
        txt = node.inner_text(timeout=1500)
        m = re.search(r"\$\s*([\d\.\,]+)", txt)
        if m: return m.group(1)
    except: pass
    try:
        css_nodes = page.locator("div[class*='styles_price'], div[class*='styles_info-container']")
        for i in range(min(4, css_nodes.count())):
            t = css_nodes.nth(i).inner_text(timeout=800)
            m = re.search(r"\$\s*([\d\.\,]+)", t)
            if m: return m.group(1)
    except: pass
    try:
        body = page.locator("body").inner_text()
        body = re.sub(r"Legales[\s\S]+", "", body, flags=re.I)
        m = re.search(r"\$\s*([\d\.\,]{5,})", body)
        if m: return m.group(1)
    except: pass
    return ""

def get_pdf(page):
    try:
        for a in page.locator("a[href$='.pdf']").all():
            href = a.get_attribute("href")
            if href: return urljoin(BASE, href)
    except: pass
    return ""

def get_difs(page):
    try:
        body = page.locator("body").inner_text()
        m = re.search(r"(Razones para tener[\s\S]{0,600}|Diferenciales[\s\S]{0,600})", body, re.I)
        if not m: return ""
        lines = [clean(x) for x in m.group(0).split("\n")]
        items = [x for x in lines if x and len(x) <= 70 and not re.search(r"Razones|Diferenciales", x, re.I)]
        out, seen = [], set()
        for x in items:
            if x not in seen:
                seen.add(x); out.append(x)
            if len(out) == 8: break
        return " · ".join(out)
    except:
        return ""

def scrape_model(page, tipo, url):
    print(f"\n[MODEL] {tipo} :: {url}")
    page.goto(url, timeout=120000, wait_until="domcontentloaded")
    time.sleep(0.5)
    accept_cookies(page)

    # <<< MODIFICADO Y MEJORADO >>> Se pasa el `url` para el fallback
    model_name = get_model_name(page, url)
    print(f"   -> Modelo detectado: '{model_name}'")

    try:
        page.get_by_text("Encontrá tu versión", exact=False).scroll_into_view_if_needed(timeout=5000)
    except: pass
    time.sleep(0.2)

    nodes = page.locator(
        "xpath=//*[self::button or self::a or self::div]"
        "[contains(., 'CVT') or contains(., 'AT') or contains(., 'MT') or contains(., 'HEV') or contains(., '4x') or contains(., 'AWD')]"
    )
    n = min(60, nodes.count())
    versions, seen = [], set()
    for i in range(n):
        t = clean(nodes.nth(i).inner_text())
        if is_version_line(t):
            if "GR YARIS AT GR YARIS MT" in t: t = "GR YARIS AT/MT"
            k = t.lower()
            if k not in seen:
                seen.add(k); versions.append((i, t))

    ficha = get_pdf(page)
    difs  = get_difs(page)
    rows = []

    if versions:
        for i, ver_raw in versions:
            try:
                nodes.nth(i).scroll_into_view_if_needed(timeout=1500)
                nodes.nth(i).click(timeout=1200)
            except: pass
            price = get_price(page)
            
            ver_clean = re.sub(re.escape(model_name), "", ver_raw, flags=re.I).strip()
            if not ver_clean: ver_clean = ver_raw
            
            print(f"   - {model_name} {ver_clean} | ${price or 'NO_PRICE'}")
            rows.append([tipo, model_name, ver_clean, price, "ARS", ficha, difs, url, ""])
    else:
        price = get_price(page)
        print(f"   (sin versiones) {model_name} | ${price or 'NO_PRICE'}")
        rows.append([tipo, model_name, "-", price, "ARS", ficha, difs, url, ""])

    return rows

def write_sheet_service_account(rows, sheet_id, tab_name, sa_json_path):
    # (Sin cambios aquí)
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(sa_json_path, scopes)
    gc     = gspread.authorize(creds)
    ws     = gc.open_by_key(sheet_id).worksheet(tab_name)
    ws.clear()
    header = ["Tipo","Modelo","Versión","PrecioSugerido","Moneda","FichaTecnicaURL","Diferenciales","ModeloURL","MesVigencia"]
    ws.append_row(header)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
    print(f"[SHEETS] Escribí {len(rows)} filas en {sheet_id}/{tab_name}")

def keep_row(version_text: str) -> bool:
    if version_text.strip() == "-": return True
    return is_version_line(version_text)

def main():
    all_rows=[]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="es-AR", timezone_id="America/Argentina/Buenos_Aires",
            geolocation={"latitude": -34.6037, "longitude": -58.3816}, permissions=["geolocation"],
            viewport={"width": 1280, "height": 2200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 DarwinAI-Scraper")
        )
        page = context.new_page()
        page.set_default_timeout(45000)

        targets = discover_models(page)
        for tipo, url in targets:
            try:
                all_rows += scrape_model(page, tipo, url)
            except Exception as e:
                print(f"[ERROR] Falló el scrapeo de {url}: {e}")

        context.close()
        browser.close()

    import datetime, locale as pylocale
    try: pylocale.setlocale(pylocale.LC_TIME, "es_AR.utf8")
    except:
        try: pylocale.setlocale(pylocale.LC_TIME, "es_ES.utf8")
        except: print("[WARN] No se pudo setear el locale a es_AR ni es_ES.")
    mes = datetime.datetime.now().strftime("%b-%Y").title()

    df = pd.DataFrame(all_rows, columns=[
        "Tipo","Modelo","Versión","PrecioSugerido","Moneda","FichaTecnicaURL","Diferenciales","ModeloURL","MesVigencia"
    ])
    df["MesVigencia"] = mes
    before, after = len(df), len(df)
    if "Versión" in df.columns:
        df = df[df["Versión"].apply(keep_row)]
        after = len(df)
    print(f"[FILTER] Filas antes: {before} | después: {after}")

    df.to_csv(CSV_OUT, index=False, encoding="utf-8")
    print(f"[CSV] Guardado {os.path.abspath(CSV_OUT)} con {len(df)} filas")

    if os.path.exists(SA_JSON):
        write_sheet_service_account(df.values.tolist(), SHEET_ID, SHEET_TAB, SA_JSON)
    else:
        print("[WARN] No se encontró service_account.json. Omitiendo la escritura en Google Sheets.")

if __name__ == "__main__":
    main()
