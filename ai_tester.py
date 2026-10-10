#!/usr/bin/env python3
"""
AI Website Tester for Sam Bakery
================================
What it does, in order:
 1. Opens the website in a real browser (Playwright + Chromium).
 2. Tests it like a user: clicks every link, opens product details, uses the
    filters and search, fills the order form with good and bad data, checks the
    bonus QR code and stamp card, and looks at the mobile layout.
 3. Saves a screenshot for every problem it finds (the evidence).
 4. Sends the problems + screenshots + page source to Claude (Anthropic API),
    which explains each problem and suggests a fix.
 5. Writes an HTML report.

Usage:
    python ai_tester.py <url-or-path> [--label before] [--headed]
Examples:
    python ai_tester.py ../bakery/index.html --label before --headed
    python ai_tester.py "../bakery/index.html?fixed=1" --label after --headed
    python ai_tester.py "https://YOURNAME.github.io/sam-bakery/?fixed=1" --label live

Set ANTHROPIC_API_KEY to get AI explanations (tests run without it).
"""
import argparse, base64, datetime, html, json, os, re, sys, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
IGNORED_ERRORS = ("Failed to load resource", "net::ERR")  # e.g. fonts blocked offline


class Tester:
    def __init__(self, page, base_url, outdir):
        self.page, self.base = page, base_url
        self.out = Path(outdir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.findings, self.passed, self.js_errors = [], [], []
        page.on("pageerror", lambda e: self.js_errors.append(str(e)))
        page.on("console", self._on_console)

    def _on_console(self, msg):
        if msg.type == "error" and not any(s in msg.text for s in IGNORED_ERRORS):
            self.js_errors.append(msg.text)

    # ---------- helpers ----------
    def goto(self, section=None):
        self.page.goto(self.base)
        self.page.wait_for_selector("nav a")
        if section:
            self.page.evaluate("h => { location.hash = h }", section)
            self.page.wait_for_timeout(150)

    def fail(self, title, detail, shot_name):
        n = len(self.findings) + 1
        safe = re.sub(r"[^a-z0-9_]+", "_", shot_name.lower())
        path = self.out / f"{n:02d}_{safe}.png"
        self.page.screenshot(path=str(path), full_page=True)
        self.findings.append({"id": n, "title": title, "detail": detail, "screenshot": str(path)})
        print(f"  [FAIL] {title}")

    def ok(self, msg):
        self.passed.append(msg)
        print(f"  [ ok ] {msg}")

    def items(self):
        return self.page.eval_on_selector_all(
            ".item", "els => els.map(e => [e.querySelector('strong').textContent, e.dataset.tags || ''])")

    # ---------- 1. load ----------
    def test_load(self):
        print("Test: page load")
        self.goto()
        if not self.page.title().strip():
            self.fail("Page has no title", "The <title> tag is empty.", "no_title")
        elif self.js_errors:
            self.fail("JavaScript error on page load", "; ".join(self.js_errors), "load_error")
        elif not self.items():
            self.fail("Catalog is empty", "The home page shows no products.", "empty_catalog")
        else:
            self.ok(f"Home page loads, {len(self.items())} products shown, no errors")

    # ---------- 2. every internal link ----------
    def test_links(self):
        print("Test: all internal links (menu + footer)")
        self.goto()
        links = self.page.eval_on_selector_all(
            "a[href^='#']", "els => els.map(e => [e.textContent.trim(), e.getAttribute('href')])")
        for text, href in links:
            self.goto()
            self.page.click(f"a[href='{href}']")
            self.page.wait_for_timeout(200)
            target = self.page.locator(f"[id='{href[1:]}']")
            if target.count() == 0 or not target.first.is_visible():
                self.fail(f"Link '{text}' is broken",
                          f"Clicking '{text}' (href={href}) shows nothing: there is no visible "
                          f"element with id '{href[1:]}'.", f"broken_link_{text}")
            else:
                self.ok(f"Link '{text}' works")

    # ---------- 3. product details ----------
    def test_details(self):
        print("Test: product 'Details' windows")
        self.goto()
        buttons = self.page.locator("button.more")
        n = buttons.count()
        if n == 0:
            self.fail("No 'Details' buttons", "Products have no Details button.", "no_details")
            return
        for i in range(min(n, 3)):
            self.goto()
            name = self.page.locator(".item strong").nth(i).inner_text()
            self.page.locator("button.more").nth(i).click()
            self.page.wait_for_timeout(200)
            dlg = self.page.locator("dialog[open]")
            if dlg.count() == 0:
                self.fail(f"Details for '{name}' do not open", "Clicking Details opened nothing.", f"details_{name}")
                continue
            text = dlg.inner_text()
            if name not in text or dlg.locator("li").count() == 0 or "so'm" not in text:
                self.fail(f"Details for '{name}' are incomplete",
                          "The window should show the name, price and an ingredients list. "
                          f"It shows: {text[:200]!r}", f"details_incomplete_{name}")
                continue
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)
            if self.page.locator("dialog[open]").count() != 0:
                self.fail(f"Details window for '{name}' cannot be closed with Esc", "Still open after Esc.", f"details_close_{name}")
            else:
                self.ok(f"Details for '{name}' open with ingredients and close properly")

    # ---------- 4. filters ----------
    def test_filters(self):
        print("Test: category filter buttons")
        self.goto()
        self.page.click("button[data-filter='all']")
        baseline = self.items()
        labels = self.page.eval_on_selector_all("button[data-filter]", "els => els.map(e => e.dataset.filter)")
        for label in labels:
            if label == "all":
                continue
            self.page.click(f"button[data-filter='{label}']")
            self.page.wait_for_timeout(150)
            got = {n for n, _ in self.items()}
            expected = {n for n, tags in baseline if label in tags.split()}
            if got != expected:
                self.fail(f"Filter button '{label}' does not work",
                          f"After clicking '{label}' the page shows {len(got)} products but "
                          f"{len(expected)} products belong to '{label}'. "
                          f"Expected: {sorted(expected)}. Shown: {sorted(got)}.", f"filter_{label}")
            else:
                self.ok(f"Filter '{label}' shows exactly the {len(expected)} right products")

    # ---------- 5. search ----------
    def test_search(self):
        print("Test: search box")
        self.goto()
        box = self.page.locator("input[type=search]")
        before = len(self.js_errors)
        box.fill("zzzqx")
        self.page.wait_for_timeout(200)
        new_errors, shown = self.js_errors[before:], self.items()
        if new_errors or shown:
            detail = "Searched for nonsense text 'zzzqx'. "
            if new_errors:
                detail += "JavaScript error(s): " + "; ".join(new_errors) + ". "
            if shown:
                detail += f"{len(shown)} unrelated products are still displayed."
            self.fail("Search breaks when nothing matches", detail, "search_no_results")
        else:
            self.ok("Search with no matches shows a friendly message, no errors")
        box.fill("cake")
        self.page.wait_for_timeout(200)
        names = [n for n, _ in self.items()]
        if not names or any("cake" not in n.lower() for n in names):
            self.fail("Search returns wrong results", f"Searching 'cake' showed: {names}", "search_wrong")
        else:
            self.ok("Search 'cake' returns only matching products")

    # ---------- 6. forms ----------
    VALID = {"text": "Test Student", "email": "test@example.com", "number": "2",
             "date": (datetime.date.today() + datetime.timedelta(days=7)).isoformat()}
    BAD = {"text": "", "email": "not-an-email", "number": "-3", "date": "2000-01-01"}

    def _submit(self, form, bad_index=None):
        inputs = form.locator("input:not([type=hidden]):not([type=submit])")
        for i in range(inputs.count()):
            kind = inputs.nth(i).get_attribute("type") or "text"
            inputs.nth(i).fill((self.BAD if i == bad_index else self.VALID).get(kind, "x"))
        form.locator("button[type=submit]").click()
        self.page.wait_for_timeout(200)
        status = self.page.locator("[role=status]:visible")
        return status.count() > 0 and status.first.inner_text().strip() != ""

    def test_forms(self):
        print("Test: order form (valid and invalid data)")
        self.goto()
        for f in range(self.page.locator("form").count()):
            self.goto()
            section = self.page.locator("form").nth(f).evaluate("el => el.closest('section')?.id || ''")

            def open_form():
                self.goto(section)
                return self.page.locator("form").nth(f)

            form = open_form()
            inputs = form.locator("input:not([type=hidden]):not([type=submit])")
            fields = [(i, inputs.nth(i).get_attribute("name") or f"field{i}",
                       inputs.nth(i).get_attribute("type") or "text") for i in range(inputs.count())]
            if self._submit(form):
                self.ok("Form accepts valid data")
            else:
                self.fail("Form rejects valid data", "All fields were valid but no confirmation appeared.", "form_valid_rejected")
            for i, name, kind in fields:
                form = open_form()
                if self._submit(form, bad_index=i):
                    self.fail(f"Form accepts invalid '{name}'",
                              f"Submitted with every field valid except '{name}' = {self.BAD.get(kind, 'x')!r} "
                              f"({kind} field). The site still showed a success message.", f"form_bad_{name}")
                else:
                    self.ok(f"Invalid '{name}' is rejected")

    # ---------- 7. bonus card ----------
    def _stamps(self):
        m = re.search(r"(\d+) of 10", self.page.locator("#cardSub").inner_text())
        return int(m.group(1)) if m else -1

    def test_bonus(self):
        print("Test: bonus card, QR code and stamps")
        self.goto()
        self.page.evaluate("() => localStorage.clear()")
        self.goto("bonus")
        rows = self.page.locator("#rewards tbody tr").count()
        if rows != 10:
            self.fail("Reward table is wrong", f"Expected 10 rows, found {rows}.", "bonus_table")
        else:
            self.ok("Reward table has 10 rows")
        self.page.click("#showQr")
        self.page.wait_for_timeout(500)
        qr = self.page.locator("#qr canvas, #qr img")
        if self.page.locator("dialog[open]").count() == 0 or qr.count() == 0:
            self.fail("QR code does not appear", "Clicking 'Show QR code' did not show a QR image.", "bonus_qr")
        else:
            self.ok("QR code window opens and shows a code")
        self.page.keyboard.press("Escape")
        start = self._stamps()
        self.goto("order")
        form = self.page.locator("form").first
        self._submit(form)  # valid order
        self.goto("bonus")
        end = self._stamps()
        if end != start + 1:
            self.fail("Stamp is not added after an order",
                      f"Stamps before a valid order: {start}. After: {end}. Expected {start + 1}.", "bonus_stamp")
        else:
            self.ok("A valid order adds one stamp to the card")

    # ---------- 8. mobile layout ----------
    def test_mobile(self):
        print("Test: phone-sized screen (375px)")
        self.page.set_viewport_size({"width": 375, "height": 700})
        self.goto()
        ids = self.page.eval_on_selector_all("nav a", "els => els.map(e => e.getAttribute('href'))")
        for href in ids:
            self.goto(href[1:])
            wide = self.page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            if wide:
                self.fail(f"Page '{href}' scrolls sideways on a phone",
                          "Content is wider than a 375px screen.", f"mobile_overflow_{href[1:]}")
            else:
                self.ok(f"Page '{href}' fits a phone screen")
        self.page.set_viewport_size({"width": 1100, "height": 800})

    def run(self):
        for t in (self.test_load, self.test_links, self.test_details, self.test_filters,
                  self.test_search, self.test_forms, self.test_bonus, self.test_mobile):
            try:
                t()
            except Exception as e:  # a crash is reported, never hidden
                print(f"  [ERR ] {t.__name__}: {e}")
                self.fail(f"Test '{t.__name__}' could not finish", str(e), f"tester_{t.__name__}")


