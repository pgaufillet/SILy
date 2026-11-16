# Run by MicroPython at boot, after boot.py: starts the application
# (sily.app). Returns at once, leaving the REPL free, without a valid
# /config.json or after repeated crashes (safe mode: the LED blinks fast);
# Ctrl-C stops the application and leaves the board at the REPL.
from sily import app

app.boot()
