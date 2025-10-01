import os, re, time, pandas as pd
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

# ====== CONFIG ======
SHEET_ID   = os.environ.get("SHEET_ID", "1AoTq1ZeJLsyFnIFiqPZZXxjYdJocW2FvigtAqkOFvX4")   # <-- poné tu Sheet ID
SHEET_TAB  = os.environ.get("SHEET_TAB", "ToyotaCatalogo")
SA_JSON    = os.environ.get("SA_JSON_PATH", "service_account.json")
BASE       = "https://www.toyota.com.ar"
MODELOS    = f"{BASE}/modelos"

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

def discover_models(page):
    """Lee https://www.toyota.com.ar/modelos y arma [(Tipo, URL_modelo), ...]"""
    page.goto(MODELOS, timeout=120000)
    page.wait_for_load_state("domcontentloaded")

    found = []
    for cat in CATEGORIES:
        # Encuentro el heading por nombre exacto
        h = page.get_by_role("heading", name=cat, exact=True)
        if not h or h.count() == 0:
            continue
        # Tomo todos los anchors “Ver modelo” cuyo heading previo sea 'cat'
        # XPATH: el <a> cuyo texto sea 'Ver modelo' y cuyo <h2> inmediatamente anterior tenga ese texto.
        anchors = page.locator(
            f"xpath=//a[normalize-space()='Ver modelo' and "
            f"preceding::*[self::h2 or self::h3][1][normalize-space()='{cat}']]"
        )
        for i in range(anchors.count()):
            href = anchors.nth(i).get_attribute("href") or ""
            if not href:
                continue
            # solo internos /modelos/...
            u = urljoin(BASE, href)
            p = urlparse(u)
            if p.netloc.endswith("toyota.com.ar") and p.path.startswith("/modelos/"):
                found.append((cat, u))

    # quitar duplicados manteniendo orden
    seen, out = set(), []
    for cat,u in found:
        if u not in seen:
            seen.add(u); out.append((cat,u))
    return out

def get_price(page):
    blk = page.get_by_text("Precio sugerido al público", exact=False).locator("xpath=..")
    try:
        txt = blk.inner_text(timeout=5000)
    except:
        txt = page.locator("body").inner_text()
    m = re.search(r"\$\s*([\d\.\,]+)", txt)
    if m: return m.group(1)
    # Fallback global
    alltxt = page.locator("body").inner_text()
    m = re.search(r"Precio sugerido al público.*?\$\s*([\d\.\,]+)", alltxt, re.I|re.S)
    return m.group(1) if m else ""

def get_pdf(page):
    for a in page.locator("a[href$='.pdf']").all():
        href = a.get_attribute("href")
        if href: return href
    return ""

def get_difs(page):
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

def scrape_model(page, tipo, url):
    page.goto(url, timeout=120000)
    page.wait_for_load_state("domcontentloaded")
    modelo = clean(page.title().split("|")[0])

    # Ir hasta “Encontrá tu versión”
    try:
        page.get_by_text("Encontrá tu versión", exact=False).scroll_into_view_if_needed()
    except:
        pass
    time.sleep(0.2)

    nodes = page.locator(
        "xpath=//*[self::button or self::a or self::div]"
        "[contains(., 'CVT') or contains(., 'AT') or contains(., 'MT') or contains(., 'HEV') or contains(., '4x')]"
    )
    n = min(60, nodes.count())
    versions, seen = [], set()
    for i in range(n):
        t = clean(nodes.nth(i).inner_text())
        if is_version_line(t):
            k = t.lower()
            if k not in seen:
                seen.add(k); versions.append((i, t))

    ficha = get_pdf(page)
    difs  = get_difs(page)
    rows = []
    for i, ver in versions:
        try:
            nodes.nth(i).scroll_into_view_if_needed()
            nodes.nth(i).click(timeout=1500)
        except:
            pass
        price = get_price(page)
        rows.append([tipo, "Toyota", ver, price, "ARS", ficha, difs, url, ""])
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

def main():
    all_rows=[]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        # 1) Descubrir modelos desde /modelos
        targets = discover_models(page)
        # 2) Scrape de cada ficha
        for tipo, url in targets:
            all_rows += scrape_model(page, tipo, url)
        browser.close()

    # filtro defensivo
    df = pd.DataFrame(all_rows, columns=[
        "Tipo","Modelo","Versión","PrecioSugerido","Moneda","FichaTecnicaURL","Diferenciales","ModeloURL","MesVigencia"
    ])
    df = df[df["Versión"].apply(is_version_line)]
    write_sheet_service_account(df.values.tolist(), SHEET_ID, SHEET_TAB, SA_JSON)

if __name__ == "__main__":
    main()
