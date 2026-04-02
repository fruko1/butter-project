"""
Denní scraper cen másla z českých supermarketů.
Zachytává síťové API požadavky které stránky dělají — spolehlivější než DOM scraping.
"""

import os
import re
import sys
import json
from datetime import date
from playwright.sync_api import sync_playwright, Response


def _clean_secret(value: str) -> str:
    return re.sub(r"[^\x21-\x7E]", "", value).strip()


TELEGRAM_TOKEN = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))


def _parse_price(text: str) -> float | None:
    match = re.search(r"(\d+)[.,](\d{1,2})", str(text))
    if match:
        val = float(f"{match.group(1)}.{match.group(2)}")
        return val if 10 < val < 500 else None
    return None


def _find_in_json(obj, results: list, store: str, depth: int = 0):
    """Rekurzivně prohledá JSON a hledá produkty s názvem a cenou."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("productName") or obj.get("title") or ""
        # Cena může být v různých klíčích
        price_raw = (
            obj.get("price") or obj.get("currentPrice") or obj.get("salesPrice")
            or obj.get("regularPrice") or obj.get("amount")
        )
        if isinstance(price_raw, dict):
            price_raw = (
                price_raw.get("amount") or price_raw.get("value")
                or price_raw.get("regular") or price_raw.get("full")
            )
        price = _parse_price(price_raw) if price_raw else None

        if name and price and len(name) > 3:
            results.append({
                "name": str(name)[:70],
                "price": price,
                "sale_price": None,
                "store": store,
            })
        else:
            for v in obj.values():
                _find_in_json(v, results, store, depth + 1)

    elif isinstance(obj, list):
        for item in obj[:50]:
            _find_in_json(item, results, store, depth + 1)


def scrape_with_intercept(page, url: str, store: str) -> list[dict]:
    """
    Načte stránku a zachytí všechny JSON API odpovědi.
    Hledá produkty s názvem a cenou v zachycených datech.
    """
    captured: list[dict] = []

    def on_response(response: Response):
        # Zachytit jen JSON odpovědi z API endpointů
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return
        if response.status != 200:
            return
        # Přeskočit drobné utility requesty
        skip = ["analytics", "tracking", "gtm", "facebook", "google", "sentry", "beacon"]
        if any(s in response.url for s in skip):
            return
        try:
            data = response.json()
            _find_in_json(data, captured, store)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(url, timeout=45000)
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[{store}] Načítání: {e}", file=sys.stderr)

    page.remove_listener("response", on_response)

    # Fallback: zkusit __NEXT_DATA__ (Next.js)
    if not captured:
        try:
            next_data = page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? JSON.parse(el.textContent) : null;
                }
            """)
            if next_data:
                _find_in_json(next_data, captured, store)
        except Exception:
            pass

    # Fallback: zkusit window.__INITIAL_STATE__ nebo podobné
    if not captured:
        for var in ["__INITIAL_STATE__", "__PRELOADED_STATE__", "__APP_STATE__", "initialData"]:
            try:
                data = page.evaluate(f"() => window.{var} || null")
                if data:
                    _find_in_json(data, captured, store)
                    break
            except Exception:
                pass

    # Debug: co stránka vrátila
    print(f"[{store}] zachyceno {len(captured)} kandidátů z API")

    # Deduplikace dle jména
    seen = set()
    unique = []
    for r in captured:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique.append(r)

    return unique


def scrape_rohlik(page) -> list[dict]:
    results = scrape_with_intercept(
        page,
        "https://www.rohlik.cz/hledat?q=m%C3%A1slo",
        "Rohlik.cz"
    )
    print(f"[Rohlik] výsledek: {len(results)} produktů")
    return results


def scrape_kosik(page) -> list[dict]:
    results = scrape_with_intercept(
        page,
        "https://www.kosik.cz/vyhledavani?q=m%C3%A1slo",
        "Kosik.cz"
    )
    print(f"[Kosik] výsledek: {len(results)} produktů")
    return results


def scrape_albert(page) -> list[dict]:
    results = scrape_with_intercept(
        page,
        "https://www.albert.cz/vyhledavani?q=m%C3%A1slo",
        "Albert.cz"
    )
    print(f"[Albert] výsledek: {len(results)} produktů")
    return results


def scrape_billa(page) -> list[dict]:
    results = scrape_with_intercept(
        page,
        "https://www.billa.cz/search?q=m%C3%A1slo",
        "Billa.cz"
    )
    print(f"[Billa] výsledek: {len(results)} produktů")
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
        lines.append("⚠️ Dnes se nepodařilo načíst žádné ceny másla.")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    import httpx
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("CHYBA: TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.", file=sys.stderr)
        sys.exit(1)

    api_url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    r = httpx.post(api_url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
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

    print(f"\nCelkem: {len(results)} produktů")

    # Ukázka prvních 5 výsledků pro debug
    for r in results[:5]:
        print(f"  {r['store']}: {r['name']} — {r['price']} Kč")

    message = format_message(results)
    print("\n--- Zpráva ---")
    print(message)
    print("--------------\n")
    send_telegram(message)
