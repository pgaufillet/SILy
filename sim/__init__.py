"""Host-only simulation of the mesh: never deployed to boards.

Drives mesh cores (`mesh.core`) in simulated time on a scripted medium:
deterministic random generator (`prng`), discrete-event scheduler
(`scheduler`), platform fakes (`platform`), scripted medium (`medium`) and
the host runner binding a core to a radio (`node`). Runs on the MicroPython
unix port; a run is reproducible from its seed.

Depends on ``mesh`` only; ``mesh`` never imports it (tests/test_layering.py).
It lives outside ``src/`` so that ``make deploy`` does not copy it, and goes
with ``mesh`` if the mesh becomes a project of its own.
"""
