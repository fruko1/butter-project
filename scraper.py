"""
Denní scraper cen másla z českých supermarketů.
Používá Playwright (headless browser) pro obejití bot-detekce.
"""

import os
import re
import sys
import json
from datetime import date
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

def _clean_secret(value: str) -> str:
    """Odstraní VŠECHNY non-printable znaky z GitHub Secrets."""
    return re.sub(r"[^\x21-\x7E]", "", value).strip()

TELEGRAM_TOKEN = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))


def _parse_price(text: str) -> float | None:
    """Extrahuje číslo z textu jako '39,90 Kč' nebo '39.90'."""
    match = re.search(r"(\d+)[.,](\d{1,2})", text)
    if match:
        return float(f"{match.group(1)}.{match.group(2)}")
    match = re.search(r"(\d+)", text)
    if match:
        val = float(match.group(1))
        return val if val > 5 else None
    return None


def scrape_rohlik(page) -> list[dict]:
    """Rohlik.cz — headless browser scraping."""
    results = []
    try:
        page.goto("https://www.rohlik.cz/hledat?q=maslo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # Počkat na načtení produktů
        try:
            page.wait_for_selector("[data-test='product-name'], h3, .ProductCard", timeout=10000)
        except Exception:
            pass

        # Zkusit zachytit XHR odpovědi přes page.evaluate
        products_json = page.evaluate("""
            () => {
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                const results = [];
                scripts.forEach(s => {
                    try {
                        const d = JSON.parse(s.textContent);
                        if (d['@type'] === 'Product' || Array.isArray(d)) {
                            results.push(d);
                        }
                    } catch(e) {}
                });
                return results;
            }
        """)

        for item in products_json:
            items = item if isinstance(item, list) else [item]
            for product in items:
                name = product.get("name", "")
                offers = product.get("offers", {})
                price = offers.get("price") if isinstance(offers, dict) else None
                if name and price:
                    results.append({
                        "name": name[:60],
                        "price": float(price),
                        "sale_price": None,
                        "store": "Rohlik.cz",
                    })

        # HTML fallback
        if not results:
            cards = page.query_selector_all("article, [data-test='product-name']")
            for card in cards[:20]:
                try:
                    name_el = card.query_selector("h2, h3, [class*='name']")
                    price_el = card.query_selector("[class*='price']")
                    if not name_el or not price_el:
                        continue
                    name = name_el.inner_text().strip()
                    price = _parse_price(price_el.inner_text())
                    if name and price:
                        results.append({
                            "name": name[:60],
                            "price": price,
                            "sale_price": None,
                            "store": "Rohlik.cz",
                        })
                except Exception:
                    pass

        print(f"[Rohlik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
    return results


def scrape_kosik(page) -> list[dict]:
    """Kosik.cz — headless browser scraping."""
    results = []
    try:
        page.goto("https://www.kosik.cz/vyhledavani?q=maslo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        try:
            page.wait_for_selector("[class*='product'], [class*='Product']", timeout=10000)
        except Exception:
            pass

        cards = page.query_selector_all("[class*='ProductCard'], [class*='product-card'], article")
        for card in cards[:20]:
            try:
                name_el = card.query_selector("h2, h3, [class*='name'], [class*='title']")
                price_el = card.query_selector("[class*='price'], [class*='Price']")
                if not name_el or not price_el:
                    continue
                name = name_el.inner_text().strip()
                price = _parse_price(price_el.inner_text())
                if name and price:
                    # Zkontrolovat původní cenu (sleva)
                    old_price_el = card.query_selector("[class*='original'], [class*='old'], [class*='crossed']")
                    old_price = _parse_price(old_price_el.inner_text()) if old_price_el else None
                    results.append({
                        "name": name[:60],
                        "price": old_price if old_price and old_price > price else price,
                        "sale_price": price if old_price and old_price > price else None,
                        "store": "Kosik.cz",
                    })
            except Exception:
                pass

        print(f"[Kosik] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Kosik] Chyba: {e}", file=sys.stderr)
    return results


def scrape_albert(page) -> list[dict]:
    """Albert.cz — headless browser scraping."""
    results = []
    try:
        page.goto("https://www.albert.cz/search?q=maslo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        try:
            page.wait_for_selector("[class*='product'], [class*='Product']", timeout=10000)
        except Exception:
            pass

        cards = page.query_selector_all("[class*='product-tile'], [class*='ProductTile'], [class*='product-card']")
        for card in cards[:20]:
            try:
                name_el = card.query_selector("h2, h3, [class*='name'], [class*='title']")
                price_el = card.query_selector("[class*='price'], [class*='Price']")
                if not name_el or not price_el:
                    continue
                name = name_el.inner_text().strip()
                price = _parse_price(price_el.inner_text())
                if name and price:
                    old_price_el = card.query_selector("[class*='original'], [class*='old'], [class*='strike']")
                    old_price = _parse_price(old_price_el.inner_text()) if old_price_el else None
                    results.append({
                        "name": name[:60],
                        "price": old_price if old_price and old_price > price else price,
                        "sale_price": price if old_price and old_price > price else None,
                        "store": "Albert.cz",
                    })
            except Exception:
                pass

        print(f"[Albert] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Albert] Chyba: {e}", file=sys.stderr)
    return results


def scrape_billa(page) -> list[dict]:
    """Billa.cz — headless browser scraping."""
    results = []
    try:
        page.goto("https://www.billa.cz/produkty?q=maslo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        try:
            page.wait_for_selector("[class*='product'], [class*='Product']", timeout=10000)
        except Exception:
            pass

        cards = page.query_selector_all("[class*='product-card'], [class*='ProductCard'], [class*='tile']")
        for card in cards[:20]:
            try:
                name_el = card.query_selector("h2, h3, [class*='name'], [class*='title']")
                price_el = card.query_selector("[class*='price'], [class*='Price']")
                if not name_el or not price_el:
                    continue
                name = name_el.inner_text().strip()
                price = _parse_price(price_el.inner_text())
                if name and price:
                    results.append({
                        "name": name[:60],
                        "price": price,
                        "sale_price": None,
                        "store": "Billa.cz",
                    })
            except Exception:
                pass

        print(f"[Billa] nalezeno {len(results)} produktů")
    except Exception as e:
        print(f"[Billa] Chyba: {e}", file=sys.stderr)
    return results


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

    if not results:
        lines.append("⚠️ Dnes se nepodařilo načíst žádné ceny. Zkontroluj GitHub Actions logy.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    import httpx
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
    print("Stahuji ceny másla pomocí headless browseru...")

    results: list[dict] = []

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
        stealth_sync(page)  # skryje znaky headless browseru

        results += scrape_rohlik(page)
        results += scrape_kosik(page)
        results += scrape_albert(page)
        results += scrape_billa(page)

        browser.close()

    print(f"\nCelkem nalezeno: {len(results)} produktů")
    message = format_message(results)
    print("\n--- Náhled zprávy ---")
    print(message)
    print("---------------------\n")
    send_telegram(message)
