# Bumper add-on for Home Assistant

Runs the official [MVladislav/bumper](https://github.com/MVladislav/bumper) image (pinned to 0.4.1) as a
Home Assistant add-on, so Ecovacs Deebot robots can be controlled locally with the core `ecovacs`
integration in `self_hosted` mode. No secrets or house data are stored here.

Data and certificates live in the add-on config folder. Port 1883 is left unpublished on purpose.
