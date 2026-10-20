"""Approved Talk A against the real SPA and loaded feature assets.

All sockets/media are synthetic page objects; no getUserMedia, device, provider
or runtime is used. Run with the unchanged safe_test launcher and UI opt-in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from tests import test_voice_browser as voice
from tests import test_subscription_setup_browser as browser

subscription_ui = browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
ROOT = Path(__file__).resolve().parents[1]
ASSETS = "/api/extensions/live_conversation/ui/synthetic/ui/"
CALL_ID = "a" * 32


@pytest.fixture
def visible_call_ui(monkeypatch):
    """Native tab visibility needs a window; headless Chromium keeps every page visible."""
    if os.environ.get("OUROBOROS_RUN_UI_SMOKE") != "1":
        pytest.skip("Set OUROBOROS_RUN_UI_SMOKE=1 to run the browser flow")
    adapter = ROOT / "web/tests/fixtures/call_native_visibility.cjs"
    monkeypatch.setenv("NODE_OPTIONS", f"--require {json.dumps(str(adapter))}")
    playwright = pytest.importorskip("playwright.sync_api")
    launch = playwright.BrowserType.launch
    monkeypatch.setattr(playwright.BrowserType, "launch",
                        lambda self, **options: launch(self, **{**options, "headless": False}))
    # The shorthand browser.new_page owns a one-page context. Use an explicit
    # context so the second page really is another tab of the same window.
    monkeypatch.setattr(playwright.Browser, "new_page",
                        lambda self, **options: self.new_context(**options).new_page())
    yield from browser.subscription_ui.__wrapped__()

FAKES = r"""(() => {
    window.__callFixture = { microphones: 0, stopped: 0, sent: [], sockets: [], tracks: [], contexts: [] };
    const fixture = window.__callFixture;
    class Track extends EventTarget {
        constructor() { super(); this.kind = 'audio'; this.enabled = true; this.readyState = 'live'; }
        stop() { this.readyState = 'ended'; fixture.stopped++; }
        getSettings() { return { sampleRate: 24000, channelCount: 1 }; }
    }
    const media = {
        async getUserMedia() {
            fixture.microphones++;
            const track = new Track();
            fixture.tracks.push(track);
            return { getTracks: () => [track], getAudioTracks: () => [track], getVideoTracks: () => [] };
        },
        async enumerateDevices() { return []; },
        addEventListener() {}, removeEventListener() {},
    };
    Object.defineProperty(navigator, 'mediaDevices', { value: media, configurable: true });
    class Context {
        constructor() { this.sampleRate = 24000; this.destination = {}; this.state = 'running'; fixture.contexts.push(this); }
        get audioWorklet() { return { addModule: async () => {} }; }
        resume() { return Promise.resolve(); }
        close() { this.state = 'closed'; return Promise.resolve(); }
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
        setSinkId() { return Promise.resolve(); }
    }
    window.AudioContext = Context;
    window.AudioWorkletNode = class {
        constructor() { this.port = { postMessage() {}, close() {}, onmessage: null }; }
        connect() {} disconnect() {}
    };
    const NativeSocket = window.WebSocket;
    window.WebSocket = class {
        static OPEN = 1; static CONNECTING = 0; static CLOSED = 3; static CLOSING = 2;
        constructor(url, protocols) {
            if (!String(url).includes('/ws/call/')) return new NativeSocket(url, protocols);
            this.readyState = 0; this.bufferedAmount = 0; fixture.sockets.push(this);
            setTimeout(() => { this.readyState = 1; this.onopen?.({}); }, 0);
        }
        send(raw) {
            if (typeof raw !== 'string') return;
            const data = JSON.parse(raw); fixture.sent.push(data);
            if (data.type === 'hello') this.emit({ type: 'state', state: 'live', reason: '' });
        }
        emit(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
        close() { this.readyState = 3; }
    };
})();"""


def open_call_ui(ui, monkeypatch, *, width, scheme, engine="openai", enabled=True, projects=(), refusal=""):
    state = {"whisper": True, "voice": {"stt": "whisper-voice:stt", "tts": "", "tts_voice": ""}}
    projection = voice.projection
    include_call = False

    def contributions(value):
        result = projection(value)
        if include_call:
            result["contributions"].append(voice._item("live_conversation:call", provides="conversation",
                runtime="client", destination="host", render={"kind": "host", "url": ASSETS + "index.js"}))
        return result
    monkeypatch.setattr(voice, "projection", contributions)
    monkeypatch.setattr(voice, "FAKES", FAKES)
    page = voice.open_app(ui, width=width, scheme=scheme, state=state)
    posts = []
    config = {"enabled": enabled, "provider": engine, "model": "synthetic-only", "voice": ""}
    ui["call_config"] = config
    ui["call_reads"] = []

    def extension(route):
        path = urlparse(route.request.url).path
        if path.startswith(ASSETS):
            relative = path.removeprefix(ASSETS)
            source = ROOT / "skills/live_conversation/ui" / relative
            assert source.is_relative_to(ROOT / "skills/live_conversation/ui") and source.is_file()
            route.fulfill(content_type="text/css" if source.suffix == ".css" else "text/javascript",
                          body=source.read_text())
        elif path == "/api/extensions/live_conversation/config":
            ui["call_reads"].append(route.request.method)
            route.fulfill(status=503 if ui.get("call_read_error") else 200,
                          content_type="application/json", body=json.dumps(config))
        else:
            route.fallback()
    page.route("**/api/extensions/live_conversation/**", extension)

    def admit(route):
        posts.append(route.request.post_data_json)
        if refusal:
            route.fulfill(status=400, content_type="application/json", body=json.dumps({
                "ok": False, "code": refusal, "error": "The selected engine needs a credential."}))
            return
        route.fulfill(content_type="application/json", body=json.dumps({
            "call_id": CALL_ID, "ws_path": "/ws/call/" + CALL_ID, "vision": engine != "grok"}))
    page.route("**/api/call/open", admit)
    if projects:
        page.route("**/api/projects*", lambda route: route.fulfill(
            content_type="application/json", body=json.dumps({"projects": list(projects)})))
        page.route("**/api/state", lambda route: route.fulfill(content_type="application/json", body=json.dumps({
            "supervisor_ready": True, "projects": list(projects), "active_chat_activities": [],
            "active_chat_activities_complete": True})))
    # The production host serves /static/modules only. The generic fixture
    # server also exposes its web root; reject that accidental second module
    # identity so a feature cannot acquire an independent media lease.
    page.route("**/modules/**", lambda route: route.fulfill(status=404, body="Not served by the host")
               if urlparse(route.request.url).path.startswith("/modules/") else route.fallback())
    include_call = True
    page.reload()
    page.wait_for_selector("#chat-input")
    return page, posts


@pytest.mark.parametrize("width,scheme", [(360, "dark"), (1280, "light")])
@pytest.mark.parametrize("engine", ["gemini", "grok", "openai"])
def test_talk_a_real_composer_and_loaded_feature(subscription_ui, monkeypatch, width, scheme, engine):
    page, posts = open_call_ui(subscription_ui, monkeypatch, width=width, scheme=scheme, engine=engine)
    field = page.locator("#page-chat #chat-input")
    talk = page.locator("#page-chat [data-call-entry='talk']")
    talk.wait_for()
    try:
        field.press("Enter")
        assert not posts and page.evaluate("window.__callFixture.microphones") == 0
        before = talk.bounding_box()
        radius = page.locator('#page-chat .chat-field').evaluate("node => getComputedStyle(node).borderRadius")
        field.fill("draft")
        send = page.locator("#page-chat .chat-send-inline")
        assert send.inner_text().strip() == "Send"
        assert abs(send.bounding_box()["width"] - before["width"]) < 1
        field.fill("")
        page.locator("#chat-file-input").set_input_files({"name": "synthetic.txt", "mimeType": "text/plain", "buffer": b"synthetic"})
        assert send.inner_text().strip() == "Send"
        page.locator(".attach-badge button").click()
        talk.wait_for()
        browser.capture(page, f"live-talk-{engine}-{width}-{scheme}")
        talk.click()
        page.wait_for_function("document.querySelector('#page-chat .chat-input-wrap')?.dataset.callActive === 'true'")
        page.wait_for_function("window.__callFixture.sent.some(x => x.type === 'hello')")
        assert len(posts) == 1 and posts[0]["chat_id"] == 1
        assert page.locator("#page-chat [data-composer-voice]").is_hidden()
        assert page.evaluate("window.__callFixture.microphones") == 1
        if engine == "grok":
            assert page.locator(".call-show").is_hidden()
            assert page.locator(".call-vision-note").inner_text() == "This voice engine does not accept images."
        else:
            assert page.locator(".call-show").is_visible()
            assert page.locator(".call-vision-note").count() == 0
        page.locator(".call-keyboard").click()
        draft = "Keep this draft: Обычный nano, tools & memory.\nA second line that must survive End."
        field.fill(draft)
        assert page.locator('#page-chat .chat-field').evaluate("node => getComputedStyle(node).borderRadius") == radius
        assert page.locator('#page-chat .chat-text-row').evaluate("node => node.scrollWidth <= node.clientWidth"), "call controls fit their row"
        end = page.locator('.call-end').bounding_box()
        assert end and end['x'] >= 0 and end['x'] + end['width'] <= width and end['y'] + end['height'] <= 800
        page.evaluate("""() => {
            const socket = window.__callFixture.sockets.at(-1);
            socket.emit({type:'caption',speaker:'owner',text:'wrong phrase',final:false,item_id:'one',replace:true});
            socket.emit({type:'caption',speaker:'owner',text:'Corrected phrase with a long project name — 2302258.',final:true,item_id:'one',replace:true});
        }""")
        voice.no_page_scroll(page)
        browser.capture(page, f"live-keyboard-{engine}-{width}-{scheme}")
        page.locator(".call-end").click()
        page.wait_for_function("document.querySelector('#page-chat .chat-input-wrap')?.dataset.callActive === 'false'")
        assert field.input_value() == draft
        page.locator("#page-chat .voice-mic").wait_for(state="visible")
        assert page.evaluate("window.__callFixture.stopped") > 0
        assert page.evaluate("window.__callFixture.sent.some(x => x.type === 'bye')")
        voice.no_page_scroll(page)
        browser.capture(page, f"live-ended-{engine}-{width}-{scheme}")
    finally:
        evidence = os.environ.get("OUROBOROS_TEST_EVIDENCE_ROOT")
        if evidence:
            target = Path(evidence) / "live-ui"
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{engine}-{width}-{scheme}.json").write_text(json.dumps({
                "synthetic_only": True, "posts": posts,
                "state": page.evaluate("({microphones: __callFixture.microphones, stopped: __callFixture.stopped, sent: __callFixture.sent})"),
                "page_errors": subscription_ui["errors"],
            }, indent=2))


def test_beta_off_has_no_talk_or_media(subscription_ui, monkeypatch):
    page, posts = open_call_ui(subscription_ui, monkeypatch, width=360, scheme="dark", enabled=False)
    page.wait_for_timeout(500)
    assert page.locator("[data-call-entry='talk']").count() == 0
    page.locator("#chat-input").press("Enter")
    assert not posts and page.evaluate("window.__callFixture.microphones") == 0


def start_call(page, room="#page-chat"):
    page.locator(f"{room} [data-call-entry='talk']").click()
    page.wait_for_function("window.__callFixture.sent.some(x => x.type === 'hello')")
    page.locator(f"{room} .call-state").filter(has_text="Listening").wait_for()


def assert_live(page):
    assert page.evaluate("""() => {
        const f = __callFixture;
        return f.stopped === 0 && f.tracks[0].readyState === 'live'
            && f.contexts[0].state === 'running' && f.sockets[0].readyState === 1
            && !f.sent.some(x => x.type === 'bye');
    }""")


def assert_ended(page):
    page.wait_for_function("window.__callFixture.sent.some(x => x.type === 'bye')")
    assert page.evaluate("""() => {
        const f = __callFixture;
        return f.stopped > 0 && f.contexts[0].state === 'closed' && f.sockets[0].readyState === 3;
    }""")


@pytest.mark.parametrize("reason", ["pagehide", "panic", "device_loss", "server_stop"])
def test_call_lifecycle_ends_capture_playback_and_socket(subscription_ui, monkeypatch, reason):
    page, _ = open_call_ui(subscription_ui, monkeypatch, width=1280, scheme="light")
    start_call(page)
    assert_live(page)
    page.evaluate("""reason => {
        if (reason === 'server_stop') __callFixture.sockets[0].emit({type:'state', state:'closed', reason:'owner_stop'});
        else if (reason === 'device_loss') __callFixture.tracks[0].dispatchEvent(new Event('ended'));
        else window.dispatchEvent(new Event(reason === 'panic' ? 'ouro:panic' : 'pagehide'));
    }""", reason)
    assert_ended(page)
    assert not subscription_ui["errors"]


def test_background_document_keeps_call_until_pagehide(visible_call_ui, monkeypatch):
    page, _ = open_call_ui(visible_call_ui, monkeypatch, width=1280, scheme="light")
    start_call(page)
    # Native browser tab visibility, without overriding document.hidden or dispatching visibilitychange.
    other = page.context.new_page()
    try:
        other.goto("about:blank")
        other.bring_to_front()
        page.wait_for_function("document.hidden === true", polling=100, timeout=5000)
        assert_live(page)
        page.bring_to_front()
        page.wait_for_function("document.hidden === false", polling=100)
        assert_live(page)
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        assert_ended(page)
    finally:
        other.close()


def test_failed_config_reads_keep_call_and_confirmed_off_ends_it(subscription_ui, monkeypatch):
    page, _ = open_call_ui(subscription_ui, monkeypatch, width=1280, scheme="light")
    start_call(page)
    subscription_ui["call_read_error"] = True
    for trigger in ["focus", "visibilitychange", "ouro:settings-updated"]:
        count = len(subscription_ui["call_reads"])
        page.evaluate("""trigger => (trigger === 'visibilitychange' ? document : window)
            .dispatchEvent(new Event(trigger))""", trigger)
        voice.wait_until(lambda: len(subscription_ui["call_reads"]) > count, page)
        assert_live(page)
    subscription_ui["call_read_error"] = False
    # A successful still-on response stays quiet too.
    count = len(subscription_ui["call_reads"])
    page.evaluate("window.dispatchEvent(new Event('ouro:settings-updated'))")
    voice.wait_until(lambda: len(subscription_ui["call_reads"]) > count, page)
    assert_live(page)
    subscription_ui["call_config"]["enabled"] = False
    page.evaluate("window.dispatchEvent(new Event('ouro:settings-updated'))")
    assert_ended(page)
    assert page.locator("[data-call-entry='talk']").count() == 0


@pytest.mark.parametrize("code", ["credential", "missing_credential"])
def test_missing_key_sheet_opens_settings(subscription_ui, monkeypatch, code):
    from playwright.sync_api import expect
    page, posts = open_call_ui(subscription_ui, monkeypatch, width=1280, scheme="light", refusal=code)
    page.locator("[data-call-entry='talk']").click()
    expect(page.locator(".call-state")).to_have_text("The selected engine needs a credential.")
    expect(page.locator(".call-action").first).to_have_text("Open Settings")
    assert len(posts) == 1 and page.evaluate("__callFixture.stopped") > 0
    page.locator(".call-action").first.click()
    expect(page.locator("#page-settings")).to_be_visible()
    assert page.evaluate("__callFixture.sockets.length") == 0


@pytest.mark.parametrize("from_reference", [False, True], ids=["sidebar", "outside-sidebar"])
def test_project_call_names_and_reveals_its_room_across_navigation(subscription_ui, monkeypatch, from_reference):
    from playwright.sync_api import expect
    projects = [{"id": "voice-room", "name": "Voice project", "chat_id": 71, "lifecycle": "active"},
                {"id": "other-room", "name": "Other project", "chat_id": 72, "lifecycle": "active"}]
    page, posts = open_call_ui(subscription_ui, monkeypatch, width=1280, scheme="light",
                               projects=projects[1:] if from_reference else projects)
    if from_reference:
        page.evaluate("project => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail: {project}}))", projects[0])
    else:
        page.locator('.nav-project-row[data-project-id="voice-room"]').click()
    room = "#panel-pchat-voice-room"
    start_call(page, room)
    assert posts[-1]["chat_id"] == 71
    page.locator('[data-nav-page="settings"]').click()
    mark = page.locator(".call-mark")
    expect(mark).to_be_visible()
    expect(mark.locator(".call-mark-room")).to_have_text("Voice project")
    assert_live(page)
    mark.locator(".call-mark-open").click()
    expect(page.locator(room)).to_be_visible()
    expect(mark).to_be_hidden()
    page.locator('.nav-project-row[data-project-id="other-room"]').click()
    expect(page.locator(room)).to_be_hidden()
    other = page.locator("#panel-pchat-other-room")
    other.locator("[data-call-entry='talk']").click()
    expect(other.locator(".call-action").first).to_have_text("Go to the call")
    other.locator(".call-action").first.click()
    expect(page.locator(room)).to_be_visible()
    assert_live(page)
    assert len(posts) == 1
    page.locator('[data-nav-page="settings"]').click()
    mark.locator(".call-mark-end").click()
    assert_ended(page)
    expect(page.locator(room)).to_have_count(0)
    expect(mark).to_be_hidden()
    assert not subscription_ui["errors"]


@pytest.mark.parametrize("width", [360, 1280])
def test_composer_inserted_text_restores_send(subscription_ui, monkeypatch, width):
    from playwright.sync_api import expect
    page, posts = open_call_ui(subscription_ui, monkeypatch, width=width, scheme="light")
    talk = page.locator("#page-chat [data-call-entry='talk']")
    talk.wait_for()
    page.locator("#page-chat [data-composer-plus]").click()
    page.get_by_role("menuitem", name="Make a button with Ouroboros…").click()
    expect(page.locator("#chat-input")).not_to_have_value("")
    expect(page.locator("#page-chat .chat-send-inline")).to_have_text("Send")
    assert not posts
    page.locator("#chat-input").fill("")
    expect(talk).to_be_visible()
