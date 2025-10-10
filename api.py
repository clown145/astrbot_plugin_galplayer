import asyncio
import json
import websockets
import base64
import uuid
from astrbot.api import logger

class RemoteControlServer:
    def __init__(self, host, port, secret_token: str):
        self.host = host
        self.port = port
        self.secret_token = secret_token
        self.server = None
        self.client = None
        self.pending_screenshots = {}

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
                    if request_id in self.pending_screenshots:
                        if data.get("status") == "success":
                            screenshot_bytes = base64.b64decode(data.get("image_data"))
                            self.pending_screenshots[request_id].set_result(screenshot_bytes)
                        else:
                            error_message = data.get("error", "未知错误")
                            self.pending_screenshots[request_id].set_exception(Exception(f"远程操作失败: {error_message}"))
                except Exception as e:
                    logger.error(f"处理客户端消息时出错: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"与远程客户端的连接已关闭: {e}")
        finally:
            logger.info("远程客户端已断开连接。")
            self.client = None
            for future in self.pending_screenshots.values():
                if not future.done():
                    future.set_exception(Exception("客户端已断开连接"))
            self.pending_screenshots.clear()

    async def start(self):
        ten_mb = 10 * 1024 * 1024
        logger.info(f"正在启动远程控制服务器于 ws://{self.host}:{self.port}")
        self.server = await websockets.serve(self._handler, self.host, self.port, max_size=ten_mb)

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

    async def remote_start_session(self, session_id: str, window_title: str):
        """通知客户端为一个新的会话查找并绑定窗口"""
        await self._send_command({"action": "start_session", "session_id": session_id, "title": window_title})

    async def remote_stop_session(self, session_id: str):
        """通知客户端结束一个会话，释放资源"""
        await self._send_command({"action": "stop_session", "session_id": session_id})

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

    async def remote_screenshot(self, session_id: str, save_path: str, delay: float):
        """对指定会话的窗口截图"""
        request_id = str(uuid.uuid4())
        command = {"action": "screenshot", "session_id": session_id, "request_id": request_id, "delay": delay}
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_screenshots[request_id] = future

        try:
            await self._send_command(command)
            screenshot_bytes = await asyncio.wait_for(future, timeout=15.0)
            with open(save_path, "wb") as f:
                f.write(screenshot_bytes)
            return True
        except asyncio.TimeoutError:
            logger.error("等待远程截图超时。")
            raise Exception("等待远程截图超时。")
        finally:
            if request_id in self.pending_screenshots:
                del self.pending_screenshots[request_id]
