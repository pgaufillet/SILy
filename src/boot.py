# Run by MicroPython at every boot and soft reset, before main.py (which a
# soft reset from the raw REPL, as host tools make, does not run). The
# hardware watchdog the application arms survives a soft reset, the timer
# that fed it at the REPL does not: sily.watchdog feeds it again. Never
# raises: a failing boot.py would keep main.py from starting the
# application.
try:
    from sily import watchdog

    watchdog.at_boot()
except Exception as e:
    print("SILY boot.py:", repr(e))
