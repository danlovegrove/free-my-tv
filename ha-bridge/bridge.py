#!/usr/bin/env python3
"""tv-bridge: what the Free My TV home screen may see and do in Home Assistant, and nothing else.

The TV never holds a Home Assistant token: a TV app's files can be read off the TV, the release is public, and a
long-lived token reaches every service, locks included. This small service holds the token (HA_TOKEN, from the
environment) and keeps one WebSocket connection to Home Assistant. The TV talks only to it, on the home network:

    GET  /view      what the home screen shows: the household strip, the player, the action tiles, the doorbell
    GET  /events    the same as Server-Sent Events: "view" on every change, "ding" when the doorbell rings
    POST /call      {"action": "<id>"}: one of the actions in config.yaml, or the player's play/pause and volume
    GET  /snapshot  the doorbell camera's latest image
    GET  /art       the player's cover art

Only the entities named in config.yaml are read, only its actions run, and locks and alarm panels are refused even
when listed. Everything it returns is display text: the TV formats nothing and calls no service by name.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime

import yaml
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

log = logging.getLogger('tv-bridge')

# Never callable from the TV, whatever config.yaml says: anyone in the room can press a TV remote.
FORBIDDEN_DOMAINS = ('lock', 'alarm_control_panel')
# The symbols the TV has (app/assets/symbols); anything else is drawn as 'smart_button'.
ICONS = ('bedtime', 'bell', 'blinds', 'bolt', 'fan', 'home', 'lightbulb', 'live_tv', 'play', 'smart_button', 'speaker',
         'sync', 'toggle', 'tv', 'sunny', 'clear_night', 'rainy', 'cloud')
OPENING_CLASSES = ('door', 'window', 'opening', 'garage_door')
PLAYER_ACTIONS = {
    'player.play_pause': 'media_player.media_play_pause',
    'player.volume_up': 'media_player.volume_up',
    'player.volume_down': 'media_player.volume_down',
    'player.next': 'media_player.media_next_track',
}
DING_SHOWN_S = 60   # how long a ring stays "current" for a TV that comes back to the home screen


class ConfigError(Exception):
    pass


def load_config(path):
    with open(path, encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw)


def parse_config(raw):
    """Checks config.yaml and returns it in one shape: {strip, player, actions, doorbell}."""
    def entity(value, where):
        if not isinstance(value, str) or '.' not in value:
            raise ConfigError('%s: expected an entity id like sensor.bin_day, got %r' % (where, value))
        if value.split('.', 1)[0] in FORBIDDEN_DOMAINS:
            raise ConfigError('%s: %s entities are never offered to the TV' % (where, value.split('.', 1)[0]))
        return value

    def icon(value):
        return value if value in ICONS else 'smart_button'

    strip = []
    for i, item in enumerate(raw.get('strip') or []):
        if len(strip) == 3:
            raise ConfigError('strip: at most 3 items fit the top of the home screen')
        strip.append({'entity': entity(item.get('entity'), 'strip[%d].entity' % i),
                      'label': str(item.get('label') or ''),
                      'icon': icon(item.get('icon')),
                      'states': {str(k): str(v) for k, v in (item.get('states') or {}).items()}})

    player = raw.get('player')
    if player is not None:
        player = entity(player, 'player')
        if not player.startswith('media_player.'):
            raise ConfigError('player: must be a media_player entity')

    actions = []
    for i, item in enumerate(raw.get('actions') or []):
        where = 'actions[%d]' % i
        aid = str(item.get('id') or '')
        if not aid or aid.startswith('player.'):
            raise ConfigError('%s.id: a short name like "film", not starting with "player."' % where)
        service = str(item.get('service') or '')
        if service.count('.') != 1:
            raise ConfigError('%s.service: like scene.turn_on' % where)
        if service.split('.', 1)[0] in FORBIDDEN_DOMAINS:
            raise ConfigError('%s.service: %s services are never offered to the TV' % (where, service.split('.', 1)[0]))
        targets = item.get('entity') or []
        if isinstance(targets, str):
            targets = [targets]
        targets = [entity(t, where + '.entity') for t in targets]
        actions.append({'id': aid, 'label': str(item.get('label') or aid), 'icon': icon(item.get('icon')),
                        'service': service, 'entities': targets, 'data': dict(item.get('data') or {})})
    if len(set(a['id'] for a in actions)) != len(actions):
        raise ConfigError('actions: each id once')

    doorbell = raw.get('doorbell')
    if doorbell:
        doorbell = {'ding': entity(doorbell.get('ding'), 'doorbell.ding'),
                    'camera': entity(doorbell.get('camera'), 'doorbell.camera'),
                    'label': str(doorbell.get('label') or 'Doorbell')}
        if not doorbell['camera'].startswith('camera.'):
            raise ConfigError('doorbell.camera: must be a camera entity')
    return {'strip': strip, 'player': player, 'actions': actions, 'doorbell': doorbell or None}


# MARK: - Display text

def local_zone():
    name = os.environ.get('TZ', '')
    if ZoneInfo and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return None


def parse_when(value, zone):
    """An HA date or date-time string as an aware local datetime (a bare date at midnight), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            d = datetime.strptime(value, '%Y-%m-%d')
            return d.replace(tzinfo=zone) if zone else d
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if d.tzinfo is None:
            return d.replace(tzinfo=zone) if zone else d
        return d.astimezone(zone) if zone else d
    except ValueError:
        return None


