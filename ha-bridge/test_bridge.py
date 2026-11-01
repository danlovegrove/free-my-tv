"""Tests for tv-bridge against a fake Home Assistant (its WebSocket and REST API). Run: python3 -m pytest ha-bridge"""
import asyncio
import json
import os
import sys

import pytest
from aiohttp import ClientSession, web

sys.path.insert(0, os.path.dirname(__file__))
import bridge as B  # noqa: E402

os.environ['TZ'] = 'Europe/London'

CONFIG = {
    'strip': [
        {'entity': 'sensor.bin_day', 'label': 'Bins', 'icon': 'home'},
        {'entity': 'calendar.family', 'label': 'Next', 'icon': 'bell'},
        {'entity': 'binary_sensor.front_door', 'label': 'Front door', 'icon': 'nonexistent'},
    ],
    'player': 'media_player.living_room',
    'actions': [{'id': 'film', 'label': 'Film mode', 'icon': 'bedtime', 'service': 'scene.turn_on', 'entity': 'scene.film_mode'}],
    'doorbell': {'ding': 'event.front_door_ding', 'camera': 'camera.front_door', 'label': 'Front door'},
}

# 2026-11-02 18:30 London
NOW = 1793644200.0

STATES = [
    {'entity_id': 'sensor.bin_day', 'state': '2026-11-03', 'attributes': {'device_class': 'date'}},
    {'entity_id': 'calendar.family', 'state': 'off',
     'attributes': {'message': 'Swimming', 'start_time': '2026-11-05 17:00:00', 'all_day': False}},
    {'entity_id': 'binary_sensor.front_door', 'state': 'off', 'attributes': {'device_class': 'door'}},
    {'entity_id': 'media_player.living_room', 'state': 'playing',
     'attributes': {'friendly_name': 'Living Room', 'media_title': 'Teardrop', 'media_artist': 'Massive Attack',
                    'volume_level': 0.24, 'entity_picture': '/api/media_player_proxy/media_player.living_room?token=x'}},
    {'entity_id': 'event.front_door_ding', 'state': '2026-11-01T10:00:00+00:00', 'attributes': {}},
    {'entity_id': 'camera.front_door', 'state': 'idle', 'attributes': {}},
    {'entity_id': 'lock.front_door', 'state': 'locked', 'attributes': {}},
]


class FakeHA:
    """Just enough of Home Assistant: auth, get_states, subscribe_events, call_service, camera and art proxies."""

    def __init__(self, token='good'):
        self.token = token
        self.calls = []
        self.sockets = []

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        auth = await ws.receive_json()
        if auth.get('access_token') != self.token:
            await ws.send_json({'type': 'auth_invalid'})
            await ws.close()
            return ws
        await ws.send_json({'type': 'auth_ok'})
        self.sockets.append(ws)
        async for msg in ws:
            m = json.loads(msg.data)
            if m['type'] == 'get_states':
                await ws.send_json({'id': m['id'], 'type': 'result', 'success': True, 'result': STATES})
            elif m['type'] == 'subscribe_events':
                await ws.send_json({'id': m['id'], 'type': 'result', 'success': True, 'result': None})
            elif m['type'] == 'call_service':
                self.calls.append((m['domain'], m['service'], m['service_data']))
                await ws.send_json({'id': m['id'], 'type': 'result', 'success': True, 'result': {}})
        return ws

    async def push(self, entity_id, state, attributes=None):
        for ws in self.sockets:
            await ws.send_json({'type': 'event', 'event': {'event_type': 'state_changed', 'data': {
                'entity_id': entity_id, 'new_state': {'entity_id': entity_id, 'state': state, 'attributes': attributes or {}}}}})

    async def picture(self, request):
        if request.headers.get('Authorization') != 'Bearer ' + self.token:
            return web.Response(status=401)
        return web.Response(body=b'JPEG' + request.path.encode(), content_type='image/jpeg')

    def app(self):
        app = web.Application()
        app.router.add_get('/api/websocket', self.websocket)
        app.router.add_get('/api/camera_proxy/{entity}', self.picture)
        app.router.add_get('/api/media_player_proxy/{entity}', self.picture)
        return app


async def wait_for(cond, timeout=3):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError('timed out')
        await asyncio.sleep(0.02)


@pytest.fixture
async def setup(aiohttp_client, aiohttp_server):
    ha = FakeHA()
    server = await aiohttp_server(ha.app())
    bridge = B.Bridge(B.parse_config(CONFIG), str(server.make_url('')), 'good', clock=lambda: NOW)
    bridge.session = ClientSession()
    task = asyncio.ensure_future(bridge.run())
    client = await aiohttp_client(B.make_app(bridge))
    await wait_for(lambda: bridge.connected)
    yield ha, bridge, client
    task.cancel()
    await bridge.session.close()


# MARK: - Config

def test_config_refuses_locks_and_alarms():
    with pytest.raises(B.ConfigError):
        B.parse_config({'actions': [{'id': 'x', 'service': 'lock.unlock', 'entity': 'lock.front_door'}]})
    with pytest.raises(B.ConfigError):
        B.parse_config({'actions': [{'id': 'x', 'service': 'script.turn_on', 'entity': 'alarm_control_panel.home'}]})
    with pytest.raises(B.ConfigError):
        B.parse_config({'strip': [{'entity': 'lock.front_door'}]})


