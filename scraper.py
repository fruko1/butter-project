"""
Denní scraper cen másla z českých supermarketů.
Posílá přehled cen a slev na Telegram každý den ráno.
"""

import os
import re
import sys
import httpx
from datetime import date
from bs4 import BeautifulSoup

# .strip() odstraní neviditelné znaky (newline, mezery) z GitHub Secrets
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}


def search_rohlik() -> list[dict]:
    """Rohlik.cz — interní search API."""
    results = []
    try:
        url = "https://www.rohlik.cz/api/v1/search/results"
        params = {"query": "maslo", "page": 1, "pageSize": 20}
        headers = {**HEADERS, "Accept": "application/json"}
        r = httpx.get(url, params=params, headers=headers, timeout=20, follow_redirects=True)
        print(f"[Rohlik] status={r.status_code}")
        r.raise_for_status()
        data = r.json()
        # Rohlik vrací produkty v různých klíčích dle verze API
        items = (
            data.get("data", {}).get("productList", [])
            or data.get("data", [])
            or data.get("results", [])
            or data.get("products", [])
        )
        for item in items:
            name = item.get("name", "")
            price_data = item.get("price", {})
            price_full = (
                price_data.get("full")
                or price_data.get("amount")
                or price_data.get("value")
                or item.get("price")
            )
            price_sale = (
                price_data.get("sale")
                or price_data.get("discountedAmount")
                or price_data.get("saleValue")
            )
            if not price_full or not name:
                continue
            results.append({
                "name": name,
                "price": float(price_full),
                "sale_price": float(price_sale) if price_sale else None,
                "store": "Rohlik.cz",
            })
        print(f"[Rohlik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
    return results


def search_kosik() -> list[dict]:
    """Kosik.cz — interní API (www subdoména)."""
    results = []
    try:
        # Kosik přesměrovává api.kosik.cz → www.kosik.cz
        url = "https://www.kosik.cz/api/v1/products/"
        params = {"search": "maslo", "limit": 20}
        headers = {**HEADERS, "Accept": "application/json"}
        r = httpx.get(url, params=params, headers=headers, timeout=20, follow_redirects=True)
        print(f"[Kosik] status={r.status_code}, url={r.url}")
        r.raise_for_status()
        data = r.json()
        items = (
            data.get("data", {}).get("products", [])
            or data.get("products", [])
            or data.get("results", [])
            or (data if isinstance(data, list) else [])
        )
        for item in items:
            name = item.get("name", "")
            price = item.get("price") or item.get("currentPrice")
            original_price = item.get("originalPrice") or item.get("regularPrice")
            if not price or not name:
                continue
            price = float(price)
            is_sale = original_price and float(original_price) > price
            results.append({
                "name": name,
                "price": float(original_price) if is_sale else price,
                "sale_price": price if is_sale else None,
                "store": "Kosik.cz",
            })
        print(f"[Kosik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Kosik] Chyba: {e}", file=sys.stderr)
    return results


def search_albert() -> list[dict]:
    """Albert.cz — scraping HTML výsledků vyhledávání."""
    results = []
    try:
        # Albert používá Salesforce Commerce Cloud
        url = "https://www.albert.cz/search"
        params = {"q": "maslo", "lang": "cs_CZ"}
        headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml"}
        r = httpx.get(url, params=params, headers=headers, timeout=20, follow_redirects=True)
        print(f"[Albert] status={r.status_code}, url={r.url}")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Zkusit JSON-LD strukturovaná data (nejspolehlivější)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else ld.get("itemListElement", [])
                for item in items:
                    product = item.get("item", item)
                    name = product.get("name", "")
                    offer = product.get("offers", {})
                    price = offer.get("price") or offer.get("lowPrice")
                    if name and price and "máslo" in name.lower():
                        results.append({
                            "name": name,
                            "price": float(price),
                            "sale_price": None,
                            "store": "Albert.cz",
                        })
            except Exception:
                pass

        # Fallback: HTML scraping
        if not results:
            selectors = [
                ".product-tile",
                "[data-component='ProductTile']",
                "article[class*='product']",
                ".b-product-tile",
            ]
            for selector in selectors:
                cards = soup.select(selector)
                if cards:
                    print(f"[Albert] HTML selector '{selector}' nalezl {len(cards)} karet")
                    break
            for card in cards[:20]:
                name_el = card.select_one("h2, h3, .product-name, [class*='name']")
                price_el = card.select_one(".price, [class*='price'], [data-price]")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_num = _parse_price(price_el.get_text(strip=True))
                if not price_num:
                    continue
                results.append({
                    "name": name,
                    "price": price_num,
                    "sale_price": None,
                    "store": "Albert.cz",
                })

        print(f"[Albert] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Albert] Chyba: {e}", file=sys.stderr)
    return results


def search_billa() -> list[dict]:
    """Billa.cz — scraping HTML (API endpoint se změnil)."""
    results = []
    try:
        url = "https://www.billa.cz/recepty-a-produkty/vyhledavani"
        params = {"q": "maslo"}
        headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml"}
        r = httpx.get(url, params=params, headers=headers, timeout=20, follow_redirects=True)
        print(f"[Billa] status={r.status_code}, url={r.url}")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Zkusit JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else ld.get("itemListElement", [])
                for item in items:
                    product = item.get("item", item)
                    name = product.get("name", "")
                    offer = product.get("offers", {})
                    price = offer.get("price") or offer.get("lowPrice")
                    if name and price:
                        results.append({
                            "name": name,
                            "price": float(price),
                            "sale_price": None,
                            "store": "Billa.cz",
                        })
            except Exception:
                pass

        # Fallback: HTML scraping
        if not results:
            for card in soup.select(".product-tile, [class*='ProductCard'], [class*='product-card']")[:20]:
                name_el = card.select_one("h2, h3, [class*='name'], [class*='title']")
                price_el = card.select_one("[class*='price'], [data-price]")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_num = _parse_price(price_el.get_text(strip=True))
                if not price_num:
                    continue
                results.append({
                    "name": name,
                    "price": price_num,
                    "sale_price": None,
                    "store": "Billa.cz",
                })

        print(f"[Billa] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Billa] Chyba: {e}", file=sys.stderr)
    return results


def _parse_price(text: str) -> float | None:
    """Extrahuje číslo z textu jako '39,90 Kč' nebo '39.90'."""
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
        lines.append("💰 <b>NEJLEVNĚJŠÍ:</b>")
        for r in regular[:8]:
            lines.append(f"• {r['name']} — {r['price']:.2f} Kč @ {r['store']}")
        lines.append("")

    all_prices = [r.get("sale_price") or r["price"] for r in results]
    if all_prices:
        avg = sum(all_prices) / len(all_prices)
        lines.append(f"📊 Průměrná cena: <b>{avg:.2f} Kč</b> ({len(results)} produktů)")

    if not results:
        lines.append("⚠️ Nepodařilo se načíst žádné ceny. Zkontroluj GitHub Actions logy.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Odešle zprávu přes Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.", file=sys.stderr)
        sys.exit(1)

    # Sestavit URL bezpečně — token je již ořezaný přes .strip()
    api_url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = httpx.post(api_url, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"Telegram API chyba: {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)
    print("Zpráva úspěšně odeslána na Telegram.")


if __name__ == "__main__":
    print("Stahuji ceny másla...")
    results: list[dict] = []
    results += search_rohlik()
    results += search_kosik()
    results += search_albert()
    results += search_billa()

    print(f"\nCelkem nalezeno: {len(results)} produktů")
    message = format_message(results)
    print("\n--- Náhled zprávy ---")
    print(message)
    print("---------------------\n")
    send_telegram(message)
