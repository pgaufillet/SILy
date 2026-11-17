"""SILy application: SportIdent punches carried over a LoRa mesh.

Uses the ``mesh`` and ``board`` packages; neither imports this one.
"""

__version__ = "0.2.0.dev0"


def build_id():
    """Return the build id of the code on the board, None when unknown.

    ``make deploy`` writes it (``git describe --always --dirty``) into its
    staged copy only, as ``sily/build.py`` (``BUILD``): never in ``src/``,
    so the host tests and a board never deployed by it have none (code
    copied otherwise over a deployed board leaves the earlier id). Not
    named ``build``: the import sets the package attribute of that name.
    """
    try:
        from sily.build import BUILD
    except ImportError:
        return None
    return BUILD
