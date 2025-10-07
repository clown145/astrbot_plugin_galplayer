
import asyncio
import json
import websockets
import base64
import uuid
from astrbot.api import logger

class RemoteControlServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.server = None
        self.client = None
        self.pending_screenshots = {}

    async def _handler(self, websocket):
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

    async def remote_find_window(self, window_title: str):
        await self._send_command({"action": "find_window", "title": window_title})

    async def remote_press_key(self, key_name: str, method: str):
        await self._send_command({"action": "press_key", "key": key_name, "method": method})

    async def remote_screenshot(self, save_path: str, delay: float):
        request_id = str(uuid.uuid4())
        command = {"action": "screenshot", "request_id": request_id, "delay": delay}
        
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