from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Image, Poke

from .image_utils import ImageProcessingError, extract_click_point

IS_WINDOWS = sys.platform == 'win32'

local_operations = None
REMOTE_SUPPORT = False

try:
    from .api import RemoteControlServer
    REMOTE_SUPPORT = True
except ImportError:
    pass


PLUGIN_NAME = "astrbot_plugin_galplayer"
BUTTONS_FILE_NAME = "buttons.json"


def get_plugin_data_path() -> Path:
    return StarTools.get_data_dir(PLUGIN_NAME)


def load_buttons_data() -> Dict[str, Dict[str, Any]]:
    data_file = get_plugin_data_path() / BUTTONS_FILE_NAME
    if not data_file.exists():
        return {}
    try:
        with data_file.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, dict):
                return data
            logger.warning("buttons.json 内容格式不正确，已忽略。")
    except Exception as exc:
        logger.warning(f"读取按钮注册数据失败: {exc}")
    return {}


def save_buttons_data(data: Dict[str, Dict[str, Any]]) -> None:
    data_dir = get_plugin_data_path()
    data_dir.mkdir(parents=True, exist_ok=True)
    data_file = data_dir / BUTTONS_FILE_NAME
    with data_file.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

@dataclass
class RegistrationState:
    stage: str
    initiator_id: str
    window_title: str
    original_path: Path
    screenshot_size: Optional[Tuple[int, int]] = None
    annotated_path: Optional[Path] = None
    point_ratio: Optional[Tuple[float, float]] = None
    timeout_task: Optional[asyncio.Task] = None
    temp_paths: list[Path] = field(default_factory=list)
    last_event: Optional[AstrMessageEvent] = None

