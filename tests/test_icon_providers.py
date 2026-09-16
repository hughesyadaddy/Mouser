"""QML image providers: rendered icons are memoized in bounded LRU caches.

No QApplication: ``_render_svg_pixmap`` / ``QSvgRenderer`` / ``QPixmap`` are
replaced with counting fakes and ``SystemIconProvider`` gets an injected
file-icon provider, so the tests exercise cache keys, hit/miss accounting
and eviction rather than Qt's rasterizer.
"""
import unittest
from unittest.mock import patch

try:
    import main_qml
    from PySide6.QtCore import QSize
except Exception:  # pragma: no cover - env without PySide6 / project deps
    main_qml = None
    QSize = None


class _FakePixmap:
    """Default construction is null, like ``QPixmap()``; pass ``tag`` for a
    'rendered' one."""

    def __init__(self, null=None, tag=None):
        self._null = (tag is None) if null is None else null
        self.tag = tag

    def isNull(self):
        return self._null


class _FakeIcon:
    def __init__(self, null=False, path=""):
        self._null = null
        self._path = path
        self.pixmap_calls = 0

    def isNull(self):
        return self._null

    def pixmap(self, w, h):
        self.pixmap_calls += 1
        return _FakePixmap(tag=(self._path, w, h))


class _FakeFileIconProvider:
    def __init__(self, null_paths=()):
        self.calls = []
        self._null_paths = set(null_paths)

    def icon(self, file_info):
        path = file_info.filePath()
        self.calls.append(path)
        return _FakeIcon(null=path in self._null_paths, path=path)