def test_config_limits():
    with pytest.raises(B.ConfigError):
        B.parse_config({'strip': [{'entity': 'sensor.a'}] * 4})
    with pytest.raises(B.ConfigError):
        B.parse_config({'player': 'light.lamp'})
    with pytest.raises(B.ConfigError):
        B.parse_config({'actions': [{'id': 'player.x', 'service': 'scene.turn_on'}]})
    assert B.parse_config(CONFIG)['strip'][2]['icon'] == 'smart_button'


# MARK: - What the TV sees

async def test_view(setup):
    ha, bridge, client = setup
    r = await client.get('/view')
    assert r.headers['Access-Control-Allow-Origin'] == '*'
    v = await r.json()
    assert v['connected'] is True
    assert [s['text'] for s in v['strip']] == ['Tomorrow', 'Swimming · Thu 17:00', 'Closed']
    assert v['player']['title'] == 'Teardrop' and v['player']['artist'] == 'Massive Attack' and v['player']['volume'] == 24
    assert [(a['id'], a['label']) for a in v['actions']] == [
        ('film', 'Film mode'), ('player.play_pause', 'Pause'), ('player.volume_down', 'Vol −'), ('player.volume_up', 'Vol +')]
    assert v['doorbell'] == {'label': 'Front door', 'ringing': False, 'at': 0}
    assert 'lock.front_door' not in bridge.states   # never read: not in config.yaml


async def test_live_changes_and_ding(setup):
    ha, bridge, client = setup
    r = await client.get('/events')
    first = await r.content.readuntil(b'\n\n')
    assert first.startswith(b'event: view\n')
    await ha.push('binary_sensor.front_door', 'on', {'device_class': 'door'})
    msg = await r.content.readuntil(b'\n\n')
    assert b'event: view' in msg and b'"Open"' in msg
    await ha.push('event.front_door_ding', '2026-11-02T18:30:00+00:00')
    seen = b''
    while b'event: ding' not in seen:
        seen += await r.content.readuntil(b'\n\n')
    assert bridge.view()['doorbell']['ringing'] is True
    r.close()


async def test_binary_sensor_ding_only_on_rising_edge(setup):
    ha, bridge, client = setup
    bridge.config['doorbell']['ding'] = 'binary_sensor.ding'
    bridge.watched.add('binary_sensor.ding')
    bridge.set_state('binary_sensor.ding', {'state': 'off'})
    bridge.set_state('binary_sensor.ding', {'state': 'off'})
    assert bridge.last_ding == 0
    bridge.set_state('binary_sensor.ding', {'state': 'on'})
    assert bridge.last_ding == NOW


# MARK: - What the TV can do

async def test_calls(setup):
    ha, bridge, client = setup
    assert (await client.post('/call', data=json.dumps({'action': 'film'}))).status == 200
    assert (await client.post('/call', data=json.dumps({'action': 'player.volume_up'}))).status == 200
    assert ha.calls == [('scene', 'turn_on', {'entity_id': ['scene.film_mode']}),
                        ('media_player', 'volume_up', {'entity_id': 'media_player.living_room'})]


async def test_refuses_everything_else(setup):
    ha, bridge, client = setup
    for action in ('lock.unlock', 'unlock', '', 'scene.turn_on'):
        assert (await client.post('/call', data=json.dumps({'action': action}))).status == 403
    assert (await client.post('/call', data='not json')).status == 400
    assert ha.calls == []


async def test_pictures_use_the_token(setup):
    ha, bridge, client = setup
    snap = await client.get('/snapshot')
    assert snap.status == 200 and (await snap.read()).startswith(b'JPEG/api/camera_proxy/camera.front_door')
    art = await client.get('/art')
    assert art.status == 200 and b'media_player_proxy' in await art.read()


async def test_art_never_follows_a_url_from_the_entity(setup):
    ha, bridge, client = setup
    bridge.states['media_player.living_room']['attributes']['entity_picture'] = 'https://evil.example/x.jpg'
    assert (await client.get('/art')).status == 404


async def test_preflight(setup):
    ha, bridge, client = setup
    r = await client.options('/call')
    assert r.status == 204 and 'POST' in r.headers['Access-Control-Allow-Methods']


async def test_bad_token_stays_disconnected(aiohttp_server):
    ha = FakeHA()
    server = await aiohttp_server(ha.app())
    bridge = B.Bridge(B.parse_config(CONFIG), str(server.make_url('')), 'wrong', clock=lambda: NOW)
    bridge.session = ClientSession()
    with pytest.raises(RuntimeError):
        await bridge.connect_once()
    assert bridge.connected is False
    await bridge.session.close()


# MARK: - Display text

def test_strip_text_edges():
    zone = B.local_zone()
    from datetime import datetime
    now = datetime.fromtimestamp(NOW, zone)
    item = {'entity': 'sensor.temp', 'states': {}}
    assert B.strip_text(item, {'state': '19.5', 'attributes': {'unit_of_measurement': '°C'}}, now, zone) == '19.5 °C'
    assert B.strip_text(item, {'state': 'unavailable'}, now, zone) == '—'
    assert B.strip_text(item, None, now, zone) == '—'
    mapped = {'entity': 'sensor.bins', 'states': {'1': 'Tomorrow (recycling)'}}
    assert B.strip_text(mapped, {'state': '1', 'attributes': {}}, now, zone) == 'Tomorrow (recycling)'
    cal = {'entity': 'calendar.x', 'states': {}}
    assert B.strip_text(cal, {'state': 'on', 'attributes': {'message': 'Dinner'}}, now, zone) == 'Dinner · now'
    assert B.strip_text(cal, {'state': 'off', 'attributes': {}}, now, zone) == 'Nothing planned'
