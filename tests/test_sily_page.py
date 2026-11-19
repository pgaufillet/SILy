"""The status page's JavaScript (sily/page.html): its logic run under node.

tests/page_test.js extracts the page's ``<script id=logic>`` (summary,
alarms, recent delivery, event log, storage, graph, HTML views; no DOM) and
runs one group of cases per test below against fixture answers of
``/status.json``; its exit status tells the result, its failures are
printed. Skipped when ``node`` is not on the PATH: the page's code is then
not tested (its polling and drawing, which need a browser, never are).
"""

import os
import unittest

import run

_SCRIPT = run._dirname(__file__) + "/page_test.js"
GROUPS = ("summary", "recent", "alarms", "events", "gaps", "storage", "graph", "html")


class Logic(unittest.TestCase):
    def _node(self, *args):
        if os.system("node --version > /dev/null 2>&1") != 0:
            self.skipTest("node not found: the page's JavaScript is not tested")
        return os.system('node "%s" %s' % (_SCRIPT, " ".join(args)))

    def _group(self, name):
        self.assertEqual(self._node(name), 0, "page_test.js " + name + ": see its output above")

    def test_groups_are_these(self):
        self.assertEqual(self._node("--groups", ",".join(GROUPS)), 0)

    def test_unknown_group_fails(self):
        self.assertNotEqual(self._node("none > /dev/null 2>&1"), 0)

    def test_summary(self):
        self._group("summary")

    def test_recent_delivery(self):
        self._group("recent")

    def test_alarms(self):
        self._group("alarms")

    def test_events(self):
        self._group("events")

    def test_gaps(self):
        self._group("gaps")

    def test_storage(self):
        self._group("storage")

    def test_graph(self):
        self._group("graph")

    def test_html(self):
        self._group("html")