@register(PLUGIN_NAME, "随风潜入夜", "和群友一起推 Galgame", "1.3.0")
class GalgamePlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_sessions = {}
        self.buttons_data = load_buttons_data()
        self.registration_states: Dict[str, RegistrationState] = {}
        self.temp_img_dir = Path("data") / "tmp" / "galplayer"
        self.temp_img_dir.mkdir(parents=True, exist_ok=True)

        # Poke to g feature
        self.poke_to_g = self.config.get("poke_to_g", False)
        self.last_poke_time: Dict[str, float] = {}
        
        self.local_mode_available = False
        if IS_WINDOWS:
            try:
                from . import local_operations as lo
                globals()['local_operations'] = lo
                self.local_mode_available = True
            except ImportError as e:
                logger.critical(f"当前是 Windows 系统，但无法加载本地操作模块。请检查依赖。错误: {e}")
        
        self.mode = self.config.get("mode", "local")

        if self.mode == "local":
            if not IS_WINDOWS:
                logger.info("当前系统非 Windows，自动切换到远程模式。")
                self.mode = "remote"
            elif not self.local_mode_available:
                logger.warning("配置为本地模式，但本地模块加载失败，将强制切换到远程模式。")
                self.mode = "remote"
    
        self.remote_server = None
        if self.mode == "remote":
            if not REMOTE_SUPPORT:
                logger.error("远程模式需要安装 \"websockets\" 库，但无法导入，插件功能将被禁用。")
                self.mode = "disabled"
            else:
                secret_token = self.config.get("remote_secret_token")
                if not secret_token:
                    logger.error("远程模式已启用，但未在配置中设置 \"remote_secret_token\"，插件功能将被禁用。")
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
        for session_id in list(self.registration_states.keys()):
            self._clear_registration_state(session_id)
        logger.info("Galgame 插件已卸载。")

    def get_session_id(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        return f"group_{group_id}" if group_id else f"private_{event.get_sender_id()}"

    def _get_registration_timeout(self) -> float:
        try:
            timeout = float(self.config.get("registration_timeout_seconds", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        return max(10.0, timeout)

    def _copy_to_temp(self, source: Path, session_id: str, suffix: str) -> Path:
        destination = self.temp_img_dir / f"{session_id}_{suffix}_{uuid.uuid4().hex}.png"
        shutil.copy2(source, destination)
        return destination

    async def _extract_first_image_path(self, event: AstrMessageEvent) -> Optional[Path]:
        for component in event.get_messages():
            if isinstance(component, Image):
                try:
                    file_path = await component.convert_to_file_path()
                    return Path(file_path)
                except Exception as exc:
                    logger.warning(f"转换用户图片失败: {exc}")
                    return None
        return None

    def _get_window_title(self, session: Dict[str, Any]) -> Optional[str]:
        title = session.get("window_title")
        if title:
            return str(title)
        window = session.get("window")
        if window is not None:
            return getattr(window, "title", None)
        return None

    def _remove_temp_path(self, state: RegistrationState, path: Optional[Path]) -> None:
        if not path:
            return
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.debug(f"清理临时文件失败: {path}: {exc}")
        finally:
            if path in state.temp_paths:
                state.temp_paths.remove(path)

    def _clear_registration_state(self, session_id: str) -> None:
        state = self.registration_states.pop(session_id, None)
        if not state:
            return
        if state.timeout_task:
            state.timeout_task.cancel()
        for temp_path in list(state.temp_paths):
            self._remove_temp_path(state, temp_path)

    def _schedule_registration_timeout(self, session_id: str, event: AstrMessageEvent) -> None:
        state = self.registration_states.get(session_id)
        if not state:
            return
        if state.timeout_task:
            state.timeout_task.cancel()

        timeout_seconds = self._get_registration_timeout()

        async def timeout_coroutine():
            try:
                await asyncio.sleep(timeout_seconds)
                if self.registration_states.get(session_id) is not state:
                    return
                self._clear_registration_state(session_id)
                try:
                    await event.send(event.plain_result("按钮注册操作已超时，请重新发送 /注册按钮 开始。"))
                except Exception as exc:
                    logger.warning(f"发送注册超时提示失败: {exc}")
            except asyncio.CancelledError:
                pass

        state.last_event = event
        state.timeout_task = asyncio.create_task(timeout_coroutine())

    async def _perform_click_at_ratio(
        self,
        event: AstrMessageEvent,
        session: Dict[str, Any],
        ratio: Tuple[float, float],
    ) -> None:
        method = self.config.get("input_method", "PostMessage")
        x_ratio = max(0.0, min(1.0, ratio[0]))
        y_ratio = max(0.0, min(1.0, ratio[1]))
        session_id = self.get_session_id(event)

        if self.mode == "remote":
            if not self.remote_server or not self.remote_server.client:
                raise RuntimeError("远程客户端未连接。")
            await self.remote_server.remote_click(session_id, x_ratio, y_ratio, method)
        elif self.mode == "local" and self.local_mode_available:
            window = session.get("window")
            if not window:
                raise RuntimeError("找不到本地游戏窗口。")
            await asyncio.to_thread(
                local_operations.click_on_window,
                window,
                x_ratio,
                y_ratio,
                method,
            )
        else:
            raise RuntimeError(f"插件当前模式 ({self.mode}) 无法执行点击。")

    async def _maybe_handle_registration(self, event: AstrMessageEvent) -> bool:
        session_id = self.get_session_id(event)
        state = self.registration_states.get(session_id)
        if not state or state.last_event is event:
            return False

        sender_id = event.get_sender_id()
        if sender_id != state.initiator_id:
            await event.send(event.plain_result("当前有其他用户正在注册按钮，请等待该流程完成。"))
            return True

        message_text = event.message_str.strip()
        normalized_text = message_text.replace("１", "1").replace("２", "2").lower()
        if normalized_text in {"取消", "cancel"}:
            self._clear_registration_state(session_id)
            await event.send(event.plain_result("已取消按钮注册流程。"))
            return True

        session = self.game_sessions.get(session_id)
        if session is None:
            self._clear_registration_state(session_id)
            await event.send(event.plain_result("当前没有正在运行的游戏，会话已结束，注册流程中止。"))
            return True

        if state.stage == "awaiting_mark":
            image_path = await self._extract_first_image_path(event)
            if not image_path:
                await event.send(event.plain_result("请发送带有标注的截图，以便识别按钮位置。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            annotated_copy = self._copy_to_temp(image_path, session_id, "annotated")
            state.temp_paths.append(annotated_copy)

            try:
                (centroid_x, centroid_y), (width, height) = extract_click_point(
                    state.original_path, annotated_copy
                )
            except ImageProcessingError as exc:
                self._remove_temp_path(state, annotated_copy)
                await event.send(event.plain_result(f"标注解析失败：{exc}。请使用更明显的颜色或更粗的线条重新标注。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            width = max(width, 1)
            height = max(height, 1)
            ratio_x = max(0.0, min(1.0, centroid_x / max(width - 1, 1)))
            ratio_y = max(0.0, min(1.0, centroid_y / max(height - 1, 1)))

            state.annotated_path = annotated_copy
            state.point_ratio = (ratio_x, ratio_y)
            state.screenshot_size = (width, height)
            state.stage = "awaiting_confirm"

            try:
                await self._perform_click_at_ratio(event, session, state.point_ratio)
            except Exception as exc:
                logger.error(f"执行注册点击失败: {exc}", exc_info=True)
                await event.send(event.plain_result(f"尝试执行点击时出现错误：{exc}。流程已取消。"))
                self._clear_registration_state(session_id)
                return True

            delay_seconds = self.config.get("screenshot_delay_seconds", 0.5)
            try:
                delay_seconds = float(delay_seconds)
            except (TypeError, ValueError):
                delay_seconds = 0.5
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

            await self._handle_game_action(event, session, key_to_press=None, take_screenshot=True)
            await event.send(event.plain_result("已尝试点击，请回复 1 表示成功，2 表示失败。"))
            self._schedule_registration_timeout(session_id, event)
            return True

        if state.stage == "awaiting_confirm":
            if normalized_text == "1":
                state.stage = "awaiting_name"
                self._schedule_registration_timeout(session_id, event)
                await event.send(event.plain_result("点击成功！请发送按钮名称（建议不要包含空格）。"))
                return True

            if normalized_text == "2":
                if state.annotated_path:
                    self._remove_temp_path(state, state.annotated_path)
                    state.annotated_path = None
                state.point_ratio = None
                state.screenshot_size = None

                await self._handle_game_action(event, session, key_to_press=None, take_screenshot=True)

                save_path = session.get("save_path")
                if save_path:
                    new_original = self._copy_to_temp(Path(save_path), session_id, "orig")
                    state.temp_paths.append(new_original)
                    self._remove_temp_path(state, state.original_path)
                    state.original_path = new_original

                state.stage = "awaiting_mark"
                self._schedule_registration_timeout(session_id, event)
                await event.send(event.plain_result("请在最新截图上重新标注目标位置并发送给我。"))
                return True

            await event.send(event.plain_result("请输入 1 表示成功，或 2 表示失败。"))
            self._schedule_registration_timeout(session_id, event)
            return True

        if state.stage == "awaiting_name":
            button_name = message_text.strip()
            if not button_name:
                await event.send(event.plain_result("按钮名称不能为空，请重新输入。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            if any(ch.isspace() for ch in button_name):
                await event.send(event.plain_result("按钮名称不能包含空白字符，请重新输入。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            if len(button_name) > 32:
                await event.send(event.plain_result("按钮名称过长，请控制在 32 个字符以内。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            if not state.point_ratio:
                await event.send(event.plain_result("缺少坐标信息，请重新开始注册流程。"))
                self._clear_registration_state(session_id)
                return True

            window_buttons = self.buttons_data.setdefault(state.window_title, {})
            if button_name in window_buttons:
                await event.send(event.plain_result("该按钮名称已存在，请换一个名称。"))
                self._schedule_registration_timeout(session_id, event)
                return True

            window_buttons[button_name] = {
                "x_ratio": state.point_ratio[0],
                "y_ratio": state.point_ratio[1],
            }
            save_buttons_data(self.buttons_data)
            await event.send(
                event.plain_result(
                    f"按钮 '{button_name}' 注册成功！之后可使用 /点 {button_name} 执行点击。"
                )
            )
            self._clear_registration_state(session_id)
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
                save_path_str = str(session["save_path"])
                input_method = self.config.get("input_method", "PostMessage")
                use_dxcam = (
                    (input_method == "SendInput" and bool(self.config.get("foreground_use_dxcam", True)))
                    or (input_method == "PostMessage" and bool(self.config.get("background_use_dxcam", False)))
                )
                if use_dxcam and hasattr(local_operations, "screenshot_window_dxcam"):
                    await asyncio.to_thread(local_operations.screenshot_window_dxcam, window, save_path_str, input_method == "SendInput")
                else:
                    await asyncio.to_thread(local_operations.screenshot_window, window, save_path_str)
                await event.send(event.image_result(save_path_str))
        except Exception as e:
            logger.error(f"处理本地游戏动作时出错: {e}")
            await event.send(event.plain_result("游戏窗口似乎已经关闭或出现问题，游戏已自动结束。"))
            if (session_id := self.get_session_id(event)) in self.game_sessions:
                del self.game_sessions[session_id]
            self._clear_registration_state(session_id)

    async def _handle_remote_action(self, event: AstrMessageEvent, session: dict, session_id: str, key_to_press: str, take_screenshot: bool):
        if not self.remote_server:
            await event.send(event.plain_result("远程客户端未连接。请确保远程脚本正在运行并已连接。"))
            return
        try:
            if key_to_press:
                input_method = self.config.get("input_method", "PostMessage")
                await self.remote_server.remote_press_key(session_id, key_to_press, input_method)
            if take_screenshot:
                delay = self.config.get("screenshot_delay_seconds", 0.5) if key_to_press else 0
                # 确保保存扩展名与编码格式一致（仅远程）
                desired_ext = "jpg" if bool(self.config.get("remote_use_jpeg", False)) else "png"
                current_path: Path = session["save_path"]
                if current_path.suffix.lower().lstrip(".") != desired_ext:
                    new_path = current_path.with_suffix(f".{desired_ext}")
                    session["save_path"] = new_path
                save_path_str = str(session["save_path"])
                input_method = self.config.get("input_method", "PostMessage")
                use_dxcam = (
                    (input_method == "SendInput" and bool(self.config.get("foreground_use_dxcam", True)))
                    or (input_method == "PostMessage" and bool(self.config.get("background_use_dxcam", False)))
                )
                # 是否使用 JPEG 由配置控制（仅影响远程编码格式）
                image_format = "jpeg" if bool(self.config.get("remote_use_jpeg", False)) else "png"
                bring_to_front = (input_method == "SendInput")
                await self.remote_server.remote_screenshot(session_id, save_path_str, delay, use_dxcam, image_format, bring_to_front)
                await event.send(event.image_result(save_path_str))
        except ConnectionError:
            await event.send(event.plain_result("远程客户端未连接。请确保远程脚本正在运行并已连接。"))
        except Exception as e:
            # 某些环境下会抛出 "Timed out" 或自定义的超时提示；这类情况不再向用户报错，仅记录为警告
            msg = str(e).strip().lower()
            if "timed out" in msg or "超时" in msg:
                logger.warning(f"远程截图等待超时（已忽略）：{e}")
                return
            logger.error(f"处理远程游戏动作时出错: {e}")
            await event.send(event.plain_result(f"远程操作失败: {e}。会话保持不变，请稍后重试 /gal resend 或继续操作。"))
    @filter.command_group("gal", alias={"g"})
    async def gal_group(self): ...

    @gal_group.command("start", alias={"开始游戏"})
    async def start_game(self, event: AstrMessageEvent, window_title: str):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            yield event.plain_result("本群聊已在游戏中！请先用 /gal stop 停止。")
            return
        
        # 保存路径按模式/配置决定扩展名，远程可选 JPEG
        if self.mode == "remote":
            use_jpeg = bool(self.config.get("remote_use_jpeg", False))
            ext = "jpg" if use_jpeg else "png"
            save_path = self.temp_img_dir / f"{session_id}.{ext}"
        else:
            save_path = self.temp_img_dir / f"{session_id}.png"

        if self.mode == "remote":
            if not self.remote_server or not self.remote_server.client:
                yield event.plain_result("远程客户端未连接。请在远程电脑上运行客户端脚本。")
                return
            yield event.plain_result(f"正在通知远程客户端查找窗口: '{window_title}'...")
            try:
                await self.remote_server.remote_start_session(session_id, window_title)
                self.game_sessions[session_id] = {
                    "window_title": window_title,
                    "last_triggered_time": 0.0,
                    "save_path": save_path,
                }
                logger.info(f"会话 {session_id} 开始远程游戏，窗口: {window_title}")
                yield event.plain_result("远程模式已连接，启动成功。")
                await self._handle_remote_action(
                    event,
                    self.game_sessions[session_id],
                    session_id,
                    key_to_press=None,
                    take_screenshot=True,
                )
            except Exception as e:
                yield event.plain_result(f"启动远程游戏失败: {e}")

        elif self.mode == "local" and self.local_mode_available:
            yield event.plain_result(f"正在查找本地窗口: '{window_title}'...")
            window = await asyncio.to_thread(local_operations.find_game_window, window_title)
            if not window:
                yield event.plain_result(f"找不到窗口 '{window_title}'。请确认游戏已运行且标题匹配。")
                return
            self.game_sessions[session_id] = {
                "window": window,
                "window_title": getattr(window, "title", window_title),
                "last_triggered_time": 0.0,
                "save_path": save_path,
            }
            logger.info(f"会话 {session_id} 开始本地游戏，窗口: {window.title}")
            yield event.plain_result("本地游戏开始！这是当前画面：")
            await self._handle_local_action(
                event,
                self.game_sessions[session_id],
                key_to_press=None,
                take_screenshot=True,
            )
        else:
            yield event.plain_result(
                f"插件当前模式 ({self.mode}) 无法启动游戏。请检查配置和运行环境。"
            )
        event.stop_event()

    @gal_group.command("stop", alias={"停止"})
    async def stop_game(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            if self.mode == "remote" and self.remote_server and self.remote_server.client:
                await self.remote_server.remote_stop_session(session_id) # 通知客户端清理
            
            if (save_path := self.game_sessions[session_id]['save_path']).exists():
                save_path.unlink()
            del self.game_sessions[session_id]
            self._clear_registration_state(session_id)
            yield event.plain_result("游戏已停止。")
        else:
            yield event.plain_result("当前没有正在进行的游戏。")
        event.stop_event()

    
    @filter.command("注册按钮", alias={"register_button"})
    async def register_button(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏，请先使用 /gal start <窗口标题>。")
            event.stop_event()
            return

        if session_id in self.registration_states:
            yield event.plain_result("当前已有按钮注册流程进行中，请先完成或等待超时。")
            event.stop_event()
            return

        window_title = self._get_window_title(session)
        if not window_title:
            yield event.plain_result("无法确认当前窗口，请重新开始游戏后再试。")
            event.stop_event()
            return

        yield event.plain_result("正在获取当前画面，请稍候...")
        await self._handle_game_action(event, session, key_to_press=None, take_screenshot=True)

        save_path = session.get("save_path")
        save_path = Path(save_path) if save_path else None
        if not save_path or not save_path.exists():
            yield event.plain_result("截图失败，请稍后重试。")
            event.stop_event()
            return

        original_copy = self._copy_to_temp(save_path, session_id, "orig")
        state = RegistrationState(
            stage="awaiting_mark",
            initiator_id=event.get_sender_id(),
            window_title=window_title,
            original_path=original_copy,
        )
        state.temp_paths.append(original_copy)
        self.registration_states[session_id] = state
        self.buttons_data.setdefault(window_title, {})
        self._schedule_registration_timeout(session_id, event)

        yield event.plain_result(
            "请直接在机器人发送的截图上标注需要点击的按钮位置，并在 60 秒内发回。"
        )
        event.stop_event()

    @filter.command("点", alias={"click"})
    async def click_registered_button(self, event: AstrMessageEvent, button_name: str):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏，请先使用 /gal start 启动。")
            event.stop_event()
            return

        if session_id in self.registration_states:
            yield event.plain_result("按钮注册流程进行中，暂时无法执行点击。")
            event.stop_event()
            return

        window_title = self._get_window_title(session)
        if not window_title:
            yield event.plain_result("无法确认当前窗口，请重新开始游戏。")
            event.stop_event()
            return

        buttons = self.buttons_data.get(window_title, {})
        mapping = buttons.get(button_name)
        if not mapping:
            yield event.plain_result(f"当前窗口未找到名为 '{button_name}' 的按钮。")
            event.stop_event()
            return

        now = time.time()
        if now - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
            yield event.plain_result("指令触发过于频繁，请稍后再试。")
            event.stop_event()
            return
        session["last_triggered_time"] = now

        yield event.plain_result(f"正在尝试点击按钮 '{button_name}'...")
        try:
            await self._perform_click_at_ratio(
                event,
                session,
                (
                    float(mapping.get("x_ratio", 0.0)),
                    float(mapping.get("y_ratio", 0.0)),
                ),
            )
        except Exception as exc:
            logger.error(f"执行已注册按钮点击失败: {exc}", exc_info=True)
            yield event.plain_result(f"执行点击时出现错误：{exc}")
            event.stop_event()
            return

        screenshot_on_click = self.config.get("screenshot_on_click", True)
        if isinstance(screenshot_on_click, str):
            screenshot_on_click = screenshot_on_click.lower() not in {"0", "false", "no"}
        if screenshot_on_click:
            delay_seconds = self.config.get("screenshot_delay_seconds", 0.5)
            try:
                delay_seconds = float(delay_seconds)
            except (TypeError, ValueError):
                delay_seconds = 0.5
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self._handle_game_action(event, session, key_to_press=None, take_screenshot=True)
        else:
            await event.send(event.plain_result("点击动作已发送。"))
        event.stop_event()

    @filter.command("按钮列表", alias={"button_list"})
    async def list_registered_buttons(self, event: AstrMessageEvent):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏，请先使用 /gal start 启动。")
            event.stop_event()
            return

        window_title = self._get_window_title(session)
        if not window_title:
            yield event.plain_result("无法确认当前窗口，请重新开始游戏。")
            event.stop_event()
            return

        buttons = self.buttons_data.get(window_title, {})
        if not buttons:
            yield event.plain_result("当前窗口尚未注册任何按钮。")
        else:
            lines = "\n".join(f"- {name}" for name in sorted(buttons.keys()))
            yield event.plain_result(f"当前窗口已注册的按钮:\n{lines}")
        event.stop_event()

    @filter.command("删除按钮", alias={"remove_button"})
    async def remove_registered_button(self, event: AstrMessageEvent, button_name: str):
        session_id = self.get_session_id(event)
        session = self.game_sessions.get(session_id)
        if not session:
            yield event.plain_result("当前没有正在进行的游戏，请先使用 /gal start 启动。")
            event.stop_event()
            return

        window_title = self._get_window_title(session)
        if not window_title:
            yield event.plain_result("无法确认当前窗口，请重新开始游戏。")
            event.stop_event()
            return

        buttons = self.buttons_data.get(window_title, {})
        if button_name not in buttons:
            yield event.plain_result(f"未找到名为 '{button_name}' 的按钮。")
            event.stop_event()
            return

        del buttons[button_name]
        if not buttons:
            self.buttons_data.pop(window_title, None)
        save_buttons_data(self.buttons_data)
        yield event.plain_result(f"按钮 '{button_name}' 已删除。")
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

    @filter.command("输入", alias={"输", "type"})
    async def type_key(self, event: AstrMessageEvent, key_name: str):
        key_aliases = {"空格": "space", "回车": "enter", "上": "up", "下": "down", "左": "left", "右": "right"}
        actual_key_name = key_aliases.get(key_name, key_name)
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
        help_text = (
            f"🎮 Galgame 插件帮助 (当前模式: {self.mode.upper()}) 🎮\n"
            "--------------------\n"
            f"按键模式: {input_method}\n"
            "指令:\n"
            "  /gal start <窗口标题>\n"
            "  /gal stop\n"
            "  /gal resend\n"
            "  /输入 <按键> (别名: /输, /type，支持中文别名: 上/下/左/右/空格...)\n"
            "  /注册按钮 — 发送当前截图并引导标注\n"
            "  /点 <按钮名> — 点击已注册按钮\n"
            "  /按钮列表 — 查看当前窗口的按钮\n"
            "  /删除按钮 <按钮名>\n"
            "\n"
            "快捷指令:\n"
            f"  g 或 gal (快捷键: '{quick_key}')"
        )
        yield event.plain_result(help_text)
        event.stop_event()

    async def _handle_g_command(self, event: AstrMessageEvent):
        """处理 'g' 或 'gal' 指令或戳一戳事件，推进游戏。"""
        session_id = self.get_session_id(event)
        if session_id in self.game_sessions:
            session = self.game_sessions[session_id]

            # 检查游戏动作的统一冷却时间
            if time.time() - session.get("last_triggered_time", 0) < self.config.get("cooldown_seconds", 3.0):
                return
            session["last_triggered_time"] = time.time()

            quick_key = self.config.get("quick_advance_key", "space")
            await self._handle_game_action(event, session, key_to_press=quick_key)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """监听并响应戳一戳事件。"""
        if not self.poke_to_g:
            return

        # 尝试从特定平台的 message_obj 中获取信息
        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return

        raw_message = getattr(message_obj, "raw_message", None)
        message_chain = getattr(message_obj, "message", None)

        # 严格按照 1.py 的方式进行检查
        if (
            not raw_message
            or not message_chain
            or not isinstance(message_chain[0], Poke)
        ):
            return

        target_id = raw_message.get("target_id")
        user_id = raw_message.get("user_id")

        if not user_id or not target_id:
            logger.debug("Poke 事件中缺少 user_id 或 target_id")
            return

        # 检查戳的是否是Bot自己
        if str(target_id) != event.get_self_id():
            return

        # 戳一戳冷却
        sender_id_str = str(user_id)
        now = time.time()
        cooldown = self.config.get("cooldown_seconds", 3.0)
        last_poke = self.last_poke_time.get(sender_id_str, 0)
        if now - last_poke < cooldown:
            return
        self.last_poke_time[sender_id_str] = now

        logger.info(f"戳一戳事件已触发'g'指令，来自用户 {sender_id_str}")
        await self._handle_g_command(event)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_advance_message(self, event: AstrMessageEvent):
        if await self._maybe_handle_registration(event):
            event.stop_event()
            return

        if event.message_str.strip().lower() in ["g", "gal"]:
            await self._handle_g_command(event)
            event.stop_event()
