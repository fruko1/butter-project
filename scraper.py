"""
Denní scraper cen másla z českých supermarketů.

- Rohlik.cz: přímé REST API (bez prohlížeče)
- Kosik.cz, Albert.cz, Billa.cz: Playwright + zachycení síťových odpovědí
"""

import os
import re
import sys
import json
import httpx
from datetime import date
from playwright.sync_api import sync_playwright, Response


def _clean_secret(value: str) -> str:
    """Odstraní všechny non-printable znaky z GitHub Secrets."""
    return re.sub(r"[^\x21-\x7E]", "", value).strip()


TELEGRAM_TOKEN = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}

NOT_BUTTER = [
    "šunka", "klobás", "meloun", "vanilka", "sýr", "jogurt",
    "mléko", "káva", "čaj", "pivo", "víno", "salám", "šlehač",
]


def _is_likely_butter(name: str) -> bool:
    name_lower = name.lower()
    return not any(kw in name_lower for kw in NOT_BUTTER)


def _parse_price(value) -> float | None:
    """Převede různé formáty ceny na float."""
    if isinstance(value, (int, float)) and 10 < float(value) < 1000:
        return float(value)
    if isinstance(value, str):
        match = re.search(r"(\d+)[.,](\d{1,2})", value)
        if match:
            val = float(f"{match.group(1)}.{match.group(2)}")
            return val if 10 < val < 1000 else None
    return None


# ─── ROHLIK.CZ — přímé REST API ───────────────────────────────────────────────