def day_words(when, now, with_time):
    """Today 17:00 · Tomorrow · Thu 09:30 · 14 Nov: short, for the strip."""
    days = (when.date() - now.date()).days
    if days == 0:
        day = 'Today'
    elif days == 1:
        day = 'Tomorrow'
    elif 1 < days < 7:
        day = when.strftime('%a')
    else:
        day = '%d %s' % (when.day, when.strftime('%b'))
    return '%s %s' % (day, when.strftime('%H:%M')) if with_time else day


def strip_text(item, st, now, zone):
    """What the strip shows for one entity: a few words."""
    if not st or st.get('state') in (None, 'unavailable', 'unknown'):
        return '—'
    state, attrs = st['state'], st.get('attributes') or {}
    if state in item['states']:
        return item['states'][state]
    domain = item['entity'].split('.', 1)[0]
    if domain == 'binary_sensor':
        cls = attrs.get('device_class')
        if cls in OPENING_CLASSES:
            return 'Open' if state == 'on' else 'Closed'
        if cls in ('motion', 'occupancy', 'presence'):
            return 'Detected' if state == 'on' else 'Clear'
        return 'On' if state == 'on' else 'Off'
    if domain == 'calendar':
        message = attrs.get('message')
        if not message:
            return 'Nothing planned'
        start = parse_when(attrs.get('start_time'), zone)
        if state == 'on':
            return '%s · now' % message
        if start:
            return '%s · %s' % (message, day_words(start, now, not attrs.get('all_day')))
        return message
    if attrs.get('device_class') in ('date', 'timestamp'):
        when = parse_when(state, zone)
        if when:
            return day_words(when, now, attrs.get('device_class') == 'timestamp')
    unit = attrs.get('unit_of_measurement')
    return '%s %s' % (state, unit) if unit else state


# MARK: - The bridge

