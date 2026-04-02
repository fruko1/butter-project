"""
Denní scraper cen másla z českých supermarketů přes Heureka.cz.
Posílá přehled cen a slev na Telegram každý den ráno.
"""

import os
import re
import sys
import json
import httpx
from datetime import date
from bs4 import BeautifulSoup

# Odfiltrovat VŠECHNY non-printable znaky z GitHub Secrets (nejen whitespace)
def _clean_secret(value: str) -> str:
    return re.sub(r"[^\x21-\x7E]", "", value).strip()

TELEGRAM_TOKEN = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}


def search_heureka() -> list[dict]:
    """
    Heureka.cz — agregátor cen z českých obchodů.
    Scrape kategorie 'máslo' a vrátí produkty s nejlepší cenou od každého obchodu.
    """
    results = []
    try:
        url = "https://potraviny.heureka.cz/maslo/"
        r = httpx.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        print(f"[Heureka] status={r.status_code}, url={r.url}")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Heureka používá strukturovaná data (JSON-LD) nebo produktové karty
        # Zkusit JSON-LD nejdříve
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                # ItemList nebo Product
                items = []
                if isinstance(ld, list):
                    items = ld
                elif ld.get("@type") == "ItemList":
                    items = [e.get("item", e) for e in ld.get("itemListElement", [])]
                elif ld.get("@type") == "Product":
                    items = [ld]

                for product in items:
                    name = product.get("name", "")
                    offers = product.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if name and price:
                        results.append({
                            "name": name[:60],
                            "price": float(price),
                            "sale_price": None,
                            "store": "Heureka (nejlepší cena)",
                        })
            except Exception:
                pass

        # Fallback: HTML produktové karty
        if not results:
            print("[Heureka] JSON-LD nenalezen, zkouším HTML selektory...")
            # Heureka produktová karta
            cards = soup.select(".c-product__body, [class*='ProductItem'], [data-product-id]")
            print(f"[Heureka] nalezeno {len(cards)} HTML karet")
            for card in cards[:25]:
                name_el = card.select_one(
                    "h2, h3, [class*='title'], [class*='name'], .c-product__title"
                )
                price_el = card.select_one(
                    "[class*='price'], [data-price], .c-product__price"
                )
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price_num = _parse_price(price_el.get_text(strip=True))
                if not price_num or price_num < 10:  # filtr nesmyslných hodnot
                    continue
                results.append({
                    "name": name[:60],
                    "price": price_num,
                    "sale_price": None,
                    "store": "Heureka",
                })

        print(f"[Heureka] celkem {len(results)} produktů")
    except Exception as e:
        print(f"[Heureka] Chyba: {e}", file=sys.stderr)
    return results


def search_rohlik() -> list[dict]:
    """Rohlik.cz — scraping HTML výsledků vyhledávání."""
    results = []
    try:
        url = "https://www.rohlik.cz/hledat"
        params = {"q": "maslo"}
        r = httpx.get(url, params=params, headers=HEADERS, timeout=25, follow_redirects=True)
        print(f"[Rohlik] status={r.status_code}")
        # Rohlik je SPA (React), plain request nevrátí produkty — zkusíme JSON-LD
        soup = BeautifulSoup(r.text, "lxml")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    name = item.get("name", "")
                    offers = item.get("offers", {})
                    price = offers.get("price") if isinstance(offers, dict) else None
                    if name and price and float(price) > 5:
                        results.append({
                            "name": name[:60],
                            "price": float(price),
                            "sale_price": None,
                            "store": "Rohlik.cz",
                        })
            except Exception:
                pass
        print(f"[Rohlik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
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
        lines.append(f"📊 Průměr: <b>{avg:.2f} Kč</b> | Nejlevnější: <b>{min(all_prices):.2f} Kč</b>")

    if not results:
        lines.append("⚠️ Dnes se nepodařilo načíst žádné ceny. Zkontroluj GitHub Actions logy.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.", file=sys.stderr)
        sys.exit(1)

    print(f"[Telegram] token délka={len(TELEGRAM_TOKEN)}, chat_id='{TELEGRAM_CHAT_ID}'")

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
    results += search_heureka()
    results += search_rohlik()

    print(f"\nCelkem nalezeno: {len(results)} produktů")
    message = format_message(results)
    print("\n--- Náhled zprávy ---")
    print(message)
    print("---------------------\n")
    send_telegram(message)
