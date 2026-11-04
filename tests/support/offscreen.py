"""Force Qt offscreen for tests. Import this before any PySide6 import.

Test runs were creating real cocoa windows and stealing focus from the user.
A caller that sets QT_QPA_PLATFORM explicitly still wins.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MOUSER_TESTS_NO_GUI", "1")
