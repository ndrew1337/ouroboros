"""Real Dashboard › Updates and the legacy Apply confirmation with the update description.

The page is the shipped SPA modules (dashboard.js, updates.js, update_status.js, confirm_dialog.js,
chat_markdown.js) served from ``web/``; only the API replies are synthetic, derived from the real
``1b5608..fcd6`` and ``#2367`` ranges (``tests/fixtures/update_description_ranges.json``: receipts
parsed by the production collector, post-merge runs from a retained live read). No runtime, task or
model starts. Screens land in the test-evidence directory for inspection.

Covered: an update on offer with nothing written offers Write beside Check; Write posts the exact range
once and the request's task shows as writing; Apply opened meanwhile pins only its own prose, so the
worker's ``update_progress_changed`` and the task's ``task_done`` behind it still read the status and
Cancel shows the large structured description rendered as Markdown; Check never authors and keeps the
read text's DOM; the Apply confirmation renders a large and a small description in a bounded region,
keeps the consequences in its body at the prose's size and ink (ordinary dialogs keep theirs) and every
per-commit receipt inside collapsed Details, and cancels by Escape and by Cancel without applying.
"""
import copy
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest

from ouroboros.test_evidence import diagnostic_directory
from tests.test_update_letter_browser import WEB
from tests.ui_console_errors import PageErrors

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "update_description_ranges.json").read_text(encoding="utf-8"))
BOOT = """<script type="module">
import {initDashboard} from '/static/modules/dashboard.js';
import {initUpdates} from '/static/modules/updates.js';
document.querySelectorAll('[data-nav-page]').forEach(node=>node.classList.toggle('active',node.dataset.navPage==='dashboard'));
const state={activePage:'dashboard',dashboardActiveSubtab:'updates'};
const dashboard=initDashboard({state}); dashboard.page.classList.add('active');
const handlers=new Map();
window.__wsHandlers=handlers;
const ws={on(type,fn){handlers.set(type,fn);return()=>handlers.delete(type);},isConnected:()=>true};
initUpdates({mount:document.getElementById('dashboard-panel-updates'),state,ws});
dashboard.activateTab('updates');
</script>"""
TASK = "d3adb33f"
INK = "el => [getComputedStyle(el).fontSize, getComputedStyle(el).color]"
PIN = {"action": "keep", "direction": "forward", "serving": {"version": "4.8.0"}, "target": {"version": "4.8.0"}}
EFFECTS = {"engine": PIN, "replacement_engine": PIN, "active_work": {"status": "known", "model": 0, "non_model": 1},
           "runtime": {"installed": {"python": "3.10.19", "sqlite": "3.50.4"},
                       "bundle": {"python": "3.10.19", "sqlite": "3.50.4"}}}


def _letter(case, state):
    """The ``letter`` the backend projects for one case and stage (shapes of update_letter.project_letter)."""
    key = case["key"]
    base = {"state": "none", "relation": "", "text": "", "author_version": "", "target_version": "", "written_at": "",
            "error_kind": "", "error_text": "", "key": {}, "has_last_good": False, "description_current": False,
            "failed_at": "", "latest_failed_key": None, "qualification": case["qualification"], "revision": 0,
            "author": {}, "checked_key": key, "material": {"key": key, "text_bytes": 152000, "commits": case["commits"]},
            "request": None}
    request = {"task_id": TASK, "mode": "write", "key": key, "requested_at": "2026-10-28T11:20:00Z"}
    if state == "writing":
        return {**base, "request": {**request, "status": "running", "saved": False}}
    if state == "ready":
        return {**base, "state": "ready", "relation": "pending", "text": case["text"], "key": key,
                "description_current": True, "revision": 1, "author_version": "7.8.1", "target_version": case["version"],
                "written_at": "2026-10-28T11:31:00Z",
                "author": {"task_id": TASK, "model": "anthropic/claude-opus-5.5", "requested": True},
                "request": {**request, "status": "completed", "saved": True}}
    return base


