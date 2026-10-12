# Bumper add-on for Home Assistant

Runs the official [MVladislav/bumper](https://github.com/MVladislav/bumper) image (pinned to 0.4.1) as a
Home Assistant add-on, so Ecovacs Deebot robots can be controlled locally with the core `ecovacs`
integration in `self_hosted` mode. No secrets or house data are stored here.

Data and certificates live in the add-on config folder. Port 1883 is left unpublished on purpose.

## Patch for legacy XMPP robots (OZMO 930 and similar)

The add-on builds on the upstream image and applies `bumper/patch_legacy_ctl.py`: Bumper 0.4.1
relays commands from Home Assistant's `ecovacs` integration (sucks) without the `id` attribute the
Ecovacs cloud adds, so the robot acks but sends no data, and relays replies without the `td` name
sucks needs. The patch adds both. The build fails if upstream code changes under the patch.

## Map of a legacy XMPP robot

Set the option `map_bot` to the robot's XMPP address (`<did>@<class>.ecorobot.net/atom`) and the
add-on draws the map stored in the robot into `/share/deebot/mappa.png` (`deebot_map.py`,
standard library only). While the robot cleans, its position and the path it covered are
redrawn every 5 seconds; otherwise every 30 seconds. The PNG is flipped top-down and carries a tEXt chunk "vallenza" with the calibration (pixel <-> mm) and the virtual walls, read by the HA card. Show it in HA
with a Local File camera pointing at that path. Empty option = no map, nothing runs.