class Bridge:
    def __init__(self, config, ha_url, token, clock=time.time):
        self.config = config
        self.ha_url = ha_url.rstrip('/')
        self.token = token
        self.clock = clock
        self.zone = local_zone()
        self.states = {}
        self.connected = False
        self.last_ding = 0.0
        self.clients = set()      # asyncio.Queue per open /events stream
        self.ws = None
        self.next_id = 1
        self.pending = {}         # WebSocket message id → Future
        self.session = None
        self.watched = set(i['entity'] for i in config['strip'])
        if config['player']:
            self.watched.add(config['player'])
        if config['doorbell']:
            self.watched.update([config['doorbell']['ding'], config['doorbell']['camera']])

    # What the TV shows

    def view(self):
        now = datetime.fromtimestamp(self.clock(), self.zone) if self.zone else datetime.fromtimestamp(self.clock())
        out = {'connected': self.connected,
               'strip': [{'label': i['label'], 'icon': i['icon'], 'text': strip_text(i, self.states.get(i['entity']), now, self.zone)}
                         for i in self.config['strip']],
               'actions': [{'id': a['id'], 'label': a['label'], 'icon': a['icon']} for a in self.config['actions']],
               'player': self.player_view(),
               'doorbell': None}
        player = out['player']
        if player and player['state'] not in ('unavailable', 'off'):
            # The player's tiles, after the actions: short labels, since a drawer of 12 tiles leaves ~120 px each.
            out['actions'] = out['actions'] + [
                {'id': 'player.play_pause', 'label': 'Pause' if player['state'] == 'playing' else 'Play', 'icon': 'play'},
                {'id': 'player.volume_down', 'label': 'Vol −', 'icon': 'speaker'},
                {'id': 'player.volume_up', 'label': 'Vol +', 'icon': 'speaker'}]
        bell = self.config['doorbell']
        if bell:
            fresh = self.last_ding and self.clock() - self.last_ding < DING_SHOWN_S
            out['doorbell'] = {'label': bell['label'], 'ringing': bool(fresh), 'at': int(self.last_ding * 1000) if fresh else 0}
        return out

    def player_view(self):
        pid = self.config['player']
        if not pid:
            return None
        st = self.states.get(pid) or {}
        attrs = st.get('attributes') or {}
        state = st.get('state') or 'unavailable'
        art = attrs.get('entity_picture') or ''
        volume = attrs.get('volume_level')
        return {'state': state,
                'name': attrs.get('friendly_name') or pid,
                'title': attrs.get('media_title') or '',
                'artist': attrs.get('media_artist') or attrs.get('media_album_name') or '',
                'source': attrs.get('source') or '',
                'volume': round(volume * 100) if isinstance(volume, (int, float)) else None,
                # The picture changes name with the track: the TV reloads /art only when this does.
                'art': hashlib.sha1(art.encode()).hexdigest()[:12] if art else ''}

    # Changes

    def set_state(self, entity_id, new_state):
        """A state from Home Assistant (None: removed). True when the TV's view may have changed."""
        if entity_id not in self.watched:
            return False
        old = self.states.get(entity_id)
        if new_state is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = {'state': new_state.get('state'), 'attributes': new_state.get('attributes') or {}}
        bell = self.config['doorbell']
        if bell and entity_id == bell['ding'] and old is not None and new_state is not None:
            if self.is_ding(entity_id, old, new_state):
                self.ring()
        return True

    @staticmethod
    def is_ding(entity_id, old, new):
        if new.get('state') in ('unavailable', 'unknown', None):
            return False
        if entity_id.startswith('event.'):
            # An event entity's state is the time it last fired: a new one is a ring.
            return new.get('state') != old.get('state')
        return old.get('state') != 'on' and new.get('state') == 'on'

    def ring(self):
        self.last_ding = self.clock()
        log.info('doorbell rang')
        self.broadcast('ding', {'label': self.config['doorbell']['label'], 'at': int(self.last_ding * 1000)})

    def broadcast(self, event, data):
        for q in list(self.clients):
            if q.qsize() < 50:   # a stalled TV doesn't hold memory
                q.put_nowait((event, data))

    def changed(self):
        self.broadcast('view', self.view())

    # Home Assistant

    def service_for(self, action_id):
        """(domain.service, data) for an action the TV may run, or None."""
        pid = self.config['player']
        if action_id in PLAYER_ACTIONS:
            return (PLAYER_ACTIONS[action_id], {'entity_id': pid}) if pid else None
        for a in self.config['actions']:
            if a['id'] == action_id:
                data = dict(a['data'])
                if a['entities']:
                    data['entity_id'] = a['entities']
                return a['service'], data
        return None

    async def call(self, action_id):
        found = self.service_for(action_id)
        if not found:
            raise web.HTTPForbidden(text='not an action this bridge offers')
        service, data = found
        domain, name = service.split('.', 1)
        if domain in FORBIDDEN_DOMAINS:
            raise web.HTTPForbidden(text='never offered')
        if not self.connected:
            raise web.HTTPServiceUnavailable(text='not connected to Home Assistant')
        result = await self.request({'type': 'call_service', 'domain': domain, 'service': name, 'service_data': data})
        if not result.get('success'):
            raise web.HTTPBadGateway(text=(result.get('error') or {}).get('message', 'Home Assistant refused the call'))

    async def request(self, message, timeout=10):
        mid = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        message = dict(message, id=mid)
        try:
            await self.ws.send_json(message)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)

    async def run(self):
        """Keeps one connection to Home Assistant, reconnecting with a growing wait (2 s up to a minute)."""
        wait = 2
        while True:
            try:
                await self.connect_once()
                wait = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, a restart of Home Assistant, a bad token
                log.warning('Home Assistant: %s', e)
            if self.connected:
                self.connected = False
                self.changed()
            await asyncio.sleep(wait)
            wait = min(60, wait * 2)

    async def connect_once(self):
        url = self.ha_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + '/api/websocket'
        async with self.session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
            first = await ws.receive_json()
            if first.get('type') != 'auth_required':
                raise RuntimeError('unexpected greeting %r' % first.get('type'))
            await ws.send_json({'type': 'auth', 'access_token': self.token})
            answer = await ws.receive_json()
            if answer.get('type') != 'auth_ok':
                raise RuntimeError('Home Assistant refused the token')
            self.ws = ws
            reader = asyncio.ensure_future(self.read(ws))
            try:
                states = await self.request({'type': 'get_states'})
                self.states = {}
                for s in states.get('result') or []:
                    if s.get('entity_id') in self.watched:
                        self.states[s['entity_id']] = {'state': s.get('state'), 'attributes': s.get('attributes') or {}}
                await self.request({'type': 'subscribe_events', 'event_type': 'state_changed'})
                self.connected = True
                log.info('connected to Home Assistant, %d of %d entities found', len(self.states), len(self.watched))
                self.changed()
                await reader
            finally:
                reader.cancel()
                self.ws = None
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError('connection closed'))

    async def read(self, ws):
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            for m in data if isinstance(data, list) else [data]:
                if m.get('type') == 'result' and m.get('id') in self.pending:
                    fut = self.pending[m['id']]
                    if not fut.done():
                        fut.set_result(m)
                elif m.get('type') == 'event':
                    ev = (m.get('event') or {}).get('data') or {}
                    if self.set_state(ev.get('entity_id'), ev.get('new_state')):
                        self.changed()

    async def fetch(self, path):
        """A picture from Home Assistant's REST API, with the token. (bytes, content type)."""
        async with self.session.get(self.ha_url + path, headers={'Authorization': 'Bearer ' + self.token},
                                    allow_redirects=False, timeout=ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise web.HTTPBadGateway(text='Home Assistant answered %d' % r.status)
            return await r.read(), r.headers.get('Content-Type', 'image/jpeg')


# MARK: - HTTP for the TV

CORS = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type', 'Cache-Control': 'no-store'}


@web.middleware
async def cors(request, handler):
    if request.method == 'OPTIONS':
        return web.Response(status=204, headers=CORS)
    try:
        response = await handler(request)
    except web.HTTPException as e:
        e.headers.update(CORS)
        raise
    response.headers.update(CORS)
    return response


def make_app(bridge):
    async def view(request):
        return web.json_response(bridge.view())

    async def events(request):
        resp = web.StreamResponse(headers=dict(CORS, **{'Content-Type': 'text/event-stream', 'X-Accel-Buffering': 'no'}))
        await resp.prepare(request)
        q = asyncio.Queue()
        bridge.clients.add(q)
        try:
            await send(resp, 'view', bridge.view())
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), 20)
                    await send(resp, event, data)
                except asyncio.TimeoutError:
                    # A named event, not a comment: the TV's page sees it, and a TV that hears nothing for 50 s
                    # knows the bridge is gone even when no connection error ever arrives (power cut, Wi-Fi drop).
                    await send(resp, 'ping', {'connected': bridge.connected})
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            bridge.clients.discard(q)
        return resp

    async def call(request):
        try:
            body = json.loads(await request.text() or '{}')
        except ValueError:
            raise web.HTTPBadRequest(text='JSON body: {"action": "<id>"}')
        await bridge.call(str(body.get('action') or ''))
        return web.json_response({'ok': True})

    async def snapshot(request):
        bell = bridge.config['doorbell']
        if not bell:
            raise web.HTTPNotFound()
        data, kind = await bridge.fetch('/api/camera_proxy/' + bell['camera'])
        return web.Response(body=data, content_type=kind.split(';')[0])

    async def art(request):
        st = bridge.states.get(bridge.config['player'] or '') or {}
        path = (st.get('attributes') or {}).get('entity_picture') or ''
        if not path.startswith('/'):   # only Home Assistant's own proxy path: never a URL the entity names
            raise web.HTTPNotFound()
        data, kind = await bridge.fetch(path)
        return web.Response(body=data, content_type=kind.split(';')[0])

    async def health(request):
        return web.json_response({'ok': True, 'connected': bridge.connected})

    app = web.Application(middlewares=[cors])
    app.router.add_get('/view', view)
    app.router.add_get('/events', events)
    app.router.add_post('/call', call)
    app.router.add_get('/snapshot', snapshot)
    app.router.add_get('/art', art)
    app.router.add_get('/health', health)
    return app


async def send(resp, event, data):
    await resp.write(('event: %s\ndata: %s\n\n' % (event, json.dumps(data, separators=(',', ':')))).encode())


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ha_url, token = os.environ.get('HA_URL', ''), os.environ.get('HA_TOKEN', '')
    if not ha_url or not token:
        sys.exit('Set HA_URL (http://<home assistant>:8123) and HA_TOKEN (a long-lived token of a non-admin user).')
    try:
        config = load_config(os.environ.get('CONFIG', '/config/config.yaml'))
    except (OSError, ConfigError, yaml.YAMLError) as e:
        sys.exit('config.yaml: %s' % e)
    bridge = Bridge(config, ha_url, token)
    app = make_app(bridge)

    async def start(app):
        bridge.session = ClientSession()
        app['ha'] = asyncio.ensure_future(bridge.run())

    async def stop(app):
        app['ha'].cancel()
        await bridge.session.close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    web.run_app(app, port=int(os.environ.get('PORT', '8124')), access_log=None, shutdown_timeout=2)


if __name__ == '__main__':
    main()