def _status(case, letter):
    key = case["key"]
    return {"managed": True, "check_ok": None, "available": True, "safe_to_apply": True,
            "current_version": "7.8.1", "latest_version": case["version"], "current_sha": key["base_sha"],
            "current_short_sha": key["base_sha"][:8], "running_sha": key["base_sha"], "latest_sha": key["target_sha"],
            "latest_short_sha": key["target_sha"][:8], "checked_target_sha": key["target_sha"],
            "update_channel": "development", "target_ref": "managed/ouroboros", "behind": case["commits"], "ahead": 0,
            "local_changes": "absent", "warnings": [], "update_tx": {"active": False}, "update_progress": {},
            "checked_at": "2026-10-28T08:10:03Z", "letter": letter}


def _preflight(case):
    key = case["key"]
    plan = {"available": True, "kind": "clean", "auto_mergeable": True, "base_sha": key["base_sha"],
            "target_sha": key["target_sha"], "local_dirty_count": 0, "update_channel": "development",
            "target_ref": "managed/ouroboros", "recommended_strategy": "auto_merge"}
    return {"merge_plan": plan, "effects": {**EFFECTS, "target_sha": key["target_sha"]},
            "letter": _letter(case, "ready"), "qualification": case["qualification"]}


@pytest.mark.parametrize(("engine", "width", "height"), [("webkit", 1165, 851), ("chromium", 1165, 851),
                                                          ("webkit", 390, 844)])
