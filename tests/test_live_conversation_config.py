"""Feature configuration reads registered metadata, never credential values."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros.contracts.realtime import CatalogModel, CatalogVoice, ProviderCatalog, RouteFacts
from skills.live_conversation import plugin


class API:
    def __init__(self, root):
        self.root, self.routes, self.sections = root, {}, []
    def get_state_dir(self): return str(self.root / 'feature')
    def get_runtime_info(self): return {'data_dir': str(self.root)}
    def get_settings(self, _keys): raise AssertionError('no secret reads')
    def register_route(self, name, handler, **_kwargs): self.routes[name] = handler
    def register_live_conversation(self, name, handler, **kwargs): self.feature = kwargs
    def register_settings_section(self, *args, **kwargs): self.sections.append((args, kwargs))


@pytest.fixture
def config_api(tmp_path, monkeypatch):
    from ouroboros import extension_registry_state as registry, skill_loader
    catalog = ProviderCatalog('Fixture voice', (CatalogModel('fixture-model', 'Fixture model', default=True, vision=True,
        voices=(CatalogVoice('fixture-voice', 'Fixture voice'),), honours=frozenset({'pause'}),
        facts=RouteFacts(conversation_window_tokens=12345, source='fixture measured'),
        notes=(('search', 'Fixture does not search.'),)),), ('TEST_CREDENTIAL',), 'Fixture audio stays synthetic.')
    skill = SimpleNamespace(name='fixture-engine')
    providers = {'fixture-engine:voice': {'skill':skill.name, 'catalog':catalog}}
    monkeypatch.setattr(registry, '_realtime_providers', providers)
    monkeypatch.setattr(registry, '_extensions', {skill.name: SimpleNamespace(loaded_skill=skill)})
    monkeypatch.setattr(registry, '_unloading', set())
    granted = ['TEST_CREDENTIAL']
    monkeypatch.setattr(skill_loader, 'grant_status_for_skill', lambda _root, _skill: {'granted_keys':list(granted), 'usable':True})
    api = API(tmp_path)
    plugin.register(api)
    return api, granted, providers


def request(api, value=None):
    async def body(): return value
    response = asyncio.run(api.routes['config'](SimpleNamespace(method='GET' if value is None else 'POST', json=body)))
    return response.status_code, json.loads(response.body)


def test_registered_catalogs_and_grants_decide_offer_without_reading_secrets(config_api):
    api, granted, providers = config_api
    status, data = request(api)
    assert status == 200 and data['offered'] is False and data['provider'] == 'gemini_live:gemini'
    assert data['catalogs'][0]['id'] == 'fixture-engine:voice'
    model = data['catalogs'][0]['models'][0]
    assert model['voices'][0]['id'] == 'fixture-voice' and model['facts']['conversation_window_tokens'] == 12345
    value = {'enabled':True, 'provider':'fixture-engine:voice', 'model':'fixture-model', 'voice':'fixture-voice'}
    status, data = request(api, value)
    assert status == 200 and data['offered'] is True and data['selected_model']['vision'] is True
    granted.clear()
    assert request(api)[1]['offered'] is False
    granted.append('TEST_CREDENTIAL')
    assert request(api)[1]['offered'] is True
    providers.clear()
    assert request(api)[1]['offered'] is False
    assert request(api, {**value, 'enabled':False})[0] == 200


def test_unknown_catalog_selections_are_refused_and_call_tools_registered(config_api):
    api, _, _ = config_api
    for fields in ({'model':'invented'}, {'voice':'invented'}, {'provider':'missing:engine'}):
        status, data = request(api, {'enabled':True, 'provider':'fixture-engine:voice', **fields})
        assert status == 400 and data['error']
    assert {tool.name for tool in api.feature['call_tools']} == {'end_call','set_call_languages'}
    assert all(tool.silent for tool in api.feature['call_tools'])
