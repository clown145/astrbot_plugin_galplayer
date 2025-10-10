# --- START OF FILE remote_client.py ---

import asyncio
import websockets
import json
import time
import ctypes
from ctypes import wintypes
import win32api
import win32con
import win32gui
import win32ui
import pygetwindow as gw
from PIL import Image
import base64
from io import BytesIO
import logging
import configparser
from pathlib import Path

CONFIG_FILE = Path("gal_client_config.ini")

def create_default_config():
    """如果配置文件不存在，则创建一个默认的"""
    config = configparser.ConfigParser()
    config['Connection'] = {
        'ServerURI': 'ws://localhost:8765',
        'SecretToken': 'YOUR_SECRET_TOKEN_HERE'
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as configfile:
        config.write(configfile)
    logger.info(f"已创建默认配置文件: {CONFIG_FILE}")
    logger.info("请修改配置文件中的 ServerURI 和 SecretToken 后再重新运行脚本。")

def load_config():
    """加载配置"""
    if not CONFIG_FILE.exists():
        create_default_config()
        return None
    
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding='utf-8')
    return config

config = load_config()
if not config:
    exit()

SERVER_URI = config.get('Connection', 'ServerURI', fallback='ws://localhost:8765')
SECRET_TOKEN = config.get('Connection', 'SecretToken', fallback='')


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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


active_windows = {} # key: session_id, value: window object

def find_and_store_window(session_id: str, window_title: str):
    """为指定 session_id 查找并存储窗口对象"""
    global active_windows
    try:
        windows = gw.getWindowsWithTitle(window_title)
        if windows:
            window = windows[0]
            active_windows[session_id] = window
            logger.info(f"会话 [{session_id}] 成功绑定窗口: '{window.title}'")
            return True
        else:
            logger.error(f"会话 [{session_id}] 找不到窗口 '{window_title}'")
            if session_id in active_windows:
                del active_windows[session_id]
            return False
    except Exception as e:
        logger.error(f"会话 [{session_id}] 查找窗口时出错: {e}")
        if session_id in active_windows:
            del active_windows[session_id]
        return False

def screenshot_window(window):
    hwnd = window._hWnd
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.2)
    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bot - top
    if width <= 0 or height <= 0: raise ValueError("窗口尺寸无效，无法截图。")
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
        buffered = BytesIO()
        im.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
        return img_str
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

def press_key_on_window(window, key_name: str, method: str):
    VK_CODE = { 'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D, 'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12, 'pause': 0x13, 'caps_lock': 0x14, 'esc': 0x1B, 'space': 0x20, 'page_up': 0x21, 'page_down': 0x22, 'end': 0x23, 'home': 0x24, 'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28, 'ins': 0x2D, 'del': 0x2E, '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39, 'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A, 'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B, ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF, '`': 0xC0, '[': 0xDB, '\\': 0xDC, ']': 0xDD, "'": 0xDE }
    key_code = VK_CODE.get(key_name.lower())
    if not key_code:
        logger.error(f"未找到按键 '{key_name}' 的键码。")
        return
    EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
    if method == "SendInput":
        if not window.isActive:
            try: window.activate(); time.sleep(0.1)
            except: logger.warning("尝试激活窗口失败，可能已关闭。"); return
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


def get_window_metrics(window):
    hwnd = window._hWnd
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
    client_rect = win32gui.GetClientRect(hwnd)
    return {
        "hwnd": hwnd,
        "window_width": max(right - left, 0),
        "window_height": max(bottom - top, 0),
        "border_left": client_left - left,
        "border_top": client_top - top,
        "client_width": max(client_rect[2] - client_rect[0], 0),
        "client_height": max(client_rect[3] - client_rect[1], 0),
        "screen_left": left,
        "screen_top": top,
    }