class LRUCacheTests(unittest.TestCase):
    def test_bounded_and_evicts_least_recently_used(self):
        cache = main_qml._LRUCache(3) if main_qml else None
        if cache is None:
            self.skipTest("main_qml unavailable")
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertEqual(cache.get("a"), 1)  # refresh "a"
        cache.put("d", 4)                    # evicts "b"
        self.assertEqual(len(cache), 3)
        self.assertIn("a", cache)
        self.assertNotIn("b", cache)
        self.assertEqual(cache.get("b"), None)
        self.assertEqual((cache.hits, cache.misses), (1, 1))
        cache.clear()
        self.assertEqual(len(cache), 0)


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class RequestParsingTests(unittest.TestCase):
    def test_app_icon_key_defaults_and_overrides(self):
        self.assertEqual(
            main_qml._app_icon_request_key("mouse", QSize(-1, -1)),
            ("mouse.svg", "#000000", 24),
        )
        self.assertEqual(
            main_qml._app_icon_request_key("mouse.svg?color=%23ff0000&size=40", QSize(16, 16)),
            ("mouse.svg", "#ff0000", 40),
        )
        self.assertEqual(
            main_qml._app_icon_request_key("mouse?size=4", QSize(16, 16)),
            ("mouse.svg", "#000000", 12),
        )
        self.assertEqual(
            main_qml._app_icon_request_key("mouse?size=big", QSize(16, 16)),
            ("mouse.svg", "#000000", 16),
        )

    def test_system_icon_key_decodes_path(self):
        self.assertEqual(
            main_qml._system_icon_request_key(
                "/Applications/Foo%20Bar.app?size=32", QSize(-1, -1)
            ),
            ("/Applications/Foo Bar.app", 32),
        )
        self.assertEqual(
            main_qml._system_icon_request_key("", None),
            ("", 24),
        )


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class SvgRendererCacheTests(unittest.TestCase):
    def setUp(self):
        self._saved = main_qml._SVG_RENDERERS
        main_qml._SVG_RENDERERS = main_qml._LRUCache(4)
        self.addCleanup(setattr, main_qml, "_SVG_RENDERERS", self._saved)

    def test_one_renderer_per_source_and_invalid_not_cached(self):
        built = []

        class _FakeRenderer:
            def __init__(self, path):
                built.append(path)
                self._path = path

            def isValid(self):
                return not self._path.endswith("missing.svg")

        with patch.object(main_qml, "QSvgRenderer", _FakeRenderer):
            first = main_qml._svg_renderer("/icons/a.svg")
            second = main_qml._svg_renderer("/icons/a.svg")
            self.assertIs(first, second)
            self.assertIsNone(main_qml._svg_renderer("/icons/missing.svg"))
            self.assertIsNone(main_qml._svg_renderer("/icons/missing.svg"))

        self.assertEqual(built, ["/icons/a.svg", "/icons/missing.svg", "/icons/missing.svg"])
        self.assertEqual(len(main_qml._SVG_RENDERERS), 1)


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class AppIconProviderTests(unittest.TestCase):
    def _provider(self, capacity=256):
        return main_qml.AppIconProvider("/tmp/Mouser", cache_capacity=capacity)

    def test_repeated_requests_render_once(self):
        provider = self._provider()
        renders = []

        def render(path, color, size):
            renders.append((path, color.name(), size))
            return _FakePixmap(tag=len(renders))

        with patch.object(main_qml, "_render_svg_pixmap", side_effect=render):
            out = QSize()
            first = provider.requestPixmap("mouse?color=%23ffffff&size=20", out, QSize(-1, -1))
            for _ in range(99):
                again = provider.requestPixmap("mouse?color=%23ffffff&size=20", out, QSize(-1, -1))
                self.assertIs(again, first)

        self.assertEqual(renders, [("/tmp/Mouser/images/icons/mouse.svg", "#ffffff", 20)])
        self.assertEqual((out.width(), out.height()), (20, 20))
        self.assertEqual((provider.cache.hits, provider.cache.misses), (99, 1))

    def test_distinct_color_and_size_are_distinct_entries(self):
        provider = self._provider()
        with patch.object(
            main_qml, "_render_svg_pixmap", side_effect=lambda *a: _FakePixmap(tag="px")
        ) as render:
            provider.requestPixmap("mouse?color=%23ffffff&size=20", None, QSize(-1, -1))
            provider.requestPixmap("mouse?color=%23000000&size=20", None, QSize(-1, -1))
            provider.requestPixmap("mouse?color=%23000000&size=24", None, QSize(-1, -1))
            provider.requestPixmap("mouse?color=%23000000&size=24", None, QSize(-1, -1))
        self.assertEqual(render.call_count, 3)
        self.assertEqual(len(provider.cache), 3)

    def test_cache_is_bounded(self):
        provider = self._provider(capacity=256)
        with patch.object(
            main_qml, "_render_svg_pixmap", side_effect=lambda *a: _FakePixmap(tag="px")
        ) as render:
            for i in range(600):
                provider.requestPixmap(f"icon{i}?size=16", None, QSize(-1, -1))
        self.assertEqual(render.call_count, 600)
        self.assertEqual(len(provider.cache), 256)
        self.assertIn(("icon599.svg", "#000000", 16), provider.cache)
        self.assertNotIn(("icon0.svg", "#000000", 16), provider.cache)

    def test_null_render_is_not_cached_and_invalidate_clears(self):
        provider = self._provider()
        with patch.object(
            main_qml, "_render_svg_pixmap", side_effect=lambda *a: _FakePixmap(null=True)
        ) as render:
            provider.requestPixmap("nope", None, QSize(-1, -1))
            provider.requestPixmap("nope", None, QSize(-1, -1))
        self.assertEqual(render.call_count, 2)
        self.assertEqual(len(provider.cache), 0)

        with patch.object(
            main_qml, "_render_svg_pixmap", side_effect=lambda *a: _FakePixmap(tag="px")
        ):
            provider.requestPixmap("ok", None, QSize(-1, -1))
        self.assertEqual(len(provider.cache), 1)
        provider.invalidate()
        self.assertEqual(len(provider.cache), 0)


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class SystemIconProviderTests(unittest.TestCase):
    def _provider(self, capacity=256, null_paths=()):
        return main_qml.SystemIconProvider(
            cache_capacity=capacity,
            file_icon_provider=_FakeFileIconProvider(null_paths=null_paths),
        )

    def test_add_profile_reopen_hits_cache(self):
        """Every Add-Profile open re-requests every known app's icon; only
        the first open may touch NSWorkspace."""
        provider = self._provider()
        apps = [f"/Applications/App%20{i}.app" for i in range(40)]
        with patch.object(main_qml, "QPixmap", _FakePixmap):
            first_open = [
                provider.requestPixmap(f"{app}?size=32", None, QSize(-1, -1)) for app in apps
            ]
            for _ in range(10):
                reopen = [
                    provider.requestPixmap(f"{app}?size=32", None, QSize(-1, -1)) for app in apps
                ]
                for a, b in zip(first_open, reopen):
                    self.assertIs(a, b)

        self.assertEqual(len(provider._provider.calls), 40)
        self.assertEqual((provider.cache.hits, provider.cache.misses), (400, 40))
        self.assertEqual(first_open[0].tag, ("/Applications/App 0.app", 32, 32))

    def test_cache_is_bounded(self):
        provider = self._provider(capacity=8)
        with patch.object(main_qml, "QPixmap", _FakePixmap):
            for i in range(50):
                provider.requestPixmap(f"/Applications/A{i}.app?size=24", None, QSize(-1, -1))
        self.assertEqual(len(provider.cache), 8)
        self.assertEqual(len(provider._provider.calls), 50)

    def test_empty_path_and_null_icon_are_not_cached(self):
        provider = self._provider(null_paths={"/Applications/Gone.app"})
        out = QSize()
        with patch.object(main_qml, "QPixmap", _FakePixmap):
            empty = provider.requestPixmap("", out, QSize(-1, -1))
            self.assertTrue(empty.isNull())
            self.assertEqual((out.width(), out.height()), (24, 24))
            gone = provider.requestPixmap("/Applications/Gone.app", None, QSize(-1, -1))
            self.assertTrue(gone.isNull())
            provider.requestPixmap("/Applications/Gone.app", None, QSize(-1, -1))
        self.assertEqual(len(provider.cache), 0)
        self.assertEqual(provider._provider.calls.count("/Applications/Gone.app"), 2)

    def test_invalidate_forces_refetch(self):
        provider = self._provider()
        with patch.object(main_qml, "QPixmap", _FakePixmap):
            provider.requestPixmap("/Applications/A.app", None, QSize(-1, -1))
            provider.invalidate()
            provider.requestPixmap("/Applications/A.app", None, QSize(-1, -1))
        self.assertEqual(len(provider._provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
