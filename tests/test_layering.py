"""Package isolation rules (MESH_DESIGN §1, README layout).

- mesh: application-agnostic, platform injected: no sily, board, machine,
  and not the host-only simulator sim.
- board: hardware only: no sily, mesh.
- sim: host-only, outside src/ (not deployed); uses mesh only: no sily,
  board, machine.
"""

import os
import unittest

import run

_ROOT = run._dirname(__file__) + "/.."
_SRC = _ROOT + "/src"

# Package directory -> modules it must not import.
_FORBIDDEN = {
    _SRC + "/mesh": ("sily", "board", "machine", "sim"),
    _SRC + "/board": ("sily", "mesh"),
    _ROOT + "/sim": ("sily", "board", "machine"),
}


def imported_modules(source):
    """Return the top-level names of the modules imported by source.

    Line based: handles `import a.b as c, d` and `from a.b import c`, at any
    indentation; relative imports stay within the package and are skipped. A
    line of a multi-line string starting with `import` or `from` is read as
    an import, which can only make the check stricter.
    """
    names = []
    for line in source.split("\n"):
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == "import":
            for item in line.split("import", 1)[1].split(","):
                names.append(item.split()[0].split(".")[0])
        elif words[0] == "from" and not words[1].startswith("."):
            names.append(words[1].split(".")[0])
    return names


def python_files(directory):
    """Return the paths of the .py files under directory, recursively."""
    paths = []
    for name in os.listdir(directory):
        path = directory + "/" + name
        if os.stat(path)[0] & 0x4000:
            paths.extend(python_files(path))
        elif name.endswith(".py"):
            paths.append(path)
    return paths


class ImportedModules(unittest.TestCase):
    def test_import_forms(self):
        source = "\n".join(
            (
                '"""Docstring."""',
                "import os",
                "import a.b as c, d",
                "from e.f import g",
                "from . import h",
                "from .i import j",
                "",
                "def k():",
                "    import machine",
                "    x = 1  # import y",
            )
        )
        self.assertEqual(imported_modules(source), ["os", "a", "d", "e", "machine"])


def _exists(path):
    try:
        os.stat(path)
    except OSError:
        return False
    return True


class Isolation(unittest.TestCase):
    def test_packages(self):
        for package, forbidden in _FORBIDDEN.items():
            paths = python_files(package)
            self.assertTrue(paths, "no source found for " + package)
            for path in paths:
                with open(path) as f:
                    imported = imported_modules(f.read())
                for name in forbidden:
                    self.assertFalse(name in imported, "%s imports %s" % (path, name))

    def test_simulator_not_deployed(self):
        # make deploy copies src/: the simulator must live outside it.
        self.assertTrue(_exists(_ROOT + "/sim/__init__.py"))
        self.assertFalse(_exists(_SRC + "/sim"))
