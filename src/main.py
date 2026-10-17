# Run by MicroPython at boot, after boot.py. Must return quickly so that the
# REPL stays available.
import sily

print("SILy", sily.__version__)
