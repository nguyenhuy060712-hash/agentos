# GĐ11.45 aos-egress: the egress proxy of the virtual computers (stdlib only). Base pinned by digest (python:3.12-alpine as pulled 2026-10-26); bump on purpose, never :latest.
#   docker build -t aos-egress:20261026 sandbox/egress        (app/egress_ctl.py builds it by itself when the image is missing)
FROM python:3.12-alpine@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d
COPY proxy.py /opt/aos-egress/proxy.py
USER 65534:65534
EXPOSE 3128
ENTRYPOINT ["python", "-u", "/opt/aos-egress/proxy.py"]