# ------------------------------------------------------------ AI explanation
def ask_claude(findings, page_source):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not findings:
        return {}
    content = []
    for f in findings[:6]:
        b64 = base64.b64encode(Path(f["screenshot"]).read_bytes()).decode()
        content.append({"type": "text", "text": f"Screenshot for finding #{f['id']}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    slim = [{k: v for k, v in f.items() if k != "screenshot"} for f in findings]
    content.append({"type": "text", "text":
        "You are a QA engineer. An automated browser test found these problems on a small bakery website. "
        "For each finding, explain in plain language what is wrong and why it matters to a user, then give a "
        "specific code-level fix using the page source. If several findings share one root cause, say so. "
        'Reply with ONLY a JSON array: [{"id": 1, "explanation": "...", "fix": "..."}].\n\nFINDINGS:\n'
        + json.dumps(slim, indent=2) + "\n\nPAGE SOURCE:\n" + page_source[:25000]})
    body = json.dumps({"model": MODEL, "max_tokens": 4000,
                       "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = json.load(r)["content"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return {item["id"]: item for item in json.loads(text)}
    except Exception as e:
        print(f"  (AI explanation unavailable: {e})")
        return {}


# -------------------------------------------------------------------- report
def write_report(path, url, label, findings, passed, ai):
    esc = html.escape
    cards = ""
    for f in findings:
        img = base64.b64encode(Path(f["screenshot"]).read_bytes()).decode()
        a = ai.get(f["id"], {})
        ai_html = (f"<h4>AI explanation</h4><p>{esc(a.get('explanation', ''))}</p>"
                   f"<h4>Suggested fix</h4><pre>{esc(a.get('fix', ''))}</pre>") if a else \
                  "<p><em>No AI explanation (set ANTHROPIC_API_KEY).</em></p>"
        cards += (f"<div class=card><h3>#{f['id']} {esc(f['title'])}</h3><p>{esc(f['detail'])}</p>"
                  f"<img src='data:image/png;base64,{img}' alt='evidence'>{ai_html}</div>")
    verdict = "<span class=bad>PROBLEMS FOUND</span>" if findings else "<span class=good>ALL CHECKS PASSED</span>"
    ok_list = "".join(f"<li>{esc(p)}</li>" for p in passed)
    Path(path).write_text(f"""<!DOCTYPE html><meta charset=utf-8><title>Test report - {esc(label)}</title>
<style>body{{font:16px/1.5 system-ui;max-width:900px;margin:30px auto;padding:0 16px}}
.card{{border:1px solid #ccc;border-left:6px solid #c0392b;border-radius:6px;padding:12px 18px;margin:18px 0}}
img{{max-width:100%;border:1px solid #ddd}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:10px}}
.bad{{color:#c0392b}}.good{{color:#1e8449}}li{{color:#1e8449}}</style>
<h1>Website test report: {esc(label)} - {verdict}</h1>
<p>{esc(url)}<br>{datetime.datetime.now():%Y-%m-%d %H:%M} | {len(findings)} problem(s) | {len(passed)} check(s) passed</p>
{cards}<h2>Passed checks</h2><ul>{ok_list}</ul>""", encoding="utf-8")


def normalise(target):
    if target.startswith(("http://", "https://", "file://")):
        return target
    p, _, query = target.partition("?")
    return Path(p).resolve().as_uri() + ("?" + query if query else "")


def main():
    ap = argparse.ArgumentParser(description="AI website tester")
    ap.add_argument("url", help="website URL or path to index.html (may end with ?fixed=1)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--headed", action="store_true", help="show the browser window (good for the demo video)")
    args = ap.parse_args()
    url, out = normalise(args.url), Path("results") / args.label

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, slow_mo=250 if args.headed else 0)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        print(f"Testing {url}\n")
        t = Tester(page, url, out)
        t.run()
        page.goto(url)
        source = page.content()
        browser.close()

    print("\nAsking Claude to explain the problems..." if t.findings else "\nNo problems found.")
    ai = ask_claude(t.findings, source)
    write_report(out / "report.html", url, args.label, t.findings, t.passed, ai)
    (out / "results.json").write_text(json.dumps(t.findings, indent=2))
    print(f"\n{len(t.findings)} problem(s), {len(t.passed)} passed. Report: {out / 'report.html'}")
    sys.exit(1 if t.findings else 0)


if __name__ == "__main__":
    main()
