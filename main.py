import asyncio
import json
import time
import sys
import inspect
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageFilter

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

@register("astrbot_plugin_galplayer", "随风潜入夜", "和群友一起玩Galgame", "1.2.0")
class GalgamePlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_sessions = {}
        self.temp_img_dir = Path("data") / "tmp" / "galplayer"
        self.temp_img_dir.mkdir(parents=True, exist_ok=True)
        self.annotation_dir = self.temp_img_dir / "annotations"
        self.annotation_dir.mkdir(parents=True, exist_ok=True)

        self.button_registry_path = Path("data") / "galplayer_buttons.json"
        self.button_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.button_registry = self._load_button_registry()
        self.pending_button_registrations = {}
        
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

    def _load_button_registry(self):
        if not self.button_registry_path.exists():
            return {}
        try:
            with self.button_registry_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            registry = {}
            for window_id, buttons in raw.items():
                registry[window_id] = {
                    name: tuple(value) if isinstance(value, (list, tuple)) and len(value) == 2 else value
                    for name, value in buttons.items()
                }
            return registry
        except Exception as e:
            logger.error(f"加载按钮配置失败: {e}")
            return {}

    def _save_button_registry(self):
        try:
            serializable = {
                window_id: {name: list(value) for name, value in buttons.items()}
                for window_id, buttons in self.button_registry.items()
            }
            with self.button_registry_path.open("w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存按钮配置失败: {e}")

    def get_session_id(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return f"group_{group_id}" if group_id else f"private_{event.get_sender_id()}"

    def _get_window_identifier(self, session: dict) -> Optional[str]:
        if session is None:
            return None
        if window_title := session.get("window_title"):
            return window_title
        window = session.get("window")
        try:
            return window.title if window else None
        except Exception:
            return None

    def _cleanup_pending_registration(self, session_id: str):
        if session_id in self.pending_button_registrations:
            del self.pending_button_registrations[session_id]

    async def _download_user_image(self, event: AstrMessageEvent, session_id: str):
        target_dir = self.annotation_dir
        filename = f"{session_id}_{int(time.time() * 1000)}.png"
        dest_path = target_dir / filename

        download_methods = ["download_images", "get_images", "get_image_paths"]
        for method_name in download_methods:
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    try:
                        result = method(dest_path.parent)
                    except TypeError:
                        result = method()
                    if inspect.isawaitable(result):
                        result = await result
                    if not result:
                        continue
                    if isinstance(result, Path):
                        candidate = result
                    elif isinstance(result, str):
                        candidate = result
                    elif isinstance(result, dict):
                        candidate = result.get("path") or result.get("file") or result.get("url")
                    else:
                        try:
                            iterator = iter(result)
                        except TypeError:
                            continue
                        try:
                            first = next(iterator)
                        except StopIteration:
                            continue
                        if isinstance(first, (list, tuple)) and first:
                            first = first[0]
                        if isinstance(first, dict):
                            candidate = first.get("path") or first.get("file") or first.get("url")
                        else:
                            candidate = first

                    if not candidate:
                        continue

                    if isinstance(candidate, Path):
                        return candidate
                    if isinstance(candidate, str):
                        if Path(candidate).exists():
                            return Path(candidate)
                        if candidate.startswith("http://") or candidate.startswith("https://"):
                            urllib.request.urlretrieve(candidate, dest_path)
                            return dest_path
                except Exception as e:
                    logger.debug(f"通过 {method_name} 下载图片失败: {e}")

        message_chain_attrs = ["message", "message_chain", "chain"]
        for attr in message_chain_attrs:
            chain = getattr(event, attr, None)
            if not chain:
                continue
            if not isinstance(chain, (list, tuple)):
                continue
            for element in chain:
                if isinstance(element, dict):
                    element_type = element.get("type") or element.get("Type")
                    if str(element_type).lower() != "image":
                        continue
                    data = element.get("data", {})
                    url = element.get("url") or data.get("url")
                    path = element.get("path") or data.get("path")
                    if path and Path(path).exists():
                        return Path(path)
                    if url and (url.startswith("http://") or url.startswith("https://")):
                        try:
                            urllib.request.urlretrieve(url, dest_path)
                            return dest_path
                        except Exception as e:
                            logger.debug(f"通过URL下载图片失败: {e}")

        return None

    def _analyze_annotation(self, base_path: Path, annotated_path: Path) -> Optional[Tuple[float, float]]:
        """Return normalized click coordinates extracted from a user-marked screenshot.

        Instead of doing a literal per-pixel diff (which breaks badly once the
        annotated image is recompressed or recoloured by the client), we look
        for regions that are *chromatically new* compared with the base
        screenshot. Pixels that suddenly become saturated/bright – i.e. typical
        paint strokes – are collected into a mask, denoised via a median filter
        and used to derive a bounding box. The box centre is converted to
        relative coordinates so the click can be replayed on different window
        sizes.
        """
        try:
            with Image.open(base_path) as base_image:
                base = base_image.convert("RGB")
            with Image.open(annotated_path) as marked_image:
                annotated = marked_image.convert("RGB")
        except Exception as e:
            logger.error(f"读取标注图片失败: {e}")
            return None

        if base.size != annotated.size:
            annotated = annotated.resize(base.size)

        base_hsv = base.convert("HSV")
        annotated_hsv = annotated.convert("HSV")
        mask = Image.new("L", base.size, 0)

        mask_data = []
        for (h0, s0, v0), (h1, s1, v1) in zip(base_hsv.getdata(), annotated_hsv.getdata()):
            hue_delta = min(abs(h1 - h0), 255 - abs(h1 - h0))
            sat_delta = max(0, s1 - s0)
            val_delta = abs(v1 - v0)

            if s1 > 80 and (sat_delta > 60 or hue_delta > 20 or val_delta > 70):
                mask_data.append(255)
            else:
                mask_data.append(0)

        mask.putdata(mask_data)
        mask = mask.filter(ImageFilter.MedianFilter(size=5))
        bbox = mask.getbbox()
        if not bbox:
            return None

        width, height = base.size
        left, top, right, bottom = bbox
        rel_x = max(0.0, min(1.0, ((left + right) / 2) / width))
        rel_y = max(0.0, min(1.0, ((top + bottom) / 2) / height))
        logger.debug(
            "Detected annotation bbox=%s within %sx%s, resolved to rel coords (%.4f, %.4f)",
            bbox,
            width,
            height,
            rel_x,
            rel_y,
        )
        return rel_x, rel_y

    async def _process_registration_image(self, event: AstrMessageEvent, session_id: str, registration: dict, image_path: Path):
        try:
            coords = self._analyze_annotation(Path(registration["screenshot_path"]), image_path)
        finally:
            if image_path.exists():
                try:
                    image_path.unlink()
                except Exception:
                    pass

        if not coords:
            await event.send(event.plain_result("未检测到标注区域，请在图片上用画笔涂抹按钮后重新发送。"))
            registration["timestamp"] = time.time()
            return

        session = self.game_sessions.get(session_id)
        if not session:
            await event.send(event.plain_result("当前会话已结束，按钮注册终止。"))
            self._cleanup_pending_registration(session_id)
            return

        try:
            await self._perform_click_and_capture(event, session, coords)
        except Exception as e:
            logger.error(f"根据标注执行点击失败: {e}")
            await event.send(event.plain_result(f"模拟点击失败: {e}"))
            registration["state"] = "await_image"
            registration["timestamp"] = time.time()
            return

        await event.send(event.plain_result("已根据标注完成一次点击。若已成功，请回复 1；若失败，请回复 2。"))
        registration["coords"] = coords
        registration["state"] = "await_confirm"
        registration["timestamp"] = time.time()

    async def _handle_pending_registration_message(self, event: AstrMessageEvent, session_id: str) -> bool:
        registration = self.pending_button_registrations.get(session_id)
        if not registration:
            return False

        if time.time() - registration.get("timestamp", 0) > 60:
            await event.send(event.plain_result("按钮注册流程已超时，请重新发送 /注册按钮 开始。"))
            self._cleanup_pending_registration(session_id)
            return True

        state = registration.get("state")
        message_text = (event.message_str or "").strip()

        if state == "await_image":
            image_path = await self._download_user_image(event, session_id)
            if image_path:
                await self._process_registration_image(event, session_id, registration, Path(image_path))
                return True
            if message_text:
                await event.send(event.plain_result("请发送标注后的图片，或使用 /注册按钮 重新开始。"))
                registration["timestamp"] = time.time()
                return True
            return False

        if state == "await_confirm":
            if message_text == "1":
                await event.send(event.plain_result("请回复按钮的名称。"))
                registration["state"] = "await_name"
                registration["timestamp"] = time.time()
                return True
            if message_text == "2":
                await event.send(event.plain_result("请重新在图片上标记按钮后再次发送。"))
                registration["state"] = "await_image"
                registration["timestamp"] = time.time()
                return True
            if message_text:
                await event.send(event.plain_result("请输入 1 表示成功或 2 表示失败。"))
                registration["timestamp"] = time.time()
                return True
            return False

        if state == "await_name":
            if not message_text:
                return False
            window_id = registration.get("window_id")
            if not window_id:
                await event.send(event.plain_result("未能确定所属窗口，按钮注册失败。"))
                self._cleanup_pending_registration(session_id)
                return True
            button_map = self.button_registry.setdefault(window_id, {})
            if message_text in button_map:
                await event.send(event.plain_result("已存在同名按钮，请换一个名字。"))
                registration["timestamp"] = time.time()
                return True
            if len(message_text) > 32:
                await event.send(event.plain_result("按钮名称过长，请控制在32个字符以内。"))
                registration["timestamp"] = time.time()
                return True
            coords = registration.get("coords")
            if not coords:
                await event.send(event.plain_result("未能获取按钮坐标，请重新执行 /注册按钮。"))
                self._cleanup_pending_registration(session_id)
                return True
            button_map[message_text] = coords
            self._save_button_registry()
            await event.send(event.plain_result(f"按钮 '{message_text}' 已注册成功！可使用 /点 {message_text} 来点击。"))
            self._cleanup_pending_registration(session_id)
            return True

        return False
    
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
            self._cleanup_pending_registration(session_id)

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
            self._cleanup_pending_registration(sid)

    async def _perform_click_and_capture(self, event: AstrMessageEvent, session: dict, coords: Tuple[float, float]):
        rel_x, rel_y = coords
        session_id = self.get_session_id(event)
        if self.mode == "remote":
            if not self.remote_server:
                raise Exception("远程服务未就绪。")
            await self.remote_server.remote_click(session_id, rel_x, rel_y)
            delay = self.config.get("screenshot_delay_seconds", 0.5)
            save_path_str = str(session["save_path"])
            await self.remote_server.remote_screenshot(session_id, save_path_str, delay)
            await event.send(event.image_result(save_path_str))
        elif self.mode == "local" and self.local_mode_available:
            window = session.get("window")
            if not window or not window.visible:
                raise Exception("窗口已关闭或不可见。")
            await asyncio.to_thread(local_operations.click_window, window, rel_x, rel_y)
            delay = self.config.get("screenshot_delay_seconds", 0.5)
            if delay > 0:
                await asyncio.sleep(delay)
            save_path_str = str(session["save_path"])
            await asyncio.to_thread(local_operations.screenshot_window, window, save_path_str)
            await event.send(event.image_result(save_path_str))
        else:
            raise Exception(f"当前模式 {self.mode} 不支持鼠标点击。")
        session["last_triggered_time"] = time.time()

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
            self.game_sessions[session_id] = {
                "window": window,
                "window_title": getattr(window, "title", window_title),
                "last_triggered_time": 0.0,
                "save_path": save_path,
            }
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
            self._cleanup_pending_registration(session_id)
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

    @gal_group.command("register_button", alias={"注册按钮"})
    async def register_button(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏，无法注册按钮。")
            event.stop_event()
            return

        window_id = self._get_window_identifier(session)
        if not window_id:
            yield event.plain_result("未能识别当前窗口，无法注册按钮。")
            event.stop_event()
            return

        yield event.plain_result("正在获取当前画面，请稍候...")
        await self._handle_game_action(event, session, take_screenshot=True)
        await event.send(event.plain_result("请在图片上涂抹需要点击的按钮区域，并重新发送图片。"))
        self.pending_button_registrations[session_id] = {
            "state": "await_image",
            "timestamp": time.time(),
            "screenshot_path": str(session["save_path"]),
            "window_id": window_id,
            "coords": None,
        }
        event.stop_event()

    @gal_group.command("click", alias={"点"})
    async def click_button(self, event: AstrMessageEvent, button_name: str):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏。")
            event.stop_event()
            return

        window_id = self._get_window_identifier(session)
        if not window_id:
            yield event.plain_result("未能获取窗口信息，无法执行点击。")
            event.stop_event()
            return

        button_map = self.button_registry.get(window_id, {})
        coords = button_map.get(button_name)
        if not coords:
            yield event.plain_result(f"未找到名为 '{button_name}' 的按钮，请先使用 /注册按钮 注册。")
            event.stop_event()
            return

        if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
            return

        try:
            await self._perform_click_and_capture(event, session, tuple(coords))
        except Exception as e:
            logger.error(f"点击已注册按钮失败: {e}")
            await event.send(event.plain_result(f"模拟点击失败: {e}"))
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
        if await self._handle_pending_registration_message(event, session_id):
            event.stop_event()
            return
        if session_id in self.game_sessions and event.message_str.strip().lower() in ["g", "gal"]:
            session = self.game_sessions[session_id]
            if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
                return
            session["last_triggered_time"] = time.time()
            quick_key = self.config.get("quick_advance_key", "space")
            await self._handle_game_action(event, session, key_to_press=quick_key)
            event.stop_event()
