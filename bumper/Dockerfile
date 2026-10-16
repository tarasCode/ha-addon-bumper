FROM ghcr.io/mvladislav/bumper:0.4.1
COPY patch_legacy_ctl.py /tmp/patch_legacy_ctl.py
RUN /bumper/.venv/bin/python /tmp/patch_legacy_ctl.py && rm /tmp/patch_legacy_ctl.py
COPY deebot_map.py /deebot_map.py
COPY repair_db.py /repair_db.py
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