def test_write_check_and_apply_read_the_description_as_prose(engine, width, height, tmp_path):
    if os.environ.get("OUROBOROS_RUN_UI_SMOKE") != "1":
        pytest.skip("Set OUROBOROS_RUN_UI_SMOKE=1")
    playwright = pytest.importorskip("playwright.sync_api")
    output = diagnostic_directory(tmp_path, "update-description")
    large, small = FIXTURE["large"], FIXTURE["small"]
    current = {"status": _status(large, _letter(large, "none")), "preflight": _preflight(large)}
    calls = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/fixture":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = (WEB / "index.html").read_text(encoding="utf-8").replace(
                    '<script type="module" src="/static/app.js"></script>', BOOT)
                self.wfile.write(html.encode("utf-8"))
            else:
                self.path = self.path.removeprefix("/static")
                super().do_GET()

    def respond(route):
        path = urlparse(route.request.url).path
        calls.append((path, route.request.method, route.request.post_data))
        if path == "/api/update/description":
            current["status"] = _status(large, _letter(large, "writing"))
            body = {"status": "queued", "task_id": TASK, "key": large["key"]}
        elif path in ("/api/update/status", "/api/update/check"):
            body = {**current["status"], "check_ok": path == "/api/update/check" or None}
        elif path == "/api/update/preflight":
            body = current["preflight"]
        elif path == "/api/update/apply":
            body = {"status": "ok", "restarting": True}
        else:
            body = {"commits": [], "tags": []}
        route.fulfill(content_type="application/json", body=json.dumps(body))

    def shot(page, name):
        path = output / f"{name}-{engine}-{width}.png"
        page.screenshot(path=str(path), full_page=False)
        print(f"UPDATE_DESCRIPTION_SCREENSHOT={path}")

    def posted(path):
        return [json.loads(body) if body else None for p, method, body in calls if p == path and method == "POST"]

    def status_reads():
        return sum(1 for p, _method, _body in calls if p == "/api/update/status")

    def wait_until(condition, what):
        for _ in range(50):
            if condition():
                return
            page.wait_for_timeout(100)
        raise AssertionError(f"the panel never read the status again after {what}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            if engine == "webkit" and not Path(pw.webkit.executable_path).is_file():
                pytest.skip(f"{engine} is not installed in the test interpreter")
            browser = getattr(pw, engine).launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": height}, has_touch=width < 980)
                errors = PageErrors(page, engine)
                page.route("**/api/**", respond)
                page.goto(f"http://127.0.0.1:{server.server_port}/fixture")

                # 1. An update on offer with nothing written: the section stays, with Check and Write.
                page.wait_for_selector("#updates-letter:not([hidden])")
                assert page.locator("#updates-letter-note").inner_text() == "No description of this update yet."
                write = page.locator("#updates-letter-write")
                assert write.is_visible() and write.inner_text() == "Write description" and write.is_enabled()
                assert page.locator("#updates-letter-refresh-label").inner_text() == "Check"
                assert page.locator("#btn-update-primary").inner_text() == "Update to 7.8.1"
                shot(page, "01-empty")

                # 2. Write posts the exact offered range once; the request's own task shows as writing.
                write.click()
                page.wait_for_function("document.querySelector('#updates-letter-write').textContent === 'Writing…'")
                assert posted("/api/update/description") == [{"mode": "write", "base_sha": large["key"]["base_sha"],
                                                              "target_sha": large["key"]["target_sha"]}]
                assert page.locator("#updates-letter-write").is_disabled()
                assert page.locator("#updates-letter-note").inner_text() == f"Ouroboros is writing the description (task {TASK})."
                assert posted("/api/update/check") == [], "Write is never a check"
                shot(page, "02-writing")

                # 3. Apply opened while it writes: the plan has no text yet and the confirmation says so. The
                #    worker's save (update_progress_changed) and its task's end (task_done) arrive while it is
                #    open; the plan pins only the dialog's prose, so Cancel shows the description now saved.
                current["preflight"] = {**_preflight(large), "letter": _letter(large, "none")}
                page.locator("#btn-update-primary").click()
                dialog = page.get_by_role("dialog")
                dialog.wait_for()
                assert "No description is available for this exact preflight range." in dialog.inner_text()
                current["status"] = _status(large, _letter(large, "ready"))
                for frame in ("window.__wsHandlers.get('update_progress_changed')()",
                              f"window.__wsHandlers.get('log')({{data: {{type: 'task_done', task_id: '{TASK}'}}}})"):
                    before = status_reads()
                    page.evaluate(frame)
                    wait_until(lambda: status_reads() > before, frame)
                dialog.get_by_role("button", name="Cancel", exact=True).click()
                dialog.wait_for(state="detached")
                current["preflight"] = _preflight(large)
                page.wait_for_selector("#updates-letter-body h2, #updates-letter-body .md-h2")
                body = page.locator("#updates-letter-body")
                rendered = body.inner_text()
                assert "Что будет заметно сразу" in rendered and "Telegram." in rendered
                assert "##" not in rendered and "**" not in rendered, "Markdown renders, never shows raw"
                assert body.locator("li").count() >= 12
                assert page.locator("#updates-letter-label").inner_text() == "What's new"
                covers = page.locator("#updates-letter-covers").inner_text()
                assert f"{large['key']['base_sha'][:8]} → {large['key']['target_sha'][:8]}" in covers
                assert page.locator("#updates-letter-write").inner_text() == "Rewrite"
                assert page.locator("#updates-letter-note").is_hidden()
                assert page.evaluate("document.documentElement.scrollWidth<=document.documentElement.clientWidth")
                heading, actions = (page.locator(f".updates-letter-{part}").bounding_box() for part in ("heading", "actions"))
                if width < 500:  # a phone: the actions follow the heading instead of squeezing its date and range
                    assert actions["y"] >= heading["y"] + heading["height"] - 1, (heading, actions)
                else:
                    assert actions["y"] < heading["y"] + heading["height"], "one row while both fit"
                shot(page, "03-large")
                page.locator("#updates-letter-details summary").click()
                provenance = page.locator("#updates-letter-provenance").inner_text()
                assert f"in task {TASK}" in provenance and "at your request" in provenance
                page.locator("#updates-letter-details summary").click()

                # 4. Check is facts only: a refreshed run leaves the read text's very DOM node in place.
                page.evaluate("window.savedBody=document.querySelector('#updates-letter-body').firstChild")
                ready = _status(large, _letter(large, "ready"))
                ready["letter"]["qualification"] = copy.deepcopy(large["qualification"])
                ready["letter"]["qualification"]["github"]["runs"][0]["conclusion"] = "failure"
                current["status"] = ready
                page.locator("#updates-letter-refresh").click()
                page.wait_for_selector(".toast")
                assert page.locator(".toast").first.inner_text() == "Description is up to date."
                assert page.evaluate("window.savedBody===document.querySelector('#updates-letter-body').firstChild")
                assert len(posted("/api/update/check")) == 1 and len(posted("/api/update/description")) == 1

                # 5. Apply: the large description reads as prose in its own region; evidence stays in Details.
                page.locator("#btn-update-primary").click()
                dialog = page.get_by_role("dialog")
                dialog.wait_for()
                region = dialog.locator("[data-confirm-content]")
                assert region.get_attribute("aria-label") == "What's new"
                region_text = region.inner_text()
                assert "Что будет заметно сразу" in region_text and "##" not in region_text and "**" not in region_text
                assert region.locator("li").count() >= 12
                plain = dialog.locator(".marketplace-modal-body > p").inner_text()
                for fact in ("Git applies this update directly", "Claudexor: 4.8.0 → 4.8.0", "forward-only"):
                    assert fact in plain, fact
                details = dialog.locator("details.confirm-dialog-details")
                assert details.locator("summary").inner_text() == "Checks for this range"
                assert details.get_attribute("open") is None, "per-commit receipts stay collapsed"
                # Beside the prose, the consequences and the Details summary read at its size and ink.
                prose = region.locator(".updates-letter-body").evaluate(INK)
                assert prose[0] == "14px" and dialog.locator(".marketplace-modal-body > p").evaluate(INK) == prose
                assert details.locator("summary").evaluate(INK) == prose
                assert "post-merge" not in plain and "full_battery" not in plain and "receipt" not in plain
                box = region.bounding_box()
                assert box["height"] <= min(0.42 * height, 380) + 2, box
                actions = dialog.locator(".marketplace-modal-actions").bounding_box()
                assert actions["y"] + actions["height"] <= height, "the actions stay in view beside long prose"
                shot(page, "04-apply-large")
                details.locator("summary").click()
                rows = details.locator(".confirm-dialog-detail-row")
                assert rows.count() == len(large["qualification"]["commits"]) + 1 + len(large["qualification"]["github"]["runs"]) + 1
                assert "Merge pull request #2384" in details.inner_text() and "No matching run observed" in details.inner_text()
                rows.nth(3).scroll_into_view_if_needed()
                shot(page, "05-apply-large-details")
                # Keyboard: the prose region takes focus and scrolls; Escape cancels without applying.
                region.focus()
                assert page.evaluate("document.activeElement.matches('[data-confirm-content]')")
                page.keyboard.press("End")
                page.keyboard.press("Escape")
                dialog.wait_for(state="detached")
                assert posted("/api/update/apply") == []

                # 5b. The same screens in the light theme (the product has two).
                page.evaluate("document.documentElement.dataset.theme='light'")
                shot(page, "03-large-light")
                page.locator("#btn-update-primary").click()
                dialog.wait_for()
                shot(page, "04-apply-large-light")
                dialog.get_by_role("button", name="Cancel", exact=True).click()
                dialog.wait_for(state="detached")
                page.evaluate("delete document.documentElement.dataset.theme")

                # 6. A small description reads the same way; Cancel closes without applying.
                current["preflight"] = _preflight(small)
                current["status"] = _status(small, _letter(small, "ready"))
                page.evaluate("window.__wsHandlers.get('update_progress_changed')()")
                page.wait_for_function(f"document.querySelector('#updates-letter-covers').textContent.includes('{small['key']['target_sha'][:8]}')")
                page.locator("#btn-update-primary").click()
                dialog.wait_for()
                small_text = dialog.locator("[data-confirm-content]").inner_text()
                assert "Небольшое исправление Dashboard" in small_text and "**" not in small_text
                shot(page, "06-apply-small")
                dialog.get_by_role("button", name="Cancel", exact=True).click()
                dialog.wait_for(state="detached")
                assert posted("/api/update/apply") == []
                # An ordinary dialog keeps its own text: only a dialog carrying prose steps its facts down.
                page.evaluate("import('/static/modules/confirm_dialog.js')"  # the dialog's own promise stays open
                              ".then((m) => { m.openConfirmDialog({title: 'Plain', body: 'An ordinary question.'}); })")
                dialog.wait_for()
                assert dialog.locator(".marketplace-modal-body > p").evaluate(
                    "el => getComputedStyle(el).fontSize === getComputedStyle(el.parentElement).fontSize")
                page.keyboard.press("Escape")
                dialog.wait_for(state="detached")
                assert errors.unexpected() == []
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
