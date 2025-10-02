import os, re, time, pandas as pd
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

# ====== CONFIG ======
SHEET_ID   = os.environ.get("SHEET_ID", "1AoTq1ZeJLsyFnIFiqPZZXxjYdJocW2FvigtAqkOFvX4")
SHEET_TAB  = os.environ.get("SHEET_TAB", "ToyotaCatalogo")
SA_JSON    = os.environ.get("SA_JSON_PATH", "service_account.json")
BASE       = "https://www.toyota.com.ar"
MODELOS    = f"{BASE}/modelos"

# Guardar CSV al lado del script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_OUT    = os.path.join(SCRIPT_DIR, "toyota_catalogo.csv")

CATEGORIES = ["Autos","Pick-Up","SUV","Comercial","Deportivos","Híbridos"]

# Heurística de “línea de versión”
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
            if el.count() and el.first.is_visible():
                el.first.click(timeout=1200)
                return
        except: pass
    for txt in ["Aceptar", "Aceptar todas", "Aceptar todo", "Accept all", "Aceptar cookies"]:
        try:
            page.get_by_role("button", name=re.compile(txt, re.I)).click(timeout=1200)
            return
        except: pass

def auto_scroll_until(page, predicate_xpath: str, max_steps=30, step_px=1600, delay=0.25):
    """Scrollea y chequea si aparece el selector XPATH. Devuelve True si aparece."""
    for _ in range(max_steps):
        # ¿ya está?
        if page.locator(predicate_xpath).count() > 0:
            return True
        page.evaluate(f"window.scrollBy(0, {step_px});")
        time.sleep(delay)
    return page.locator(predicate_xpath).count() > 0

