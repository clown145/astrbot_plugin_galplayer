import asyncio
import base64
import json
import uuid

import websockets

from astrbot.api import logger


class RemoteControlServer:
    def __init__(self, host, port, secret_token: str):
        self.host = host
        self.port = port
        self.secret_token = secret_token
        self.server = None
        self.client = None
        self.pending_requests = {}

    def _resolve_pending_request(self, request_id: str, payload: dict) -> bool:
        future = self.pending_requests.get(request_id)
        if not future:
            return False

        if future.done():
            self.pending_requests.pop(request_id, None)
            return True

        status = payload.get("status")
        if status == "success":
            future.set_result(payload)
        else:
            error_message = payload.get("error", "未知错误")
            future.set_exception(Exception(f"远程操作失败: {error_message}"))
        return True

    async def _handler(self, websocket):
        try:
            auth_message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            auth_data = json.loads(auth_message)
            
            if auth_data.get("type") == "auth" and auth_data.get("token") == self.secret_token:
                logger.info(f"客户端 {websocket.remote_address} 验证成功。")
                await websocket.send(json.dumps({"status": "auth_success"}))
            else:
                logger.warning(f"客户端 {websocket.remote_address} 验证失败：密钥不匹配。")
                await websocket.close()
                return
        except Exception as e:
            logger.warning(f"与客户端 {websocket.remote_address} 的认证过程中断: {e}")
            await websocket.close()
            return

        logger.info(f"远程客户端已连接: {websocket.remote_address}")
        self.client = websocket
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    request_id = data.get("request_id")
                    if request_id and self._resolve_pending_request(request_id, data):
                        if data.get("status") == "success" and data.get("image_data"):
                            logger.debug(f"收到远程截图成功: req={request_id}")
                        elif data.get("status") != "success":
                            logger.debug(f"收到远程请求错误: req={request_id}, err={data.get('error', '未知错误')}")
                except Exception as e:
                    logger.error(f"处理客户端消息时出错: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"与远程客户端的连接已关闭: {e}")
        finally:
            logger.info("远程客户端已断开连接。")
            self.client = None
            for future in self.pending_requests.values():
                if not future.done():
                    future.set_exception(Exception("客户端已断开连接"))
            self.pending_requests.clear()

    async def start(self):
        logger.info(f"正在启动远程控制服务器于 ws://{self.host}:{self.port}（未启用 TLS）")
        # 取消消息大小上限，避免高分辨率截图触发超限断开
        self.server = await websockets.serve(self._handler, self.host, self.port, max_size=None)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("远程控制服务器已关闭。")

    async def _send_command(self, command: dict):
        if not self.client:
            raise ConnectionError("远程客户端未连接。")
        try:
            await self.client.send(json.dumps(command))
        except websockets.exceptions.ConnectionClosed:
            raise ConnectionError("远程客户端连接已断开。")

    async def _send_request(self, command: dict, timeout: float = 10.0) -> dict:
        request_id = str(uuid.uuid4())
        command = dict(command)
        command["request_id"] = request_id

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_requests[request_id] = future

        try:
            await self._send_command(command)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending_requests.pop(request_id, None)

    async def remote_start_session(self, session_id: str, window_title: str):
        """通知客户端为一个新的会话查找并绑定窗口"""
        await self._send_request(
            {"action": "start_session", "session_id": session_id, "title": window_title},
            timeout=10.0,
        )

    async def remote_stop_session(self, session_id: str):
        """通知客户端结束一个会话，释放资源"""
        await self._send_request({"action": "stop_session", "session_id": session_id}, timeout=5.0)

    async def remote_press_key(self, session_id: str, key_name: str, method: str):
        """向指定会话的窗口按键"""
        await self._send_command({"action": "press_key", "session_id": session_id, "key": key_name, "method": method})

    async def remote_click(self, session_id: str, x_ratio: float, y_ratio: float, method: str):
        """在指定会话的窗口执行一次鼠标左键点击"""
        await self._send_command(
            {
                "action": "click",
                "session_id": session_id,
                "x_ratio": x_ratio,
                "y_ratio": y_ratio,
                "method": method,
            }
        )

    async def remote_screenshot(self, session_id: str, save_path: str, delay: float, use_dxcam: bool = False, image_format: str = "jpeg", bring_to_front: bool = False):
        """对指定会话的窗口截图。
        use_dxcam: 是否在远程端使用 dxcam 进行截图（失败会由客户端回退）。
        """
        command = {
            "action": "screenshot",
            "session_id": session_id,
            "delay": delay,
            "use_dxcam": bool(use_dxcam),
            "format": image_format,
            "bring_to_front": bool(bring_to_front),
        }

        try:
            logger.debug(
                f"下发远程截图指令: session={session_id}, use_dxcam={use_dxcam}, fmt={image_format}, delay={delay}"
            )
            response = await self._send_request(command, timeout=60.0)
            screenshot_bytes = base64.b64decode(response.get("image_data", ""))
            with open(save_path, "wb") as f:
                f.write(screenshot_bytes)
            logger.debug(f"远程截图完成: session={session_id}, bytes={len(screenshot_bytes)} -> {save_path}")
            return True
        except asyncio.TimeoutError:
            logger.error(f"等待远程截图超时: session={session_id}")
            raise Exception("等待远程截图超时。")
