"""
Human-like login-aware scraper using headful Chromium + Playwright.
Mimics human behaviour: random delays, mouse jitter, smooth scrolling.

Two modes:

1. Trustpilot preset (unchanged from before):
     python trustpilot_scraper.py --company google.com --pages 5 --out reviews.csv

2. Generic mode — works on any site, driven by CSS selectors you supply:
     python trustpilot_scraper.py \
         --url "https://example.com/reviews" \
         --item-selector ".review-card" \
         --field title=h3 --field body=".review-text" --field rating=".stars@data-rating" \
         --next-selector "a.next-page" \
         --pages 20 --out reviews.csv

Logging in:
     python trustpilot_scraper.py --login https://example.com
   Opens a real browser, you log in by hand, press ENTER, and the session
   (cookies/local storage) is saved per-domain under ./sessions/.

Session resets mid-scrape:
   Some sites re-show a login wall after a handful of pages even with a
   saved session. When that's detected, the (already-visible) browser
   window pauses — log back in by hand, press ENTER here, and scraping
   resumes on the same page automatically, using whatever was already
   collected (progress is saved to --out after every page, so nothing
   already scraped is lost while you wait).
"""

import argparse
import asyncio
import csv
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSIONS_DIR = Path(__file__).parent / "sessions"
OLD_TRUSTPILOT_SESSION_FILE = Path(__file__).parent / "trustpilot_session.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Session handling (one saved session per site domain)
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    return urlparse(url).netloc or url


def resolve_session_file(url: str) -> Path:
    """Path to the saved-session file for this URL's domain, migrating the
    old single-file Trustpilot session the first time it's needed."""
    domain = domain_of(url).replace(":", "_")
    path = SESSIONS_DIR / f"{domain}.json"
    if not path.exists() and "trustpilot.com" in domain and OLD_TRUSTPILOT_SESSION_FILE.exists():
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(OLD_TRUSTPILOT_SESSION_FILE.read_bytes())
        print(f"[info] migrated legacy session file to {path}")
    return path


# ---------------------------------------------------------------------------
# Data model (Trustpilot preset)
# ---------------------------------------------------------------------------

@dataclass
class Review:
    rating: int
    title: str
    body: str


# ---------------------------------------------------------------------------
# Human-like helpers
# ---------------------------------------------------------------------------