# ---------- discovery ---------- #
def discover_models(page):
    """Lee /modelos y arma [(Tipo, URL_modelo), ...] robusto (sin networkidle)."""
    print(f"[DISCOVERY] Navegando índice {MODELOS}")
    # cargar DOM y scripts, sin esperar networkidle
    page.goto(MODELOS, timeout=120000, wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded")
    page.set_default_timeout(60000)  # subimos el default

    time.sleep(0.6)
    accept_cookies(page)

    # Muchos bloques cargan on-scroll. Hacemos scroll + chequeo de 'Ver modelo'
    has_links = auto_scroll_until(page, "//a[normalize-space()='Ver modelo']", max_steps=40)
    anchors_global = page.locator("//a[normalize-space()='Ver modelo']")
    print(f"[DISCOVERY] Anclas globales 'Ver modelo': {anchors_global.count()}  (has_links={has_links})")

    found = []
    # 1) método por categorías (preferido)
    for cat in CATEGORIES:
        anchors = page.locator(
            f"xpath=//a[normalize-space()='Ver modelo' and "
            f"preceding::*[self::h2 or self::h3][1][normalize-space()='{cat}']]"
        )
        cnt = anchors.count()
        if cnt:
            print(f"[DISCOVERY] {cat}: {cnt} modelos")
        for i in range(cnt):
            href = anchors.nth(i).get_attribute("href") or ""
            if not href: continue
            u = urljoin(BASE, href)
            p = urlparse(u)
            if p.netloc.endswith("toyota.com.ar") and p.path.startswith("/modelos/"):
                found.append((cat, u))

    # 2) fallback: si no encontró nada por categorías, tomar todos los 'Ver modelo'
    if not found and anchors_global.count():
        print("[DISCOVERY] Fallback: tomando anchors globales")
        for i in range(anchors_global.count()):
            href = anchors_global.nth(i).get_attribute("href") or ""
            if not href: continue
            u = urljoin(BASE, href)
            p = urlparse(u)
            if p.netloc.endswith("toyota.com.ar") and p.path.startswith("/modelos/"):
                # intentar heading más cercano hacia arriba
                try:
                    tipo = anchors_global.nth(i).evaluate("""
                        (el) => {
                          let n = el;
                          while (n) {
                            let p = n.previousElementSibling;
                            while (p) {
                              if (p.tagName && (p.tagName.toLowerCase()==='h2' || p.tagName.toLowerCase()==='h3')) {
                                return p.textContent.trim();
                              }
                              const h = p.querySelector && p.querySelector('h2,h3');
                              if (h) return h.textContent.trim();
                              p = p.previousElementSibling;
                            }
                            n = n.parentElement;
                          }
                          return '';
                        }
                    """)
                except:
                    tipo = ""
                tipo = clean(tipo) or "Desconocido"
                tipo2 = next((c for c in CATEGORIES if c.lower() in tipo.lower()), tipo)
                found.append((tipo2, u))

    # quitar duplicados manteniendo orden
    seen, out = set(), []
    for cat,u in found:
        if u not in seen:
            seen.add(u); out.append((cat,u))

    print(f"[DISCOVERY] Modelos encontrados: {len(out)}")
    if not out:
        # Dump mínimo para debug en Actions (primeros 1000 chars de body)
        try:
            body = page.content()
            print("[DISCOVERY][DEBUG] Body len:", len(body))
            print("[DISCOVERY][DEBUG] Body head:", body[:1000].replace("\n"," ")[:1000])
        except: pass

    for t,u in out: print(" -", t, u)
    return out

# ---------- scraping de ficha ---------- #
def get_price(page):
    # 1) bloque cercano (evitando 'Legales')
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

    # 2) primer nodo con '$' luego del texto
    try:
        node = page.locator(
            "xpath=(//*[contains(normalize-space(),'Precio sugerido al público')])[1]"
            "/following::*[contains(.,'$')][1]"
        )
        txt = node.inner_text(timeout=1500)
        m = re.search(r"\$\s*([\d\.\,]+)", txt)
        if m: return m.group(1)
    except: pass

    # 3) clases comunes
    try:
        css_nodes = page.locator("div[class*='styles_price'], div[class*='styles_info-container']")
        for i in range(min(4, css_nodes.count())):
            t = css_nodes.nth(i).inner_text(timeout=800)
            m = re.search(r"\$\s*([\d\.\,]+)", t)
            if m: return m.group(1)
    except: pass

    # 4) bruto sin 'Legales'
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
            if href: return href
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
    time.sleep(0.2)
    accept_cookies(page)

    try:
        page.get_by_text("Encontrá tu versión", exact=False).scroll_into_view_if_needed()
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
            if "GR YARIS AT GR YARIS MT" in t:
                t = "GR YARIS AT/MT"
            k = t.lower()
            if k not in seen:
                seen.add(k); versions.append((i, t))

    ficha = get_pdf(page)
    difs  = get_difs(page)
    rows = []

    if versions:
        for i, ver in versions:
            try:
                nodes.nth(i).scroll_into_view_if_needed()
                nodes.nth(i).click(timeout=1200)
            except: pass
            price = get_price(page)
            print(f"   - {ver} | ${price or 'NO_PRICE'}")
            rows.append([tipo, "Toyota", ver, price, "ARS", ficha, difs, url, ""])
    else:
        price = get_price(page)
        titulo = clean(page.title().split("|")[0])
        print(f"   (sin versiones) {titulo} | ${price or 'NO_PRICE'}")
        if price:
            rows.append([tipo, "Toyota", titulo, price, "ARS", ficha, difs, url, ""])

    return rows

# ====== Google Sheets (Service Account) ======
def write_sheet_service_account(rows, sheet_id, tab_name, sa_json_path):
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

def main():
    all_rows=[]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            geolocation={"latitude": -34.6037, "longitude": -58.3816},
            permissions=["geolocation"],
            viewport={"width": 1280, "height": 2200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 DarwinAI-Scraper")
        )
        page = context.new_page()
        page.set_default_timeout(60000)  # más margen

        targets = discover_models(page)
        for tipo, url in targets:
            all_rows += scrape_model(page, tipo, url)

        context.close()
        browser.close()

    df = pd.DataFrame(all_rows, columns=[
        "Tipo","Modelo","Versión","PrecioSugerido","Moneda","FichaTecnicaURL","Diferenciales","ModeloURL","MesVigencia"
    ])
    df = df[df["Versión"].apply(is_version_line)]

    # CSV local para debug
    df.to_csv(CSV_OUT, index=False, encoding="utf-8")
    print(f"[CSV] Guardado {os.path.abspath(CSV_OUT)} con {len(df)} filas")

    write_sheet_service_account(df.values.tolist(), SHEET_ID, SHEET_TAB, SA_JSON)

if __name__ == "__main__":
    main()