def search_rohlik() -> list[dict]:
    """
    Rohlik.cz — fungující REST endpoint, bez prohlížeče.
    Ověřeno: vrací 171 produktů pro 'maslo'.
    """
    results = []
    try:
        url = "https://www.rohlik.cz/services/frontend-service/search-metadata"
        params = {
            "search": "maslo",
            "offset": 0,
            "limit": 25,
            "companyId": 1,
            "canCorrect": "true",
        }
        r = httpx.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        products = r.json().get("data", {}).get("productList", [])
        for item in products:
            name = item.get("productName", "")
            amount = item.get("textualAmount", "")
            price = _parse_price(item.get("price", {}).get("full"))
            original = _parse_price(item.get("originalPrice", {}).get("full"))
            if not name or not price:
                continue
            is_sale = original and original > price
            results.append({
                "name": f"{name} {amount}".strip()[:70],
                "price": original if is_sale else price,
                "sale_price": price if is_sale else None,
                "store": "Rohlik.cz",
            })
        print(f"[Rohlik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
    return results


# ─── PLAYWRIGHT + NETWORK INTERCEPTION ────────────────────────────────────────

def _find_products(obj, results: list, store: str, depth: int = 0):
    """
    Rekurzivně prohledá JSON odpověď a hledá produkty s názvem a cenou.
    Funguje bez ohledu na konkrétní strukturu API.
    """
    if depth > 7 or not obj:
        return
    if isinstance(obj, dict):
        # Hledat objekt s polem name/title + price
        name = (
            obj.get("name") or obj.get("productName") or obj.get("title")
            or obj.get("displayName") or ""
        )
        price_raw = (
            obj.get("price") or obj.get("currentPrice") or obj.get("salesPrice")
            or obj.get("regularPrice") or obj.get("priceValue")
        )
        # Cena může být zanořená jako {"amount": 39.9} nebo {"value": 39.9}
        if isinstance(price_raw, dict):
            price_raw = (
                price_raw.get("amount") or price_raw.get("value")
                or price_raw.get("regular") or price_raw.get("full")
                or price_raw.get("incVat")
            )
        price = _parse_price(price_raw) if price_raw is not None else None

        if name and price and len(str(name)) > 2:
            if _is_likely_butter(str(name)):
                # Zkusit najít původní cenu (sleva)
                orig_raw = (
                    obj.get("originalPrice") or obj.get("crossedOutPrice")
                    or obj.get("priceBeforeDiscount") or obj.get("recommendedPrice")
                )
                if isinstance(orig_raw, dict):
                    orig_raw = orig_raw.get("amount") or orig_raw.get("value") or orig_raw.get("full")
                original = _parse_price(orig_raw) if orig_raw else None
                is_sale = original and original > price
                results.append({
                    "name": str(name)[:70],
                    "price": original if is_sale else price,
                    "sale_price": price if is_sale else None,
                    "store": store,
                })
            return  # Objekt zpracován, nepokračujeme hlouběji
        else:
            for v in obj.values():
                _find_products(v, results, store, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:100]:
            _find_products(item, results, store, depth + 1)


def _scrape_with_interception(page, url: str, store: str) -> list[dict]:
    """
    Načte stránku Playwright browserem a zachytí všechny JSON API odpovědi.
    Nezávislé na CSS selektorech — funguje pro libovolnou SPA architekturu.
    """
    captured: list[dict] = []
    seen_urls: set[str] = set()

    def on_response(response: Response):
        resp_url = response.url
        # Přeskočit opakované/utility requesty
        skip = ["analytics", "tracking", "gtm", "facebook", "google",
                "sentry", "beacon", "datadog", "hotjar", "clarity"]
        if any(s in resp_url for s in skip):
            return
        if resp_url in seen_urls:
            return
        seen_urls.add(resp_url)

        if response.status != 200:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return
        try:
            data = response.json()
            before = len(captured)
            _find_products(data, captured, store)
            found = len(captured) - before
            if found > 0:
                print(f"[{store}] zachyceno {found} produktů z: {resp_url[:80]}")
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, timeout=45000)
        page.wait_for_load_state("networkidle", timeout=25000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[{store}] Načítání: {e}", file=sys.stderr)
    page.remove_listener("response", on_response)

    # Deduplikace dle jména
    seen = set()
    unique = []
    for r in captured:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique.append(r)

    print(f"[{store}] celkem {len(unique)} unikátních produktů")
    return unique


def scrape_kosik(page) -> list[dict]:
    return _scrape_with_interception(
        page, "https://www.kosik.cz/vyhledavani?q=m%C3%A1slo", "Kosik.cz"
    )


def scrape_albert(page) -> list[dict]:
    return _scrape_with_interception(
        page, "https://www.albert.cz/vyhledavani?q=m%C3%A1slo", "Albert.cz"
    )


def scrape_billa(page) -> list[dict]:
    return _scrape_with_interception(
        page, "https://www.billa.cz/search?q=m%C3%A1slo", "Billa.cz"
    )


# ─── FORMÁTOVÁNÍ A TELEGRAM ───────────────────────────────────────────────────

def _discount_pct(original: float, sale: float) -> int:
    if original <= 0:
        return 0
    return round((1 - sale / original) * 100)


def format_message(results: list[dict]) -> str:
    today = date.today().strftime("%-d. %-m. %Y")

    on_sale = [r for r in results if r.get("sale_price")]
    regular = [r for r in results if not r.get("sale_price")]
    on_sale.sort(key=lambda r: r["sale_price"])
    regular.sort(key=lambda r: r["price"])

    lines = [f"🧈 <b>Ceny másla</b> — {today}\n"]

    if on_sale:
        lines.append("🔴 <b>SLEVY DNES:</b>")
        for r in on_sale[:8]:
            pct = _discount_pct(r["price"], r["sale_price"])
            lines.append(
                f"• {r['name']} — <b>{r['sale_price']:.2f} Kč</b>"
                + (f" (-{pct}%)" if pct > 0 else "")
                + f" @ {r['store']}"
            )
        lines.append("")

    if regular:
        lines.append("💰 <b>CENY:</b>")
        for r in regular[:12]:
            lines.append(f"• {r['name']} — {r['price']:.2f} Kč @ {r['store']}")
        lines.append("")

    all_prices = [r.get("sale_price") or r["price"] for r in results]
    if all_prices:
        avg = sum(all_prices) / len(all_prices)
        lines.append(
            f"📊 Průměr: <b>{avg:.2f} Kč</b> | "
            f"Nejlevnější: <b>{min(all_prices):.2f} Kč</b>"
        )
    else:
        lines.append("⚠️ Dnes se nepodařilo načíst žádné ceny másla.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: Telegram credentials nejsou nastaveny.", file=sys.stderr)
        sys.exit(1)
    api_url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    r = httpx.post(api_url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    if r.status_code != 200:
        print(f"Telegram chyba: {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)
    print("Zpráva odeslána na Telegram.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Stahuji ceny másla...")
    results: list[dict] = []

    # Rohlik — přímé API, bez prohlížeče
    results += search_rohlik()

    # Ostatní — Playwright + síťová interceptace
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="cs-CZ",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        results += scrape_kosik(page)
        results += scrape_albert(page)
        results += scrape_billa(page)
        browser.close()

    print(f"\nCelkem: {len(results)} produktů")
    for r in results[:5]:
        sale_info = f" → sleva {r['sale_price']:.2f} Kč" if r.get("sale_price") else ""
        print(f"  {r['store']}: {r['name']} — {r['price']:.2f} Kč{sale_info}")

    message = format_message(results)
    print("\n--- Telegram zpráva ---")
    print(message)
    print("-----------------------\n")
    send_telegram(message)