def click_window_by_ratio(window, x_ratio: float, y_ratio: float, method: str):
    metrics = get_window_metrics(window)
    if metrics["window_width"] <= 0 or metrics["window_height"] <= 0:
        raise ValueError("窗口尺寸无效，无法执行点击。")

    x_ratio = max(0.0, min(1.0, x_ratio))
    y_ratio = max(0.0, min(1.0, y_ratio))

    click_x_window = int(round(x_ratio * (metrics["window_width"] - 1)))
    click_y_window = int(round(y_ratio * (metrics["window_height"] - 1)))
    client_x = click_x_window - metrics["border_left"]
    client_y = click_y_window - metrics["border_top"]

    client_x = max(0, min(metrics["client_width"] - 1, client_x))
    client_y = max(0, min(metrics["client_height"] - 1, client_y))

    if method == "SendInput":
        screen_x = metrics["screen_left"] + metrics["border_left"] + client_x
        screen_y = metrics["screen_top"] + metrics["border_top"] + client_y
        if not window.isActive:
            try:
                window.activate()
                time.sleep(0.05)
            except Exception:
                pass
        win32api.SetCursorPos((screen_x, screen_y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        l_param = (client_y << 16) | (client_x & 0xFFFF)
        win32api.PostMessage(metrics["hwnd"], win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param)
        time.sleep(0.02)
        win32api.PostMessage(metrics["hwnd"], win32con.WM_LBUTTONUP, 0, l_param)


async def send_json(websocket, data):
    await websocket.send(json.dumps(data))

async def handle_command(websocket, command):
    global active_windows
    action = command.get("action")
    session_id = command.get("session_id")

    if not session_id:
        logger.warning(f"收到缺少 session_id 的指令: {action}")
        return

    logger.info(f"收到会话 [{session_id}] 的指令: {action}")
    
    try:
        if action == "start_session":
            find_and_store_window(session_id, command.get("title"))
            return
        
        if action == "stop_session":
            if session_id in active_windows:
                del active_windows[session_id]
                logger.info(f"会话 [{session_id}] 已结束。")
            return

        game_window = active_windows.get(session_id)
        if not game_window or not game_window.visible:
            raise Exception(f"会话 [{session_id}] 的游戏窗口未找到或已关闭。")
        
        if action == "press_key":
            press_key_on_window(game_window, command.get("key"), command.get("method"))
        
        elif action == "click":
            x_ratio = command.get("x_ratio")
            y_ratio = command.get("y_ratio")
            if x_ratio is None or y_ratio is None:
                logger.error(f"会话 [{session_id}] 的点击指令缺少坐标。")
            else:
                click_window_by_ratio(game_window, float(x_ratio), float(y_ratio), command.get("method", "PostMessage"))
        
        elif action == "screenshot":
            request_id = command.get("request_id")
            if not request_id: return
            
            delay = command.get("delay", 0)
            if delay > 0: await asyncio.sleep(delay)
            
            img_b64 = screenshot_window(game_window)
            await send_json(websocket, {"request_id": request_id, "status": "success", "image_data": img_b64})

    except Exception as e:
        logger.error(f"处理会话 [{session_id}] 指令 '{action}' 时出错: {e}")
        if action == "screenshot" and "request_id" in command:
            await send_json(websocket, {"request_id": command["request_id"], "status": "error", "error": str(e)})

async def client_handler(uri):
    if not SECRET_TOKEN or SECRET_TOKEN == "YOUR_SECRET_TOKEN_HERE":
        logger.error("错误：请在配置文件 gal_client_config.ini 中设置 SecretToken！")
        return

    ten_mb = 10 * 1024 * 1024
    while True:
        try:
            async with websockets.connect(uri, max_size=ten_mb) as websocket:
                logger.info(f"已连接到服务器 {uri}，正在发送验证信息...")
                auth_payload = {"type": "auth", "token": SECRET_TOKEN}
                await websocket.send(json.dumps(auth_payload))
                
                response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                response_data = json.loads(response)
                
                if response_data.get("status") != "auth_success":
                    raise ConnectionRefusedError("服务器验证失败，请检查Token是否一致。")
                
                logger.info("服务器验证成功，连接已建立。")

                async for message in websocket:
                    try:
                        command = json.loads(message)
                        asyncio.create_task(handle_command(websocket, command))
                    except json.JSONDecodeError:
                        logger.error(f"收到无法解析的消息: {message}")
        except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, asyncio.TimeoutError) as e:
            logger.error(f"连接失败或中断: {e}. 5秒后重试...")
            active_windows.clear() # 连接断开时，清空所有会话
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"发生未知错误: {e}. 5秒后重试...")
            active_windows.clear()
            await asyncio.sleep(5)

if __name__ == "__main__":
    logger.info(f"正在尝试连接到 {SERVER_URI}...")
    asyncio.run(client_handler(SERVER_URI))
