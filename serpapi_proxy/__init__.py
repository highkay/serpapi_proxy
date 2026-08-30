"""SerpApi key pool service.

A minimal, docker-deployable SerpApi multi-key pool: a Bearer-authed admin
API plus a transparent rotating proxy. Self-contained — imports nothing from
the harvester packages (``web/``, ``provider/``, ``tools/``); its Docker
image copies only this directory.
"""