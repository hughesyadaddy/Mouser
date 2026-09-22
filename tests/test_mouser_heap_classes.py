"""tools/mouser-heap-classes: heap -s parsing, CLI modes and failure exits."""

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, "tools", "mouser-heap-classes")
_FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "heap-s.txt")

# Counts in the fixture: hackintosh Mouser 3.6.0 pid 3191 after 81 h,
# 6.0 GB footprint, captured 2026-09-21 (both leaks live).
_EXPECTED = {
    "CGEvent": 3_217_274,
    "CGSEventAppendix": 3_217_274,
    "HIDEvent": 9_357_423,
    "NSXPCConnection": 729_457,
    "GPProcessMonitor": 729_442,
    "CGImage": 158,
    "non-object": 17_142_676,
}
_EXPECTED_TOTAL_BYTES = 5_874_279_184


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("mouser_heap_classes", _TOOL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        with open(_FIXTURE, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_fixture_counts(self):
        parsed = self.tool.parse_heap_s(self.text)
        self.assertEqual(parsed["classes"], _EXPECTED)
        self.assertEqual(parsed["total_bytes"], _EXPECTED_TOTAL_BYTES)
        self.assertEqual(parsed["nodes"], 63_308_610)

    def test_exact_class_names_only(self):
        """CGImageProvider, _NSXPCConnectionClassCache etc. must not be
        folded into the indicator classes."""
        text = (
            "All zones: 3 nodes (300 bytes)\n"
            "   COUNT      BYTES       AVG   CLASS_NAME     TYPE    BINARY\n"
            "   =====      =====       ===   ==========     ====    ======\n"
            "     100       1000      10.0   CGImageProvider                CFType  CoreGraphics\n"
            "      50        500      10.0   _NSXPCConnectionClassCache     ObjC    Foundation\n"
            "       7         70      10.0   CGImage                        CFType  CoreGraphics\n"
            "       3         30      10.0   NSMutableArray (Storage)       C       CoreFoundation\n"
        )
        parsed = self.tool.parse_heap_s(text)
        self.assertEqual(parsed["classes"]["CGImage"], 7)
        self.assertEqual(parsed["classes"]["NSXPCConnection"], 0)
        self.assertEqual(parsed["total_bytes"], 300)

    def test_rows_before_the_table_header_are_ignored(self):
        text = "  12 34 5.0 CGEvent  CFType  SkyLight\nno table here\n"
        parsed = self.tool.parse_heap_s(text)
        self.assertEqual(parsed["classes"]["CGEvent"], 0)
        self.assertEqual(parsed["total_bytes"], 0)

    def test_empty_report_gives_zeroes(self):
        parsed = self.tool.parse_heap_s("")
        self.assertEqual(set(parsed["classes"]), set(self.tool.CLASSES))
        self.assertTrue(all(v == 0 for v in parsed["classes"].values()))


class CliTests(unittest.TestCase):
    def _run(self, *args, path=None):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        return subprocess.run(
            [sys.executable, _TOOL, *args], capture_output=True, text=True, env=env
        )

    def _fake_heap_dir(self, script: str) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
        heap = os.path.join(tmp, "heap")
        with open(heap, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + script)
        os.chmod(heap, os.stat(heap).st_mode | stat.S_IXUSR)
        return tmp

    def test_parse_mode_prints_json(self):
        result = self._run("--parse", _FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["pid"], 3191)
        self.assertIsInstance(report["ts"], int)
        self.assertEqual(report["classes"], _EXPECTED)
        self.assertEqual(report["total_bytes"], _EXPECTED_TOTAL_BYTES)
        self.assertEqual(
            list(report), ["pid", "ts", "classes", "total_bytes"]
        )

    def test_parse_mode_missing_file_is_a_usage_error(self):
        result = self._run("--parse", "/nonexistent/heap.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("mouser-heap-classes:", result.stderr)

    def test_pid_mode_runs_heap_and_reports(self):
        fake = self._fake_heap_dir(f'[ "$1" = -s ] && [ "$2" = 4242 ] && exec /bin/cat "{_FIXTURE}"\nexit 9\n')
        result = self._run("--pid", "4242", path=fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["pid"], 4242)
        self.assertEqual(report["classes"]["HIDEvent"], _EXPECTED["HIDEvent"])

    def test_heap_missing_exits_2(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", empty]))
        result = self._run("--pid", "1", path=empty)
        self.assertEqual(result.returncode, 2)
        self.assertIn("heap not found", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_heap_permission_denied_exits_2_with_the_reason(self):
        fake = self._fake_heap_dir(
            "echo 'heap[123]: Permission denied: task_for_pid(4242)' >&2\nexit 1\n"
        )
        result = self._run("--pid", "4242", path=fake)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Permission denied", result.stderr)
        self.assertIn("get-task-allow", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_name_mode_without_a_process_exits_2(self):
        fake = self._fake_heap_dir("exit 0\n")
        pgrep = os.path.join(fake, "pgrep")
        with open(pgrep, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(pgrep, os.stat(pgrep).st_mode | stat.S_IXUSR)
        result = self._run("--name", "NoSuchApp", path=fake)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no process named NoSuchApp", result.stderr)

    def test_name_mode_resolves_pid_via_pgrep(self):
        fake = self._fake_heap_dir(f'[ "$2" = 777 ] && exec /bin/cat "{_FIXTURE}"\nexit 9\n')
        pgrep = os.path.join(fake, "pgrep")
        with open(pgrep, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\n[ "$2" = Mouser ] && echo 777\n')
        os.chmod(pgrep, os.stat(pgrep).st_mode | stat.S_IXUSR)
        result = self._run("--name", "Mouser", path=fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pid"], 777)

    def test_requires_exactly_one_target(self):
        self.assertEqual(self._run().returncode, 2)  # argparse usage
        self.assertEqual(self._run("--pid", "1", "--parse", _FIXTURE).returncode, 2)


if __name__ == "__main__":
    unittest.main()
