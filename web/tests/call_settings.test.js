import './fixtures/call_modules.js';
import test from 'node:test';
import assert from 'node:assert/strict';
const { callSettingsHtml } = await import('../../skills/live_conversation/ui/call_settings.js');

const catalog = { id:'fixture:voice', label:'Fixture engine', privacy:'Synthetic audio stays here.', models:[
    {id:'first', label:'First model', default:true, vision:false, voices:[{id:'quiet',label:'Quiet'}], honours:['pause'],
        notes:[['thinking','This model has no thinking setting.'],['search','This model cannot search.']],
        facts:{conversation_window_tokens:12345,source:'Measured synthetic route.'}},
    {id:'second',label:'Second model',voices:[{id:'bright',label:'Bright'}],honours:['thinking','search','interruptions'],
        thinking_levels:['small','large'],facts:{conversation_window_tokens:23456,source:'Not yet measured.'}},
] };

test('settings choices, hidden knobs and notes come from the selected catalog', () => {
    const config = { provider:catalog.id, catalogs:[catalog], enabled:true, model:'', voice:'quiet' };
    const first = callSettingsHtml(config);
    assert.match(first, /Fixture engine/);
    assert.match(first, /First model/);
    assert.match(first, /Quiet/);
    assert.match(first, /name="pause"/);
    assert.doesNotMatch(first, /name="thinking"|name="search"/);
    assert.match(first, /This model has no thinking setting/);
    assert.match(first, /Synthetic audio stays here/);
    assert.match(first, /12,345/);
    const second = callSettingsHtml({...config,model:'second'});
    assert.match(second, /name="thinking"/);
    assert.match(second, /value="large"/);
    assert.match(second, /Bright/);
    assert.doesNotMatch(second, /name="pause"/);
    assert.match(second, /Not yet measured/);
    assert.doesNotMatch(second, /Gemini|Grok|OpenAI/);
});

test('an unavailable selected engine is named rather than silently replaced', () => {
    const html = callSettingsHtml({provider:'missing:voice',catalogs:[catalog],model:'lost-model',voice:'lost-voice'});
    assert.match(html, /missing:voice/);
    assert.match(html, /not available/);
    assert.match(html, /value="missing:voice" selected/);
});