async def human_delay(min_ms: float = 500, max_ms: float = 2000) -> None:
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def move_mouse_randomly(page: Page) -> None:
    """Move mouse to a random position on the visible viewport."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    x = random.randint(100, vp["width"] - 100)
    y = random.randint(100, vp["height"] - 100)
    await page.mouse.move(x, y, steps=random.randint(5, 15))


async def scroll_full_page(page: Page) -> None:
    """Scroll from top to bottom in human-like increments so every
    lazy-loaded card renders its content."""
    height = await page.evaluate("document.body.scrollHeight")
    position = 0
    while position < height:
        delta = random.randint(300, 600)
        await page.mouse.wheel(0, delta)
        position += delta
        await asyncio.sleep(random.uniform(0.15, 0.4))
        if random.random() < 0.2:  # occasional human pause
            await human_delay(400, 900)
        height = await page.evaluate("document.body.scrollHeight")
    # Scroll back up a touch — humans overshoot then settle
    await page.mouse.wheel(0, -random.randint(200, 500))
    await human_delay(400, 900)


async def dismiss_cookie_banner(page: Page) -> None:
    """Best-effort click on whatever cookie-consent button is present."""
    selectors = [
        "button#onetrust-accept-btn-handler",  # OneTrust (Trustpilot and many others)
        "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",  # Cookiebot
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('I agree')",
        "button[aria-label*='Accept' i]",
    ]
    for sel in selectors:
        try:
            btn = await page.wait_for_selector(sel, timeout=2000)
        except Exception:
            continue
        if btn:
            try:
                await btn.click()
                await human_delay(500, 1000)
            except Exception:
                pass
            return


# ---------------------------------------------------------------------------
# Trustpilot preset — extraction, login-wall check, pagination
# ---------------------------------------------------------------------------

REVIEW_CARD_SELECTOR = "article[data-service-review-card-paper='true']"


async def parse_reviews_on_page(page: Page) -> list[Review]:
    await page.wait_for_selector(REVIEW_CARD_SELECTOR, timeout=15_000)
    cards = await page.query_selector_all(REVIEW_CARD_SELECTOR)

    reviews: list[Review] = []
    for card in cards:
        rating_attr = await card.get_attribute("data-service-review-rating")
        rating = int(rating_attr) if rating_attr and rating_attr.isdigit() else 0

        if rating == 0:
            stars = await card.query_selector_all("img[alt*='Rated']")
            if stars:
                alt = await stars[0].get_attribute("alt") or ""
                parts = alt.split()
                rating = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        title_el = await card.query_selector("h2[data-service-review-title-typography]")
        if title_el is None:
            title_el = await card.query_selector("h2")
        title = (await title_el.inner_text()).strip() if title_el else ""

        body_el = await card.query_selector("p[data-service-review-text-typography]")
        if body_el is None:
            body_el = await card.query_selector("section[data-service-review-text-typography]")
        body = (await body_el.inner_text()).strip() if body_el else ""

        reviews.append(Review(rating=rating, title=title, body=body))

    return reviews


async def _hit_trustpilot_login_wall(page: Page) -> bool:
    has_cards = await page.query_selector(REVIEW_CARD_SELECTOR)
    if has_cards:
        return False
    login_cta = await page.query_selector(
        "a[href*='/users/login'], a[href*='/signup'], "
        "[data-login-wall], text=/log in to see/i"
    )
    return login_cta is not None


async def go_to_next_page_trustpilot(page: Page) -> bool:
    next_btn = await page.query_selector("a[data-page-number][aria-label*='Next']")
    if next_btn is None:
        next_btn = await page.query_selector("a[name='pagination-button-next']")
    if next_btn is None:
        return False

    await move_mouse_randomly(page)
    await human_delay(300, 800)
    await next_btn.scroll_into_view_if_needed()
    await human_delay(200, 500)
    await next_btn.click()
    await human_delay(1500, 3500)
    return True


# ---------------------------------------------------------------------------
# Generic mode — extraction, login-wall check, pagination
# ---------------------------------------------------------------------------

FieldSpec = tuple[str, str, str | None]  # (name, css_selector, attribute-or-None)


def parse_field_arg(spec: str) -> FieldSpec:
    if "=" not in spec:
        raise ValueError(f"Invalid --field '{spec}', expected NAME=SELECTOR[@ATTR]")
    name, rest = spec.split("=", 1)
    if "@" in rest:
        selector, attr = rest.rsplit("@", 1)
    else:
        selector, attr = rest, None
    return name.strip(), selector.strip(), (attr.strip() if attr else None)


async def extract_generic_items(
    page: Page, item_selector: str, field_specs: list[FieldSpec]
) -> list[dict]:
    try:
        await page.wait_for_selector(item_selector, timeout=15_000)
    except Exception:
        return []

    cards = await page.query_selector_all(item_selector)
    rows: list[dict] = []
    for card in cards:
        if field_specs:
            row = {}
            for name, selector, attr in field_specs:
                el = card if not selector else await card.query_selector(selector)
                if el is None:
                    row[name] = ""
                    continue
                if attr:
                    row[name] = (await el.get_attribute(attr)) or ""
                else:
                    row[name] = (await el.inner_text()).strip()
            rows.append(row)
        else:
            rows.append({"text": (await card.inner_text()).strip()})
    return rows


async def detect_login_wall_generic(
    page: Page,
    item_selector: str,
    wall_selector: str | None,
    wall_text: str | None,
    had_items_before: bool,
) -> bool:
    if wall_selector and await page.query_selector(wall_selector) is not None:
        return True

    if wall_text:
        try:
            if await page.get_by_text(wall_text, exact=False).count() > 0:
                return True
        except Exception:
            pass

    current_path = urlparse(page.url).path.lower()
    if any(seg in current_path for seg in ("/login", "/signin", "/sign-in", "/account/login")):
        return True

    # Heuristic fallback: items were rendering fine, then suddenly vanished —
    # classic sign of a session reset mid-pagination.
    if had_items_before:
        count = len(await page.query_selector_all(item_selector))
        if count == 0:
            return True

    return False


async def go_to_next_page_generic(page: Page, next_selector: str) -> bool:
    btn = await page.query_selector(next_selector)
    if btn is None:
        return False
    await move_mouse_randomly(page)
    await human_delay(300, 800)
    await btn.scroll_into_view_if_needed()
    await human_delay(200, 500)
    await btn.click()
    await human_delay(1500, 3500)
    return True


async def go_to_next_page_urlpattern(page: Page, url_pattern: str, next_page_num: int) -> bool:
    url = url_pattern.format(page=next_page_num)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        print(f"  [!] failed to load {url}: {exc}")
        return False
    await human_delay(500, 1200)
    return True


# ---------------------------------------------------------------------------
# Browser / session plumbing
# ---------------------------------------------------------------------------

async def _new_context(browser, session_file: Path | None) -> BrowserContext:
    kwargs = dict(
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
    )
    if session_file and session_file.exists():
        kwargs["storage_state"] = str(session_file)
        print(f"Loaded saved session from {session_file}")

    context = await browser.new_context(**kwargs)
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


async def login(url: str) -> None:
    """Open a browser, let the user log in by hand, then save the session
    for that site's domain.

    Run once per site:  python trustpilot_scraper.py --login https://example.com
    """
    session_file = resolve_session_file(url)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await _new_context(browser, session_file=None)
        page = await context.new_page()

        await page.goto(url, wait_until="domcontentloaded")
        print(
            "\n"
            "============================================================\n"
            f" A browser window is open at {domain_of(url)}. Please LOG IN now.\n"
            "\n"
            " When you are fully logged in, come back here and press ENTER\n"
            " to save the session.\n"
            "============================================================\n"
        )
        await asyncio.get_event_loop().run_in_executor(None, input)

        session_file.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(session_file))
        print(f"Session saved to {session_file.resolve()}")
        await browser.close()


async def handle_login_wall(
    context: BrowserContext, session_file: Path, site_label: str
) -> None:
    """Pause and wait for the human to log back in, then refresh the saved
    session. The browser window is already visible (we always run headful),
    so the user just switches to it."""
    print(
        "\n"
        "============================================================\n"
        f" [!] {site_label} is asking you to log in again (session reset).\n"
        " Switch to the open browser window and log in by hand.\n"
        "\n"
        " When you're back in, come here and press ENTER to resume —\n"
        " everything scraped so far has already been saved.\n"
        "============================================================\n"
    )
    await asyncio.get_event_loop().run_in_executor(None, input)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(session_file))
    print(f"[info] session refreshed and saved to {session_file}")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Main scrape loop (shared by both modes)
# ---------------------------------------------------------------------------

async def scrape(
    *,
    start_url: str,
    session_file: Path,
    max_pages: int,
    output_path: Path,
    mode: str,  # "trustpilot" or "generic"
    site_label: str,
    item_selector: str | None = None,
    field_specs: list[FieldSpec] | None = None,
    next_selector: str | None = None,
    url_pattern: str | None = None,
    wall_selector: str | None = None,
    wall_text: str | None = None,
) -> None:
    if not session_file.exists():
        print(
            f"[note] No saved session for {domain_of(start_url)} yet. If this site "
            "gates content behind a login, run `--login` first — or just let it "
            "pause you to log in when the wall shows up.\n"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await _new_context(browser, session_file)
        page = await context.new_page()

        print(f"Opening {start_url} ...")
        await page.goto(start_url, wait_until="domcontentloaded")
        await human_delay(2000, 4000)
        await dismiss_cookie_banner(page)

        all_rows: list[dict] = []
        had_items = False
        wall_retries_this_page = 0
        page_num = 1

        try:
            while page_num <= max_pages:
                print(f"  Scraping page {page_num}/{max_pages} ...")
                await scroll_full_page(page)
                await human_delay(500, 1200)

                if mode == "trustpilot":
                    wall = await _hit_trustpilot_login_wall(page)
                else:
                    wall = await detect_login_wall_generic(
                        page, item_selector, wall_selector, wall_text, had_items
                    )

                if wall:
                    wall_retries_this_page += 1
                    if wall_retries_this_page > 5:
                        print("  [stop] still hitting the login wall after several "
                              "re-login attempts — giving up on this page.")
                        break
                    await handle_login_wall(context, session_file, site_label)
                    await page.reload(wait_until="domcontentloaded")
                    await dismiss_cookie_banner(page)
                    await human_delay(1000, 2000)
                    continue  # retry the same page_num
                wall_retries_this_page = 0

                try:
                    if mode == "trustpilot":
                        page_reviews = await parse_reviews_on_page(page)
                        rows = [asdict(r) for r in page_reviews if r.title or r.body]
                    else:
                        rows = await extract_generic_items(page, item_selector, field_specs or [])
                except Exception as exc:
                    print(f"  [warn] Failed to parse page {page_num}: {exc}")
                    break

                if not rows:
                    print("  [stop] no items found on this page — reached the end "
                          "(or double-check --item-selector).")
                    break

                had_items = True
                all_rows.extend(rows)
                print(f"  → {len(rows)} items collected (total: {len(all_rows)})")

                # Save progress after every page so a crash, Ctrl+C, or a long
                # wait at a login wall never loses what's already scraped.
                write_csv(output_path, all_rows)

                if page_num >= max_pages:
                    break

                if mode == "trustpilot":
                    moved = await go_to_next_page_trustpilot(page)
                elif next_selector:
                    moved = await go_to_next_page_generic(page, next_selector)
                elif url_pattern:
                    moved = await go_to_next_page_urlpattern(page, url_pattern, page_num + 1)
                else:
                    moved = False  # single-page scrape, nothing to advance to

                if not moved:
                    print("  No next page found, stopping.")
                    break
                await human_delay(2000, 4500)
                page_num += 1
        finally:
            try:
                session_file.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_file))
            except Exception:
                pass
            await browser.close()

    print(f"\nDone. {len(all_rows)} items saved to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Login-aware Playwright scraper. Built-in preset for Trustpilot; "
            "generic mode (--url + --item-selector) works on any site. "
            "If a site resets your login mid-scrape, the browser pauses for "
            "you to log back in, then resumes automatically."
        )
    )
    parser.add_argument(
        "--login",
        nargs="?",
        const="https://www.trustpilot.com/",
        default=None,
        metavar="URL",
        help="Open a browser to log into URL manually and save the session for "
        "that site's domain, then exit. Bare --login logs into Trustpilot.",
    )

    # Trustpilot preset
    parser.add_argument("--company", help="[Trustpilot preset] company domain, e.g. 'google.com'")
    parser.add_argument("--region", default="www", help="[Trustpilot preset] regional subdomain (default: www)")

    # Generic mode
    parser.add_argument("--url", help="[Generic mode] first page URL to scrape")
    parser.add_argument("--item-selector", help="[Generic mode] CSS selector for each repeated item/card")
    parser.add_argument(
        "--field",
        action="append",
        dest="fields",
        metavar="NAME=SELECTOR[@ATTR]",
        help="[Generic mode] repeatable; extract one field per item, e.g. "
        "--field title=h2 --field rating='.stars@data-rating'. "
        "Omit entirely to just grab each item's full text as 'text'.",
    )
    parser.add_argument("--next-selector", help="[Generic mode] CSS selector for a 'next page' link/button")
    parser.add_argument(
        "--url-pattern",
        help="[Generic mode] URL template with {page}, e.g. 'https://site.com/reviews?page={page}'",
    )
    parser.add_argument(
        "--login-wall-selector",
        help="[Generic mode] CSS selector that only appears when the site wants you to log in",
    )
    parser.add_argument(
        "--login-wall-text",
        help="[Generic mode] visible text that only appears on a login wall",
    )

    parser.add_argument("--pages", type=int, default=3, help="Max number of pages to scrape (default: 3)")
    parser.add_argument("--out", default="reviews.csv", help="Output CSV path (default: reviews.csv)")
    args = parser.parse_args()

    if args.login:
        asyncio.run(login(args.login))
        return

    if args.company:
        start_url = f"https://{args.region}.trustpilot.com/review/{args.company}"
        asyncio.run(
            scrape(
                start_url=start_url,
                session_file=resolve_session_file(start_url),
                max_pages=args.pages,
                output_path=Path(args.out),
                mode="trustpilot",
                site_label=f"Trustpilot ({args.company})",
            )
        )
        return

    if args.url and args.item_selector:
        field_specs = [parse_field_arg(f) for f in (args.fields or [])]
        asyncio.run(
            scrape(
                start_url=args.url,
                session_file=resolve_session_file(args.url),
                max_pages=args.pages,
                output_path=Path(args.out),
                mode="generic",
                site_label=domain_of(args.url),
                item_selector=args.item_selector,
                field_specs=field_specs,
                next_selector=args.next_selector,
                url_pattern=args.url_pattern,
                wall_selector=args.login_wall_selector,
                wall_text=args.login_wall_text,
            )
        )
        return

    parser.error(
        "Nothing to do. Use --login URL to save a session, --company for the "
        "Trustpilot preset, or --url + --item-selector for generic mode."
    )


if __name__ == "__main__":
    main()
