import time
import ctypes
from ctypes import wintypes
import win32api
import win32con
import win32gui
import win32ui
import pygetwindow as gw
from PIL import Image

wintypes.ULONG_PTR = wintypes.WPARAM
class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.ULONG_PTR))
class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.ULONG_PTR))
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))
class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT))
    _anonymous_ = ("_input",)
    _fields_ = (("type", wintypes.DWORD), ("_input", _INPUT))

def find_game_window(window_title: str):
    """根据窗口标题查找游戏窗口"""
    try:
        windows = gw.getWindowsWithTitle(window_title)
        return windows[0] if windows else None
    except Exception:
        return None

def screenshot_window(window, save_path: str):
    """对指定窗口进行后台截图"""
    hwnd = window._hWnd
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.2)
    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bot - top
    if width <= 0 or height <= 0:
        raise ValueError("窗口尺寸无效，无法截图。")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    save_bitmap = win32ui.CreateBitmap()
    try:
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)
        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)
        bmp_str = save_bitmap.GetBitmapBits(True)
        im = Image.frombuffer('RGB', (width, height), bmp_str, 'raw', 'BGRX', 0, 1)
        im.save(save_path)
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    return save_path

def press_key_on_window(window, key_name: str, method: str):
    """向指定窗口模拟按键"""
    VK_CODE = { 'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D, 'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12, 'pause': 0x13, 'caps_lock': 0x14, 'esc': 0x1B, 'space': 0x20, 'page_up': 0x21, 'page_down': 0x22, 'end': 0x23, 'home': 0x24, 'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28, 'ins': 0x2D, 'del': 0x2E, '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39, 'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A, 'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B, ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF, '`': 0xC0, '[': 0xDB, '\\': 0xDC, ']': 0xDD, "'": 0xDE }
    key_code = VK_CODE.get(key_name.lower())
    if not key_code: return
    EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
    if method == "SendInput":
        if not window.isActive:
            try: window.activate(); time.sleep(0.1)
            except Exception: return
        scan_code = win32api.MapVirtualKey(key_code, 0)
        keybd_flags = 0x0008
        if key_code in EXTENDED_KEYS: keybd_flags |= 0x0001
        ip_down = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=scan_code, dwFlags=keybd_flags, time=0, dwExtraInfo=0))
        ctypes.windll.user32.SendInput(1, ctypes.byref(ip_down), ctypes.sizeof(ip_down))
        time.sleep(0.05)
        ip_up = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=scan_code, dwFlags=keybd_flags | 0x0002, time=0, dwExtraInfo=0))
        ctypes.windll.user32.SendInput(1, ctypes.byref(ip_up), ctypes.sizeof(ip_up))
    else:
        hwnd = window._hWnd
        scan_code = win32api.MapVirtualKey(key_code, 0)
        lParam_down = (1) | (scan_code << 16)
        if key_code in EXTENDED_KEYS: lParam_down |= (1 << 24)
        lParam_up = lParam_down | (1 << 30) | (1 << 31)
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, key_code, lParam_down)
        time.sleep(0.05)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, key_code, lParam_up)

def click_window(window, rel_x: float, rel_y: float):
    """在窗口上模拟鼠标左键点击，rel_x/rel_y 为相对坐标"""
    hwnd = window._hWnd
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.2)
    if not window.isActive:
        try:
            window.activate()
            time.sleep(0.05)
        except Exception:
            pass

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("窗口尺寸无效，无法点击。")

    rel_x = max(0.0, min(1.0, rel_x))
    rel_y = max(0.0, min(1.0, rel_y))
    target_x = int(left + rel_x * width)
    target_y = int(top + rel_y * height)

    client_x, client_y = win32gui.ScreenToClient(hwnd, (target_x, target_y))
    client_x = max(0, min(0xFFFF, client_x))
    client_y = max(0, min(0xFFFF, client_y))
    lparam = win32api.MAKELONG(client_x, client_y)

    win32api.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.05)
    win32api.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)

