# tv-bridge: Home Assistant on the Free My TV home screen

A small service on the home network between the TV and Home Assistant. The TV shows a household strip, Home
Assistant tiles and the player in its drawer, and the doorbell camera when someone rings; it never holds a Home
Assistant token. The bridge holds the token, reads only the entities in `config.yaml`, runs only its actions, and
refuses locks and alarm panels even when listed.

```
TV (Free My TV) ──LAN http──▶ tv-bridge (Docker) ──WebSocket + token──▶ Home Assistant
```

## Set up (once)

1. **A Home Assistant user for the TV.** Settings → People → Users → Add user `tv-bridge`, *not* an administrator.
   Log in as it, open its profile → Security → *Long-lived access tokens* → Create. Keep the token for step 3.
2. **A film-mode scene** in Home Assistant (lights down, Sonos to TV audio, blinds closed), e.g. `scene.film_mode`.
3. **On the Docker host:**
   ```bash
   git clone https://github.com/danlovegrove/free-my-tv && cd free-my-tv/ha-bridge
   cp .env.example .env              # HA_URL and HA_TOKEN: edit it here, never commit it
   cp config.example.yaml config.yaml   # your entity ids (Developer tools → States)
   docker compose up -d --build
   curl http://localhost:8124/view   # "connected": true, and the strip's words
   ```
   Give the host a fixed IP (a DHCP reservation) so the TV always finds it.
4. **On the Mac:** TizenBrew Installer → Install from GitHub → `danlovegrove/free-my-tv`.
5. **On the TV:** Home Settings → *Home Assistant* → type the Docker host's IP (port 8124 is assumed) → *Save*. The TV
   checks the bridge answers before saving. The address stays on the TV: it's in no build and no repository.

*Remove* on the same screen turns the strip, the tiles and the doorbell off.

## What the TV gets

| Where | What | From config.yaml |
| --- | --- | --- |
| Top right of the home screen | Up to 3 values: a date ("Tomorrow"), the next calendar event, a door ("Closed") | `strip` |
| Drawer, *Home Assistant* group | One tile per action, then Play/Pause, Vol −, Vol + | `actions`, `player` |
| Drawer, right-hand card | Cover, title, artist, volume while the player plays or is paused | `player` |
| Over the home screen | The camera, refreshed every 2 s for a minute; Back or OK closes it | `doorbell` |

The doorbell shows while Free My TV is in front. Over Netflix and other apps it needs an automation that brings Free
My TV forward first (it pauses what's playing), so that stays a choice: see *Over other apps* below.

## Changing what it shows

Edit `config.yaml`, then `docker compose restart`. The TV picks it up within seconds; no new TV build is needed.

## Over other apps (optional)

A Home Assistant automation can open Free My TV when the doorbell rings, through the Samsung Smart TV integration:

```yaml
alias: Doorbell on the TV
triggers:
  - trigger: state
    entity_id: event.front_door_ding
conditions:
  - condition: state
    entity_id: media_player.samsung_tv
    state: "on"
actions:
  - action: media_player.play_media
    target: { entity_id: media_player.samsung_tv }
    data: { media_content_type: app, media_content_id: FreeMyTVio.FreeMyTV }
```

Free My TV opens with the ring of the last minute and shows the camera. Test it once on this TV before relying on it.

## Endpoints

`GET /view` · `GET /events` (Server-Sent Events: `view`, `ding`, `ping` every 20 s) · `POST /call` `{"action": "<id>"}` ·
`GET /snapshot` · `GET /art` · `GET /health`. Anyone on the home network can call the listed actions, as anyone in the
room can press the remote: list only what's fine for that.

Tests: `python3 -m pytest ha-bridge` (a fake Home Assistant; needs `aiohttp`, `pyyaml`, `pytest`, `pytest-aiohttp`).
