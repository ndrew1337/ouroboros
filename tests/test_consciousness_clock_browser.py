"""Own-clock deadlines and separate wake lists on the shared desktop/touch UI."""
import pytest

from tests.test_subscription_setup_browser import subscription_ui as subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("width,zone,telegram", [(1280, "UTC", False), (390, "Europe/Moscow", False),
                                               (390, "Europe/Moscow", True)])
def test_deadline_and_both_lists_refresh_without_reload_or_hover(subscription_ui, width, zone, telegram):
    ui = subscription_ui
    page = ui["new_page"](viewport={"width": width, "height": 844}, has_touch=width < 980, timezone_id=zone)
    page.route("**/own-clock", lambda route: route.fulfill(content_type="text/html", body="""
        <!doctype html><html data-theme="light"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <link rel="stylesheet" href="/static/ui.css"><link rel="stylesheet" href="/static/style.css">
        <link rel="stylesheet" href="/static/settings.css"><link rel="stylesheet" href="/static/cards.css">
        <link rel="stylesheet" href="/static/room_work.css"><link rel="stylesheet" href="/static/consciousness.css">
        <style>body{display:block;margin:0}#bar{position:relative}#mount{height:70dvh;padding:16px}</style>
        </head><body><header id="bar" class="chat-page-header app-page-bar">
        <div class="app-page-title-block">Main</div></header><main id="mount"></main></body></html>
    """))
    page.goto(ui["url"] + "/own-clock")
    page.evaluate("""async ({ width, telegram }) => {
        document.documentElement.dataset.layout = width < 980 ? 'stack' : 'docked';
        if (telegram) {
            document.documentElement.dataset.host = 'telegram';
            window.Telegram = { WebApp: { platform: 'ios' } };
        }
        const { normalizeConsciousness } = await import('/static/modules/consciousness_api.js');
        const { initConsciousnessDashboard } = await import('/static/modules/consciousness_dashboard.js');
        const { createConsciousnessStatus } = await import('/static/modules/consciousness_status.js');
        Date.now = () => Date.parse('2027-01-15T12:00:00Z');
        window.raw = { enabled: true, status: 'due', detail: 'The next wake is due.',
            next_wake_at: '2027-01-15T11:50:00Z', appointment_at: '2027-01-15T11:30:00Z',
            recent_wakes: ['own', 'focus'].flatMap(cause => Array.from({ length: 4 }, (_, n) => ({
                task_id: `${cause}-${n}`, cause, summary: `${cause} note ${n}`, phase: 'done',
                note: `The whole ${cause} note ${n}.`, started_at: '2027-01-15T11:00:00Z'
            }))) };
        window.snapshot = () => normalizeConsciousness(raw);
        window.cx = createConsciousnessStatus({ bar: document.querySelector('#bar'), read: async () => snapshot() });
        window.dash = initConsciousnessDashboard({ mount: document.querySelector('#mount'),
            read: async () => snapshot(), readPlacesFn: async () => ({ places: [] }),
            markdown: { mount() {}, enhance() {}, destroy() {} } });
        window.repaint = async () => {
            cx.update({ bg_consciousness_enabled: true, bg_consciousness_state: raw });
            await dash.refresh();
        };
        await repaint();
    }""", {"width": width, "telegram": telegram})
    chip = page.locator(".cx-chip")
    since = "since 11:30" if zone == "UTC" else "since 14:30"
    assert since in chip.get_attribute("aria-label")
    if width >= 980:
        assert page.locator(".cx-chip-word").inner_text() == since
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("document.querySelector('.cx-chip-word').hidden")
        assert since in chip.get_attribute("aria-label")
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_function("!document.querySelector('.cx-chip-word').hidden")
    else:
        assert page.locator(".cx-chip-word").is_hidden()
    chip.tap() if width < 980 else chip.click()
    for group, title in [("own", "Its own wakes"), ("places", "Wakes in places")]:
        assert page.locator(f'.cx-shade [data-wakes="{group}"] li').count() == 3
        region = page.locator(f'[data-cx-wake-group="{group}"]')
        assert region.get_attribute("aria-label") == title
        assert region.locator("[data-wake]").count() == 4
    box = page.locator(".cx-shade").bounding_box()
    assert 0 <= box["x"] and box["x"] + box["width"] <= width + 1
    assert box["y"] + box["height"] <= 844
    page.locator(".cx-shade .cx-door").first.scroll_into_view_if_needed()
    assert page.locator(".cx-shade .cx-door").first.is_visible()
    page.evaluate("cx.close()")
    own_note = page.locator('[data-cx-wake-group="own"] summary').first
    own_note.tap() if width < 980 else own_note.click()
    page.wait_for_function("document.querySelector('[data-cx-wake-group=own] details').open")
    page.evaluate("async () => { raw.recent_wakes = raw.recent_wakes.filter(row => row.cause === 'own'); await repaint(); }")
    assert page.locator('[data-cx-wake-group="own"] details').first.evaluate("el => el.open")
    assert "No wakes in places in the last 48 h." in page.locator('[data-cx-wake-group="places"]').inner_text()
    own_note.tap() if width < 980 else own_note.click()
    page.wait_for_function("!document.querySelector('[data-cx-wake-group=own] details').open")
    page.evaluate("async () => { raw.recent_wakes = []; await repaint(); }")
    chip.tap() if width < 980 else chip.click()
    for text in ["No own wakes with a recorded model call in the last 48 h.", "No wakes in places in the last 48 h."]:
        assert text in page.locator(".cx-shade").inner_text()
        assert text in page.locator("[data-cx-wakes]").inner_text()
    page.evaluate("async () => { raw.live_wake_task_id = 'held'; raw.live_wake_held = true; raw.status = 'wake_paused'; raw.detail = 'Wake-up held is paused.'; await repaint(); }")
    assert "since" not in chip.get_attribute("aria-label")
    assert "paused" in page.locator(".cx-state").inner_text()
    assert "Due since" not in page.locator("[data-cx-summary]").inner_text()
    page.evaluate("cx.close(); dash.destroy(); cx.destroy()")
