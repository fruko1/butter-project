"""
Denní scraper cen másla z českých supermarketů.
Posílá přehled cen a slev na Telegram každý den ráno.
"""

import os
import sys
import httpx
from datetime import date
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}


def search_rohlik() -> list[dict]:
    """Rohlik.cz — neoficiální interní API."""
    results = []
    try:
        url = "https://www.rohlik.cz/api/v1/products"
        params = {"query": "máslo", "limit": 20, "offset": 0}
        r = httpx.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            name = item.get("name", "")
            # Rohlik vrací cenu v haléřích nebo jako float
            price_data = item.get("price", {})
            price_full = price_data.get("full") or price_data.get("amount")
            price_sale = price_data.get("sale") or price_data.get("discountedAmount")
            if not price_full:
                continue
            results.append({
                "name": name,
                "price": float(price_full),
                "sale_price": float(price_sale) if price_sale else None,
                "store": "Rohlik.cz",
            })
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
    return results


def search_kosik() -> list[dict]:
    """Kosik.cz — neoficiální interní API."""
    results = []
    try:
        url = "https://api.kosik.cz/api/v1/products/"
        params = {"search": "máslo", "limit": 20}
        r = httpx.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", {}).get("products", []):
            name = item.get("name", "")
            price = item.get("price")
            original_price = item.get("originalPrice")
            if not price:
                continue
            results.append({
                "name": name,
                "price": float(price),
                "sale_price": float(price) if original_price and original_price > price else None,
                "store": "Kosik.cz",
            })
    except Exception as e:
        print(f"[Kosik] Chyba: {e}", file=sys.stderr)
    return results


def search_albert() -> list[dict]:
    """Albert.cz — scraping HTML výsledků vyhledávání."""
    results = []
    try:
        url = "https://www.albert.cz/search/"
        params = {"q": "máslo"}
        headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml"}
        r = httpx.get(url, params=params, headers=headers, timeout=15, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # Albert používá data atributy nebo JSON-LD pro ceny
        # Hledáme produktové karty
        for card in soup.select("[data-testid='product-tile'], .product-tile, article.product"):
            name_el = card.select_one("[data-testid='product-tile-name'], .product-tile__title, h3")
            price_el = card.select_one("[data-testid='product-tile-price'], .price__amount, .product-tile__price")
            if not name_el or not price_el:
                continue
            name = name_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True).replace("\xa0", " ").replace(",", ".")
            # Extrahovat číslo z textu jako "39.90 Kč"
            price_num = _parse_price(price_text)
            if not price_num:
                continue
            # Kontrola slevy
            old_price_el = card.select_one(".price--original, .price__original, .product-tile__old-price")
            sale_price = price_num if old_price_el else None
            results.append({
                "name": name,
                "price": price_num,
                "sale_price": sale_price,
                "store": "Albert.cz",
            })
    except Exception as e:
        print(f"[Albert] Chyba: {e}", file=sys.stderr)
    return results


def search_billa() -> list[dict]:
    """Billa.cz — interní API endpointu."""
    results = []
    try:
        url = "https://shop.billa.cz/api/product-discover/"
        params = {"search": "máslo", "pageSize": 20}
        headers = {
            **HEADERS,
            "Referer": "https://shop.billa.cz/",
            "Origin": "https://shop.billa.cz",
        }
        r = httpx.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", {}).get("products", data.get("products", [])):
            name = item.get("name", "")
            price = item.get("price", {}).get("regular") or item.get("price")
            sale = item.get("price", {}).get("sale") or item.get("salePrice")
            if not price:
                continue
            results.append({
                "name": name,
                "price": float(price),
                "sale_price": float(sale) if sale else None,
                "store": "Billa.cz",
            })
    except Exception as e:
        print(f"[Billa] Chyba: {e}", file=sys.stderr)
    return results


def _parse_price(text: str) -> float | None:
    """Extrahuje číslo z textu jako '39,90 Kč' nebo '39.90'."""
    import re
    match = re.search(r"(\d+)[.,](\d{1,2})", text)
    if match:
        return float(f"{match.group(1)}.{match.group(2)}")
    match = re.search(r"(\d+)", text)
    if match:
        return float(match.group(1))
    return None


def _discount_pct(original: float, sale: float) -> int:
    """Vypočítá procento slevy."""
    if original <= 0:
        return 0
    return round((1 - sale / original) * 100)


def format_message(results: list[dict]) -> str:
    """Sestaví přehlednou Telegram zprávu s cenami a slevami."""
    today = date.today().strftime("%-d. %-m. %Y")

    on_sale = [r for r in results if r.get("sale_price")]
    regular = [r for r in results if not r.get("sale_price")]

    # Seřadit dle ceny
    on_sale.sort(key=lambda r: r["sale_price"])
    regular.sort(key=lambda r: r["price"])

    lines = [f"🧈 <b>Ceny másla</b> — {today}\n"]

    if on_sale:
        lines.append("🔴 <b>SLEVY DNES:</b>")
        for r in on_sale[:8]:
            pct = _discount_pct(r["price"], r["sale_price"])
            lines.append(
                f"• {r['name']} — <b>{r['sale_price']:.2f} Kč</b> "
                f"(-{pct}%) @ {r['store']}"
            )
        lines.append("")

    if regular:
        lines.append("💰 <b>NEJLEVNĚJŠÍ:</b>")
        for r in regular[:8]:
            lines.append(f"• {r['name']} — {r['price']:.2f} Kč @ {r['store']}")
        lines.append("")

    all_prices = [r["sale_price"] or r["price"] for r in results]
    if all_prices:
        avg = sum(all_prices) / len(all_prices)
        lines.append(f"📊 Průměrná cena: <b>{avg:.2f} Kč</b> ({len(results)} produktů)")

    if not results:
        lines.append("⚠️ Nepodařilo se načíst žádné ceny. Zkus to znovu nebo zkontroluj logy.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Odešle zprávu přes Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.", file=sys.stderr)
        sys.exit(1)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = httpx.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"Telegram API chyba: {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)
    print("Zpráva odeslána na Telegram.")


if __name__ == "__main__":
    print("Stahuji ceny...")
    results: list[dict] = []
    results += search_rohlik()
    results += search_kosik()
    results += search_albert()
    results += search_billa()

    print(f"Nalezeno {len(results)} produktů celkem.")
    message = format_message(results)
    print("--- Náhled zprávy ---")
    print(message)
    print("---------------------")
    send_telegram(message)
