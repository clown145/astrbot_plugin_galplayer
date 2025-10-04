import asyncio
import os
import time
from pathlib import Path

import win32api
import win32con
import win32gui
import win32ui
import ctypes
from ctypes import wintypes

import pygetwindow as gw
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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
    except Exception as e:
        logger.error(f"查找窗口时出错: {e}")
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

    logger.info(f"已成功后台截图窗口 '{window.title}'")
    return save_path

def press_key_on_window(window, key_name: str, method: str):
    """向指定窗口模拟按键 (已修复扩展按键问题)"""
    VK_CODE = { 'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D, 'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12, 'pause': 0x13, 'caps_lock': 0x14, 'esc': 0x1B, 'space': 0x20, 'page_up': 0x21, 'page_down': 0x22, 'end': 0x23, 'home': 0x24, 'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28, 'ins': 0x2D, 'del': 0x2E, '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39, 'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A, 'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B, ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF, '`': 0xC0, '[': 0xDB, '\\': 0xDC, ']': 0xDD, "'": 0xDE }
    key_code = VK_CODE.get(key_name.lower())
    if not key_code:
        logger.error(f"未找到按键 '{key_name}' 的键码。")
        return

    EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E} # PageUp, PageDown, End, Home, Left, Up, Right, Down, Ins, Del

    if method == "SendInput":
        logger.info(f"正在用 SendInput(前台) 模式模拟按键 '{key_name}'。")
        if not window.isActive:
            try:
                window.activate()
                time.sleep(0.1)
            except Exception:
                logger.warning("尝试激活窗口失败，可能已关闭。")
                return

        scan_code = win32api.MapVirtualKey(key_code, 0)
        keybd_flags = 0x0008  # KEYEVENTF_SCANCODE
        if key_code in EXTENDED_KEYS:
            keybd_flags |= 0x0001  # KEYEVENTF_EXTENDEDKEY

        # 按下
        ip_down = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=scan_code, dwFlags=keybd_flags, time=0, dwExtraInfo=0))
        ctypes.windll.user32.SendInput(1, ctypes.byref(ip_down), ctypes.sizeof(ip_down))
        
        time.sleep(0.05)
        
        # 弹起
        ip_up = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=scan_code, dwFlags=keybd_flags | 0x0002, time=0, dwExtraInfo=0)) # 加上 KEYEVENTF_KEYUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(ip_up), ctypes.sizeof(ip_up))

    else:
        logger.info(f"正在用 PostMessage(后台) 模式模拟按键 '{key_name}'。")
        hwnd = window._hWnd
        scan_code = win32api.MapVirtualKey(key_code, 0)

        lParam_down = (1) | (scan_code << 16)
        if key_code in EXTENDED_KEYS:
            lParam_down |= (1 << 24)
        
        lParam_up = lParam_down | (1 << 30) | (1 << 31)

        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, key_code, lParam_down)
        time.sleep(0.05)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, key_code, lParam_up)

