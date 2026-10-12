FROM ghcr.io/mvladislav/bumper:0.4.1
COPY patch_legacy_ctl.py /tmp/patch_legacy_ctl.py
RUN /bumper/.venv/bin/python /tmp/patch_legacy_ctl.py && rm /tmp/patch_legacy_ctl.py
