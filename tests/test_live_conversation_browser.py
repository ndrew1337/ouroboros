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

FAKES = r"""(() => {
    window.__callFixture = { microphones: 0, stopped: 0, sent: [], sockets: [] };
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
            return { getTracks: () => [track], getAudioTracks: () => [track], getVideoTracks: () => [] };
        },
        async enumerateDevices() { return []; },
        addEventListener() {}, removeEventListener() {},
    };
    Object.defineProperty(navigator, 'mediaDevices', { value: media, configurable: true });
    class Context {
        constructor() { this.sampleRate = 24000; this.destination = {}; this.state = 'running'; }
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


def open_call_ui(ui, monkeypatch, *, width, scheme, engine="openai", enabled=True):
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

    def extension(route):
        path = urlparse(route.request.url).path
        if path.startswith(ASSETS):
            relative = path.removeprefix(ASSETS)
            source = ROOT / "skills/live_conversation/ui" / relative
            assert source.is_relative_to(ROOT / "skills/live_conversation/ui") and source.is_file()
            route.fulfill(content_type="text/css" if source.suffix == ".css" else "text/javascript",
                          body=source.read_text())
        elif path == "/api/extensions/live_conversation/config":
            route.fulfill(content_type="application/json", body=json.dumps({
                "enabled": enabled, "provider": engine, "model": "synthetic-only", "voice": ""}))
        else:
            route.fallback()
    page.route("**/api/extensions/live_conversation/**", extension)

    def admit(route):
        posts.append(route.request.post_data_json)
        route.fulfill(content_type="application/json", body=json.dumps({
            "call_id": CALL_ID, "ws_path": "/ws/call/" + CALL_ID, "vision": engine != "grok"}))
    page.route("**/api/call/open", admit)
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
        page.locator(".call-keyboard").click()
        draft = "Keep this draft: Обычный nano, tools & memory.\nA second line that must survive End."
        field.fill(draft)
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
