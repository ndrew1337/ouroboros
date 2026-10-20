"""Qualify the integration-secret form in the served SPA without a model call.

The page is the real interface (static ``web/``). Its question card arrives as the frame the
real ``LocalChatBridge.send_quiz`` broadcasts for a ``showSecretHitl`` request; its OK posts
``saveSecretToken`` to the real ``POST /api/decisions/secret`` over a real task store and a
real Settings file; its lifecycle frames are the ones that endpoint broadcasts; a reload reads
the real chat-history endpoint. The test types a canary that matches no token pattern and
proves it reached ``settings.json`` and nothing a browser, a response or the data root keeps.
Screenshots of the input, saving, error and saved states go to ``OUROBOROS_UI_EVIDENCE_DIR``.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import owner_quiz
from ouroboros import secret_requests as sr
from ouroboros.task_results import write_task_result
from tests.test_secret_hitl import TASK, _write_skill
from tests.test_subscription_setup_browser import capture, subscription_ui as subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
CANARY = "zq7-CANARY-Ωlj3~p0+kx/2 browser"
STORAGE = "() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])"


@pytest.fixture(params=["chromium", "webkit"])
def engine_ui(request, monkeypatch):
    monkeypatch.setenv("OUROBOROS_UI_BROWSER_ENGINE", request.param)
    root = os.environ.get("OUROBOROS_UI_EVIDENCE_DIR")
    if root:
        monkeypatch.setenv("OUROBOROS_UI_EVIDENCE_DIR", str(Path(root) / request.param))
    return request.getfixturevalue("subscription_ui")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.gateway import secret_decision as sd
    from ouroboros.gateway import task_decision as td
    from ouroboros.gateway.history import make_chat_history_endpoint
    import supervisor.message_bus as mb
    import supervisor.queue as q

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", data / "settings.json")
    monkeypatch.setattr(cfg, "get_skills_repo_path", lambda: "")
    _write_skill(data)
    write_task_result(data, TASK, "running", chat_id=1)
    frames = []
    bridge = mb.LocalChatBridge({})
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(mb, "DATA_DIR", data)
    monkeypatch.setattr(mb, "load_state", lambda: {"owner_id": 1})
    monkeypatch.setattr(mb, "publish_event", lambda *args: None)
    monkeypatch.setattr(mb, "get_bridge", lambda: bridge)
    monkeypatch.setattr(td, "_live_root_task", lambda task_id: ({"id": task_id}, ""))
    monkeypatch.setattr(q, "_task_drive_for_task", lambda task, task_id: data, raising=False)
    app = Starlette(routes=[Route("/api/decisions/secret", endpoint=sd.api_secret_decision, methods=["POST"]),
                            Route("/api/chat/history", endpoint=make_chat_history_endpoint(data))])
    app.state.drive_root = data

    def ask():
        request = sr.resolve_secret_request(data, "fakeplug", ["botToken"])
        quiz_id = uuid.uuid4().hex
        owner_quiz.record_asked(data, TASK, quiz_id=quiz_id, question=sr.question_text(request), options=[],
                                wait_for_answer=True, chat_id=1, secret_request=request)
        before = len(frames)
        ok, _ = bridge.send_quiz(1, quiz_id=quiz_id, question=sr.question_text(request), options=[],
                                 task_id=TASK, wait_for_answer=True)
        assert ok
        [frame] = [frame for frame in frames[before:] if frame.get("type") == "quiz"]
        return quiz_id, frame

    return type("Backend", (), {"data": data, "frames": frames, "client": TestClient(app), "ask": staticmethod(ask)})


def _open(ui, backend):
    page = ui["page"]
    sockets, seen, held = [], [], []
    console = []
    page.on("console", lambda message: console.append(message.text))

    def secret_route(route):
        if held is not None and backend.hold:
            held.append(route)
            return
        _forward(route)

    def _forward(route):
        response = backend.client.post("/api/decisions/secret", content=route.request.post_data,
                                       headers={"Content-Type": "application/json"})
        seen.append(response.text)
        route.fulfill(status=response.status_code, content_type="application/json", body=response.text)

    def history_route(route):
        url = route.request.url
        response = backend.client.get(url[url.index("/api/chat/history"):])
        seen.append(response.text)
        route.fulfill(status=response.status_code, content_type="application/json", body=response.text)

    def socket_route(ws):
        sockets.append(ws)
        ws.send(json.dumps({"type": "heartbeat"}))

    backend.hold = False
    page.route("**/api/state", lambda route: route.fulfill(content_type="application/json", body=json.dumps(
        {"supervisor_ready": True, "active_chat_activities": [], "projects": [], "sha": "secret-hitl"})))
    page.route("**/api/chat/history*", history_route)
    page.route("**/api/decisions/secret", secret_route)
    page.route_web_socket("**/ws", socket_route)
    page.goto(ui["url"])
    page.wait_for_selector("#chat-input")
    push = lambda frame: sockets[-1].send(json.dumps(frame))  # noqa: E731 - the live socket of this load
    return page, push, seen, held, _forward, console


def _settings(backend):
    path = backend.data / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("viewport,field_count", [
    ({"width": 1512, "height": 949}, 1),
    ({"width": 390, "height": 460}, 8),
    ({"width": 1000, "height": 320}, 8),
])
def test_secret_form_is_centered_and_short_viewports_keep_actions_visible(engine_ui, backend, viewport, field_count):
    page, push, *_ = _open(engine_ui, backend)
    page.set_viewport_size(viewport)
    quiz_id, frame = backend.ask()
    field = frame["secret_request"]["fields"][0]
    frame["secret_request"]["fields"] = [dict(field, id=f"token{index}") for index in range(field_count)]
    push(frame)
    page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"] .chat-secret-open').click()
    dialog = page.locator(".secret-request-dialog")
    dialog.wait_for(state="visible")
    box = dialog.bounding_box()
    assert abs(box["x"] + box["width"] / 2 - viewport["width"] / 2) <= 1
    assert abs(box["y"] + box["height"] / 2 - viewport["height"] / 2) <= 1
    assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"]
    for selector in (".marketplace-modal-head", ".marketplace-modal-actions"):
        region = dialog.locator(selector).bounding_box()
        assert region["y"] >= 0 and region["y"] + region["height"] <= viewport["height"]
    body = dialog.locator(".secret-request-body")
    if field_count > 1:
        assert body.evaluate("node => node.scrollHeight > node.clientHeight")
        dialog.locator("input.secret-request-input").last.scroll_into_view_if_needed()
        assert dialog.locator("input.secret-request-input").last.is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    capture(page, f"secret-centered-{viewport['width']}-{viewport['height']}")
    dialog.locator(".secret-request-close").click()
    assert owner_quiz.quiz_states(backend.data, TASK)[quiz_id]["state"] == "open"
    assert not _settings(backend)


@pytest.mark.parametrize("keyboard", [False, True])
def test_secret_form_safe_overflow_and_keyboard_alignment(engine_ui, backend, keyboard):
    page, push, *_ = _open(engine_ui, backend)
    page.set_viewport_size({"width": 390, "height": 664 if keyboard else 260})
    quiz_id, frame = backend.ask()
    field = frame["secret_request"]["fields"][0]
    frame["secret_request"]["fields"] = [dict(field, id=f"token{index}") for index in range(3)]
    push(frame)
    page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"] .chat-secret-open').click()
    dialog = page.locator(".secret-request-dialog")
    dialog.wait_for(state="visible")
    if keyboard:
        # Exercise the app-owned state at CSS's boundary; desktop Playwright does
        # not prove a physical phone's soft-keyboard detection or geometry.
        page.evaluate("document.body.classList.add('keyboard-open')")
    assert dialog.bounding_box()["y"] >= 0
    assert dialog.locator(".marketplace-modal-head").bounding_box()["y"] >= 0
    if keyboard:
        assert dialog.bounding_box()["y"] < 40
        page.evaluate("document.body.classList.remove('keyboard-open')")
    else:
        dialog.locator(".secret-request-ok").scroll_into_view_if_needed()
        box = dialog.locator(".secret-request-ok").bounding_box()
        assert 0 <= box["y"] and box["y"] + box["height"] <= 260
        page.locator(".secret-request-backdrop").evaluate("node => node.scrollTop = 0")
        assert dialog.locator(".marketplace-modal-head").bounding_box()["y"] >= 0
    capture(page, f"secret-safe-{'keyboard' if keyboard else 'overflow'}")


def test_secret_form_saves_through_the_real_endpoint_and_keeps_no_copy(engine_ui, backend, monkeypatch):
    from ouroboros.gateway import owner_settings

    ui = engine_ui
    page, push, seen, held, forward, console = _open(ui, backend)
    quiz_id, frame = backend.ask()
    push(frame)

    dialog = page.locator(".secret-request-dialog")
    card = page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"]')
    card.locator(".chat-secret-open").wait_for()
    assert dialog.count() == 0, 'a live arrival must not open the form'
    capture(page, "secret-request-button")
    card.locator(".chat-secret-open").click()
    dialog.wait_for(state="visible")
    assert "fakeplug: secret required" in dialog.locator(".secret-request-title").inner_text()
    # A translucent theme sheet exposed the chat's glyphs through this protected form in WebKit.
    # Ask the real computed style, not a source-text match that ignores specificity.
    background = dialog.evaluate("node => getComputedStyle(node).backgroundColor")
    assert background.startswith("rgb("), f"secret form must have an opaque background: {background}"
    field = dialog.locator("input.secret-request-input")
    assert field.get_attribute("type") == "password"
    assert dialog.locator(".secret-request-label").inner_text() == "botToken"
    assert "FAKEPLUG_TOKEN" in dialog.locator(".ui-field-help").inner_text()
    ok, cancel = dialog.locator(".secret-request-ok"), dialog.locator(".secret-request-cancel")
    assert ok.inner_text() == "OK" and cancel.inner_text() == "Close" and ok.is_disabled()
    assert page.evaluate("document.activeElement.classList.contains('secret-request-input')")
    field.fill(CANARY)
    assert ok.is_enabled()
    capture(page, "secret-form-input")

    # Error state: Settings refuse once; nothing is saved and the entry stays for an explicit retry.
    real_update, calls = owner_settings._owner_update_settings, []

    def locked_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise owner_settings.SettingsLockUnavailable("another process holds the settings lock")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(owner_settings, "_owner_update_settings", locked_once)
    ok.click()
    error = dialog.locator(".secret-request-error")
    error.wait_for(state="visible")
    assert "nothing was saved" in error.inner_text()
    assert field.input_value() == CANARY and not _settings(backend)
    capture(page, "secret-form-error")

    # Saving state: the request is held until the screenshot is taken.
    backend.hold = True
    ok.click()
    page.wait_for_function("document.querySelector('.secret-request-ok')?.textContent === 'Saving…'")
    # A save in flight is not the owner's move: the head's mark leaves amber (DESIGN "Status marks").
    assert card.locator(".chat-quiz-mark").get_attribute("data-hue") == "neutral"
    capture(page, "secret-form-saving")
    backend.hold = False
    [route] = held
    forward(route)
    dialog.wait_for(state="detached")
    for live in [item for item in backend.frames if item.get("type") == "quiz_state"]:
        push(live)
    card = page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"]')
    card.locator(".chat-secret-status").filter(has_text="Saved to Settings: botToken").wait_for()
    assert card.locator(".chat-quiz-comment").count() == 0
    capture(page, "secret-form-saved")
    assert _settings(backend)["FAKEPLUG_TOKEN"] == CANARY

    page.reload()
    page.wait_for_selector("#chat-input")
    page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"] .chat-secret-status').filter(
        has_text="Saved to Settings").wait_for()
    texts = [page.content(), page.evaluate(STORAGE), "\n".join(console), *seen]
    for text in texts:
        assert CANARY not in text and json.dumps(CANARY)[1:-1] not in text
    for path in backend.data.rglob("*"):
        if path.is_file() and not path.name.startswith("settings.json"):
            assert CANARY.encode() not in path.read_bytes(), path


def test_a_pending_request_reopens_empty_after_reload_and_cancel_saves_nothing(engine_ui, backend):
    ui = engine_ui
    page, push, seen, _held, _forward, _console = _open(ui, backend)
    quiz_id, frame = backend.ask()
    push(frame)
    dialog = page.locator(".secret-request-dialog")
    card = page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"]')
    open_button = card.locator(".chat-secret-open")
    open_button.wait_for()
    assert dialog.count() == 0
    open_button.click()
    dialog.wait_for(state="visible")
    dialog.locator("input.secret-request-input").fill(CANARY)
    dialog.locator(".secret-request-cancel").click()  # footer Close is dismissal, not cancellation
    dialog.wait_for(state="detached")
    assert owner_quiz.quiz_states(backend.data, TASK)[quiz_id]["state"] == "open"
    assert open_button.is_enabled()
    capture(page, "secret-request-dismissed")
    open_button.click()
    assert dialog.locator("input.secret-request-input").input_value() == ""
    page.locator(".secret-request-backdrop").click(position={"x": 2, "y": 2})
    dialog.wait_for(state="detached")
    assert open_button.is_enabled()
    open_button.click()
    dialog.locator("input.secret-request-input").fill(CANARY)
    page.keyboard.press("Escape")  # hides the form; the request stays open
    dialog.wait_for(state="detached")

    page.reload()
    page.wait_for_selector("#chat-input")
    card = page.locator(f'.chat-quiz-card[data-quiz-id="{quiz_id}"]')
    open_button = card.locator(".chat-secret-open")
    open_button.wait_for()
    assert page.locator(".secret-request-dialog").count() == 0  # a replay never opens it by itself
    open_button.click()
    dialog.wait_for(state="visible")
    assert dialog.locator("input.secret-request-input").input_value() == ""
    dialog.locator(".secret-request-close").click()
    dialog.wait_for(state="detached")
    card.locator(".chat-secret-cancel").click()
    dialog.wait_for(state="detached")
    card.locator(".chat-quiz-closed-reason").filter(has_text="nothing was saved").wait_for()
    assert "FAKEPLUG_TOKEN" not in _settings(backend)
    assert owner_quiz.quiz_states(backend.data, TASK)[quiz_id]["secret_outcome"]["event"] == "secret_cancelled"
    for text in [page.content(), page.evaluate(STORAGE), *seen]:
        assert CANARY not in text
