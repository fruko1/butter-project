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


def _clean_secret(value: str) -> str:
    """Odstraní VŠECHNY non-printable znaky z GitHub Secrets."""
    return re.sub(r"[^\x21-\x7E]", "", value).strip()


TELEGRAM_TOKEN = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))

# Produkty které URČITĚ nejsou máslo (výsledky z nesouvisejících sekcí stránky)
NOT_BUTTER = ["šunka", "klobás", "meloun", "vanilka", "sýr", "jogurt", "mléko", "káva", "čaj", "pivo", "víno"]


def _is_butter(name: str) -> bool:
    """
    Vrátí True pokud produkt MŮŽE být máslo.
    Jsme už na stránce vyhledávání másla — filtrujeme jen zjevně nesouvisející produkty.
    """
    name_lower = name.lower()
    # Vyloučit zjevně nesouvisející produkty
    if any(kw in name_lower for kw in NOT_BUTTER):
        return False
    return True


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


def _extract_cards(page, store: str) -> list[dict]:
    """Obecná extrakce produktových karet z aktuální stránky."""
    results = []

    # Zkusit JSON-LD strukturovaná data
    try:
        items_json = page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                    try {
                        const d = JSON.parse(s.textContent);
                        const items = Array.isArray(d) ? d : (d.itemListElement ? d.itemListElement.map(e => e.item || e) : [d]);
                        items.forEach(p => { if (p.name && p.offers) out.push(p); });
                    } catch(e) {}
                });
                return out;
            }
        """)
        for product in items_json:
            name = product.get("name", "")
            if not _is_butter(name):
                continue
            offers = product.get("offers", {})
            price = offers.get("price") or offers.get("lowPrice") if isinstance(offers, dict) else None
            if price:
                results.append({
                    "name": name[:70],
                    "price": float(price),
                    "sale_price": None,
                    "store": store,
                })
    except Exception:
        pass

    # HTML fallback — obecné selektory pro produktové karty
    if not results:
        selectors = [
            "article",
            "[class*='ProductCard']",
            "[class*='product-card']",
            "[class*='product-tile']",
            "[class*='ProductTile']",
            "[class*='product-item']",
        ]
        cards = []
        for sel in selectors:
            found = page.query_selector_all(sel)
            if len(found) > 2:
                cards = found
                print(f"[{store}] HTML selektor '{sel}' → {len(found)} karet")
                break

        for card in cards[:30]:
            try:
                name_el = card.query_selector(
                    "h2, h3, [class*='name'], [class*='title'], [class*='Name'], [class*='Title']"
                )
                price_el = card.query_selector(
                    "[class*='price'], [class*='Price'], [data-price]"
                )
                if not name_el or not price_el:
                    continue
                name = name_el.inner_text().strip()
                if not _is_butter(name):
                    continue
                price = _parse_price(price_el.inner_text())
                if not price:
                    continue
                old_price_el = card.query_selector(
                    "[class*='original'], [class*='old'], [class*='strike'], [class*='crossed'], [class*='before']"
                )
                old_price = _parse_price(old_price_el.inner_text()) if old_price_el else None
                is_sale = old_price and old_price > price
                results.append({
                    "name": name[:70],
                    "price": old_price if is_sale else price,
                    "sale_price": price if is_sale else None,
                    "store": store,
                })
            except Exception:
                pass

    return results


def scrape_rohlik(page) -> list[dict]:
    """Rohlik.cz — vyhledávání máslo."""
    try:
        page.goto("https://www.rohlik.cz/hledat?q=m%C3%A1slo", timeout=45000)
        # Rohlik je React SPA — čekáme na konkrétní element, ne na networkidle
        try:
            page.wait_for_selector("article, [class*='ProductCard'], [class*='product']", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        results = _extract_cards(page, "Rohlik.cz")
        print(f"[Rohlik] nalezeno {len(results)} produktů másla")
        return results
    except Exception as e:
        print(f"[Rohlik] Chyba: {e}", file=sys.stderr)
        return []


def scrape_kosik(page) -> list[dict]:
    """Kosik.cz — vyhledávání máslo."""
    try:
        page.goto("https://www.kosik.cz/vyhledavani?q=m%C3%A1slo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        results = _extract_cards(page, "Kosik.cz")
        print(f"[Kosik] nalezeno {len(results)} produktů másla")
        return results
    except Exception as e:
        print(f"[Kosik] Chyba: {e}", file=sys.stderr)
        return []


def scrape_albert(page) -> list[dict]:
    """Albert.cz — vyhledávání máslo."""
    try:
        page.goto("https://www.albert.cz/vyhledavani?q=m%C3%A1slo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        results = _extract_cards(page, "Albert.cz")
        print(f"[Albert] nalezeno {len(results)} produktů másla")
        return results
    except Exception as e:
        print(f"[Albert] Chyba: {e}", file=sys.stderr)
        return []


def scrape_billa(page) -> list[dict]:
    """Billa.cz — vyhledávání máslo."""
    try:
        # Správná URL pro vyhledávání na Billa.cz
        page.goto("https://www.billa.cz/search?q=m%C3%A1slo", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        results = _extract_cards(page, "Billa.cz")
        print(f"[Billa] nalezeno {len(results)} produktů másla")
        return results
    except Exception as e:
        print(f"[Billa] Chyba: {e}", file=sys.stderr)
        return []


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
        lines.append("⚠️ Dnes se nepodařilo načíst žádné ceny másla. Zkontroluj GitHub Actions logy.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    import httpx
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.", file=sys.stderr)
        sys.exit(1)

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

        results += scrape_rohlik(page)
        results += scrape_kosik(page)
        results += scrape_albert(page)
        results += scrape_billa(page)

        browser.close()

    print(f"\nCelkem nalezeno: {len(results)} produktů másla")
    message = format_message(results)
    print("\n--- Náhled zprávy ---")
    print(message)
    print("---------------------\n")
    send_telegram(message)