@register("astrbot_plugin_galplayer", "随风潜入夜", "和群友一起玩Galgame", "1.0.0")
class GalgamePlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_sessions = {}
        self.temp_img_dir = Path("data") / "tmp" / "galplayer"
        self.temp_img_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Galgame 插件已加载。")

    def get_session_id(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return f"group_{group_id}" if group_id else f"private_{event.get_sender_id()}"
    
    async def _handle_game_action(self, event: AstrMessageEvent, session: dict, key_to_press: str = None):
        try:
            window = session.get("window")
            if not window or not window.visible:
                raise Exception("游戏窗口不可见或已关闭。")
            
            if key_to_press:
                input_method = self.config.get("input_method", "PostMessage")
                await asyncio.to_thread(press_key_on_window, window, key_to_press, input_method)
                delay = self.config.get("screenshot_delay_seconds", 0.5)
                await asyncio.sleep(delay)

            save_path_str = str(session['save_path'])
            await asyncio.to_thread(screenshot_window, window, save_path_str)
            await event.send(event.image_result(save_path_str))
        except Exception as e:
            logger.error(f"处理游戏动作时出错: {e}")
            await event.send(event.plain_result("游戏窗口似乎已经关闭或出现问题，游戏已自动结束。"))
            session_id = self.get_session_id(event)
            if session_id in self.game_sessions:
                del self.game_sessions[session_id]

    @filter.command_group("gal", alias={"g"})
    async def gal_group(self):
        """Galgame 游戏指令组"""
        pass

    @gal_group.command("start", alias={"开始游戏"})
    async def start_game(self, event: AstrMessageEvent, window_title: str):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            yield event.plain_result("本群聊已在游戏中！请先用 /gal stop 停止。")
            return
        yield event.plain_result(f"正在查找窗口: '{window_title}'...")
        window = await asyncio.to_thread(find_game_window, window_title)
        if not window:
            yield event.plain_result(f"找不到窗口 '{window_title}'。请确保游戏已运行且标题匹配。")
            return
        
        save_path = self.temp_img_dir / f"{session_id}.png"
        self.game_sessions[session_id] = {"window": window, "last_triggered_time": 0.0, "save_path": save_path}
        logger.info(f"会话 {session_id} 开始游戏，窗口: {window.title}")
        yield event.plain_result("游戏开始！这是当前画面：")
        await self._handle_game_action(event, self.game_sessions[session_id])
        event.stop_event()

    @gal_group.command("stop", alias={"停止游戏"})
    async def stop_game(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            session = self.game_sessions[session_id]
            save_path = session['save_path']
            if save_path.exists():
                save_path.unlink()
            del self.game_sessions[session_id]
            yield event.plain_result("游戏已停止。")
        else:
            yield event.plain_result("当前没有正在进行的游戏。")
        event.stop_event()

    @gal_group.command("resend", alias={"重发"})
    async def resend_screenshot(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            session = self.game_sessions[session_id]
            cooldown = self.config.get("cooldown_seconds", 3.0)
            current_time = time.time()
            if current_time - session.get("last_triggered_time", 0) < cooldown:
                return
            
            session["last_triggered_time"] = current_time
            
            await self._handle_game_action(event, session)
        else:
            await event.send(event.plain_result("当前没有正在进行的游戏。"))
        event.stop_event()

    @gal_group.command("type", alias={"输"})
    async def type_key(self, event: AstrMessageEvent, key_name: str):
        KEY_ALIASES = { '空格': 'space', '回车': 'enter', '上': 'up', '下': 'down', '左': 'left', '右': 'right' }
        actual_key_name = KEY_ALIASES.get(key_name, key_name)
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            session = self.game_sessions[session_id]
            cooldown = self.config.get("cooldown_seconds", 3.0)
            current_time = time.time()
            if current_time - session.get("last_triggered_time", 0) < cooldown:
                return
            
            session["last_triggered_time"] = current_time
            
            await self._handle_game_action(event, session, key_to_press=actual_key_name)
        else:
            await event.send(event.plain_result("当前没有正在进行的游戏。"))
        event.stop_event()
        
    @gal_group.command("help", alias={"帮助"})
    async def show_help(self, event: AstrMessageEvent):
        quick_key = self.config.get("quick_advance_key", "space")
        input_method = self.config.get("input_method", "PostMessage")
        help_text = (f"🎮 Galgame 插件帮助 (当前模式: {input_method}) 🎮\n"
                     "--------------------\n"
                     "指令:\n"
                     "  /gal start <窗口标题>\n"
                     "  /gal stop\n"
                     "  /gal resend\n"
                     "  /gal type <按键名> (别名: 上/下/左/右/空格...)\n\n"
                     "快捷指令:\n"
                     f"  g 或 gal (快捷键: '{quick_key}')")
        yield event.plain_result(help_text)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_advance_message(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        if session_id not in self.game_sessions:
            return
            
        message_text = event.message_str.strip().lower()
        if message_text in ["g", "gal"]:
            session = self.game_sessions[session_id]
            cooldown = self.config.get("cooldown_seconds", 3.0)
            current_time = time.time()
            if current_time - session.get("last_triggered_time", 0) < cooldown:
                return
            
            session["last_triggered_time"] = current_time

            quick_key = self.config.get("quick_advance_key", "space")
            await self._handle_game_action(event, session, key_to_press=quick_key)
            event.stop_event()
