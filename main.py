import asyncio
import time
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

IS_WINDOWS = sys.platform == 'win32'

local_operations = None
REMOTE_SUPPORT = False

try:
    from .api import RemoteControlServer
    REMOTE_SUPPORT = True
except ImportError:
    pass

@register("astrbot_plugin_galplayer", "随风潜入夜", "和群友一起玩Galgame", "1.1.0")
class GalgamePlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_sessions = {}
        self.temp_img_dir = Path("data") / "tmp" / "galplayer"
        self.temp_img_dir.mkdir(parents=True, exist_ok=True)
        
        self.local_mode_available = False
        if IS_WINDOWS:
            try:
                from . import local_operations as lo
                globals()['local_operations'] = lo
                self.local_mode_available = True
            except ImportError as e:
                logger.critical(f"当前是Windows系统，但无法加载本地操作模块。请检查依赖。错误: {e}")
        
        self.mode = self.config.get("mode", "local")

        if self.mode == "local":
            if not IS_WINDOWS:
                logger.info("当前系统非Windows，自动切换到远程模式。")
                self.mode = "remote"
            elif not self.local_mode_available:
                logger.warning("配置为本地模式，但本地模块加载失败。将强制切换到远程模式。")
                self.mode = "remote"
    
        self.remote_server = None
        if self.mode == "remote":
            if not REMOTE_SUPPORT:
                logger.error("远程模式需要 'websockets' 库，但无法导入。插件功能将被禁用。")
                self.mode = "disabled"
            else:
                secret_token = self.config.get("remote_secret_token")
                if not secret_token:
                    logger.error("远程模式已启用，但未在配置中设置 'remote_secret_token'。插件功能将被禁用。")
                    self.mode = "disabled"
                else:
                    server_config = self.config.get("remote_server", {})
                    host = server_config.get("host", "0.0.0.0")
                    port = server_config.get("port", 8765)
                    self.remote_server = RemoteControlServer(host, port, secret_token)
                    asyncio.create_task(self.remote_server.start())
        
        logger.info(f"Galgame 插件已加载。运行模式: {self.mode.upper()}")

    async def terminate(self):
        if self.remote_server:
            await self.remote_server.stop()
        logger.info("Galgame 插件已卸载。")

    def get_session_id(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return f"group_{group_id}" if group_id else f"private_{event.get_sender_id()}"
    
    async def _handle_game_action(self, event: AstrMessageEvent, session: dict, key_to_press: str = None, take_screenshot: bool = True):
        session_id = self.get_session_id(event)
        if self.mode == "remote":
            await self._handle_remote_action(event, session, session_id, key_to_press, take_screenshot)
        elif self.mode == "local" and self.local_mode_available:
            await self._handle_local_action(event, session, key_to_press, take_screenshot)
        else:
             await event.send(event.plain_result(f"插件当前模式 ({self.mode}) 无法在此操作系统上执行操作。"))

    async def _handle_local_action(self, event: AstrMessageEvent, session: dict, key_to_press: str, take_screenshot: bool):
        try:
            window = session.get("window")
            if not window or not window.visible:
                raise Exception("游戏窗口不可见或已关闭。")
            if key_to_press:
                input_method = self.config.get("input_method", "PostMessage")
                await asyncio.to_thread(local_operations.press_key_on_window, window, key_to_press, input_method)
            if take_screenshot:
                if key_to_press:
                    await asyncio.sleep(self.config.get("screenshot_delay_seconds", 0.5))
                save_path_str = str(session['save_path'])
                await asyncio.to_thread(local_operations.screenshot_window, window, save_path_str)
                await event.send(event.image_result(save_path_str))
        except Exception as e:
            logger.error(f"处理本地游戏动作时出错: {e}")
            await event.send(event.plain_result("游戏窗口似乎已经关闭或出现问题，游戏已自动结束。"))
            if (session_id := self.get_session_id(event)) in self.game_sessions:
                del self.game_sessions[session_id]

    async def _handle_remote_action(self, event: AstrMessageEvent, session: dict, session_id: str, key_to_press: str, take_screenshot: bool):
        if not self.remote_server:
            await event.send(event.plain_result("错误：远程服务器未初始化。"))
            return
        try:
            if key_to_press:
                input_method = self.config.get("input_method", "PostMessage")
                await self.remote_server.remote_press_key(session_id, key_to_press, input_method)
            if take_screenshot:
                delay = self.config.get("screenshot_delay_seconds", 0.5) if key_to_press else 0
                save_path_str = str(session['save_path'])
                await self.remote_server.remote_screenshot(session_id, save_path_str, delay)
                await event.send(event.image_result(save_path_str))
        except ConnectionError:
            await event.send(event.plain_result("远程客户端未连接。请确保远程脚本正在运行并已连接。"))
        except Exception as e:
            logger.error(f"处理远程游戏动作时出错: {e}")
            await event.send(event.plain_result(f"远程操作失败: {e}"))
            # 如果远程操作失败，也清理会话
            if (sid := self.get_session_id(event)) in self.game_sessions:
                del self.game_sessions[sid]

    @filter.command_group("gal", alias={"g"})
    async def gal_group(self): ...

    @gal_group.command("start", alias={"开始游戏"})
    async def start_game(self, event: AstrMessageEvent, window_title: str):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            yield event.plain_result("本群聊已在游戏中！请先用 /gal stop 停止。")
            return
        
        save_path = self.temp_img_dir / f"{session_id}.png"

        if self.mode == "remote":
            if not self.remote_server or not self.remote_server.client:
                yield event.plain_result("远程客户端未连接。请在远程电脑上运行客户端脚本。")
                return
            yield event.plain_result(f"正在通知远程客户端查找窗口: '{window_title}'...")
            try:
                await self.remote_server.remote_start_session(session_id, window_title)
                self.game_sessions[session_id] = {"window_title": window_title, "last_triggered_time": 0.0, "save_path": save_path}
                logger.info(f"会话 {session_id} 开始远程游戏，窗口: {window_title}")
                yield event.plain_result("远程游戏开始！正在获取当前画面：")
                await self._handle_remote_action(event, self.game_sessions[session_id], session_id, key_to_press=None, take_screenshot=True)
            except Exception as e:
                yield event.plain_result(f"启动远程游戏失败: {e}")

        elif self.mode == "local" and self.local_mode_available:
            yield event.plain_result(f"正在查找本地窗口: '{window_title}'...")
            window = await asyncio.to_thread(local_operations.find_game_window, window_title)
            if not window:
                yield event.plain_result(f"找不到窗口 '{window_title}'。请确保游戏已运行且标题匹配。")
                return
            self.game_sessions[session_id] = {"window": window, "last_triggered_time": 0.0, "save_path": save_path}
            logger.info(f"会话 {session_id} 开始本地游戏，窗口: {window.title}")
            yield event.plain_result("本地游戏开始！这是当前画面：")
            await self._handle_local_action(event, self.game_sessions[session_id], key_to_press=None, take_screenshot=True)
        else:
            yield event.plain_result(f"插件当前模式 ({self.mode}) 无法启动游戏。请检查配置和运行环境。")
        event.stop_event()

    @gal_group.command("stop", alias={"停止游戏"})
    async def stop_game(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            if self.mode == "remote" and self.remote_server and self.remote_server.client:
                await self.remote_server.remote_stop_session(session_id) # 通知客户端清理
            
            if (save_path := self.game_sessions[session_id]['save_path']).exists():
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
            if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
                return
            session["last_triggered_time"] = time.time()
            await self._handle_game_action(event, session, take_screenshot=True)
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
            if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
                return
            session["last_triggered_time"] = time.time()
            should_screenshot = self.config.get("screenshot_on_type", True)
            await self._handle_game_action(event, session, key_to_press=actual_key_name, take_screenshot=should_screenshot)
        else:
            await event.send(event.plain_result("当前没有正在进行的游戏。"))
        event.stop_event()
        
    @gal_group.command("help", alias={"帮助"})
    async def show_help(self, event: AstrMessageEvent):
        quick_key = self.config.get("quick_advance_key", "space")
        input_method = self.config.get("input_method", "PostMessage")
        help_text = (f"🎮 Galgame 插件帮助 (当前总模式: {self.mode.upper()}) 🎮\n"
                     f"--------------------\n"
                     f"按键模式: {input_method}\n"
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
        if session_id in self.game_sessions and event.message_str.strip().lower() in ["g", "gal"]:
            session = self.game_sessions[session_id]
            if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
                return
            session["last_triggered_time"] = time.time()
            quick_key = self.config.get("quick_advance_key", "space")
            await self._handle_game_action(event, session, key_to_press=quick_key)
            event.stop_event()
