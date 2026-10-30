"""
Windows: rahmenloses Hauptfenster, das sich trotzdem wie ein normales Fenster
an Raendern und Ecken in der Groesse aendern laesst

Die eigene Titelleiste braucht Qt.FramelessWindowHint. Qt legt das Fenster dann
ohne WS_THICKFRAME an - und ohne diesen Stil verweigert Windows das Ziehen an
den Raendern, auch wenn WM_NCHITTEST HTLEFT usw. meldet (der Doppelpfeil
erscheint, aber nichts passiert). Deshalb:

- enable_resize_frame() ergaenzt WS_THICKFRAME (Groesse aendern, Aero Snap).
- handle_nc_calc_size() beantwortet WM_NCCALCSIZE so, dass Windows keinen
  sichtbaren Rahmen zeichnet; maximiert wird auf die Arbeitsflaeche begrenzt,
  sonst ragte das Fenster ueber den Bildschirm hinaus.
- set_resize_cursor() setzt den Groessen-Cursor (Qt setzt sonst immer den Pfeil).
"""

import ctypes
from ctypes import wintypes

WM_SETCURSOR = 0x0020
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084

_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000
_SWP_FRAMECHANGED = 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010  # + NOMOVE, NOSIZE, NOZORDER, NOACTIVATE

# Hit-Test-Code (HTLEFT ... HTBOTTOMRIGHT) -> Windows-Cursor (IDC_SIZE*)
RESIZE_CURSORS = {10: 32644, 11: 32644, 12: 32645, 15: 32645,
                  13: 32642, 17: 32642, 14: 32643, 16: 32643}


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


def _user32():
    # Eigene Instanz: argtypes nicht global fuer andere ctypes-Nutzer setzen
    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.SetCursor.argtypes = [wintypes.HANDLE]
    return user32


def enable_resize_frame(hwnd: int):
    """Ergaenzt WS_THICKFRAME, falls er fehlt (idempotent)"""
    user32 = _user32()
    style = user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
    if style & _WS_THICKFRAME:
        return
    user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _WS_THICKFRAME)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FRAMECHANGED)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def handle_nc_calc_size(hwnd: int, wparam: int, lparam: int, maximized: bool) -> bool:
    """True, wenn WM_NCCALCSIZE erledigt ist (Rueckgabewert 0): der ganze
    Fensterbereich ist Client-Bereich, kein Systemrahmen sichtbar. Maximiert
    wird der Bereich auf die Arbeitsflaeche des Monitors begrenzt - Windows
    legt maximierte Fenster mit WS_THICKFRAME um die Rahmenbreite ueber den
    Bildschirmrand hinaus (Qt teils nicht), so passt es in beiden Faellen."""
    if not wparam:
        return False
    if maximized:
        user32 = _user32()
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):  # NEAREST
            work = info.rcWork
            rect = _NCCALCSIZE_PARAMS.from_address(lparam).rgrc[0]
            rect.left, rect.top = max(rect.left, work.left), max(rect.top, work.top)
            rect.right, rect.bottom = min(rect.right, work.right), min(rect.bottom, work.bottom)
    return True


def set_resize_cursor(hit_code: int) -> bool:
    """Setzt den Groessen-Cursor fuer einen Rand-Hit-Test-Code; False fuer andere Codes"""
    cursor = RESIZE_CURSORS.get(hit_code)
    if cursor is None:
        return False
    user32 = _user32()
    user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(cursor)))
    return True
