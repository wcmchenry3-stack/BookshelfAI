"""AI gateway: the only place the backend calls a paid AI model.

Routers and services ask for a *feature* (``scan.standard``, ``scan.enhanced``)
through :func:`app.ai.gateway.run_feature`. The server-controlled registry in
:mod:`app.ai.registry` decides the provider, model, token cap and timeout;
nothing the client sends can change them. Provider code lives only in
:mod:`app.ai.providers`, which a security test enforces.
"""
