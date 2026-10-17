"""Application-agnostic LoRa mesh.

Isolation rule: this package imports nothing from the SILy application
(``sily``), nor from board support (``board``), nor ``machine``. The radio and
the platform (clock, persistent storage, memory kept across resets, random
source) are injected, so that the mesh runs on the MicroPython unix port and
can be extracted as a project of its own.
"""
