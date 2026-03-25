"""
API模块：负责从iTick获取伦敦金实时价格

本模块实现：
1. GoldPrice 数据类：标准化价格数据结构
2. ITickConfig 配置类：itick API参数封装
3. GoldPriceAPI 类：封装itick API请求逻辑

iTick API说明：
- REST API: https://api.itick.org/forex/quote?region={region}&code={code}
- WebSocket: wss://api.itick.org/forex (可选实时推送模式)
- 认证方式: Header中传递 token
- 数据格式: JSON

支持的贵金属交易对：
- XAUUSD: 黄金兑美元
- XAGUSD: 白银兑美元
- 其他可配置的贵金属品种
"""

import asyncio
import json
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from threading import Thread, Event
import aiohttp
from astrbot.api import logger

from .constants import MIN_PRICE, MAX_PRICE, MAX_TIMESTAMP_DIFF, MAX_CALLBACK_QUEUE_SIZE


@dataclass
class GoldPrice:
    """
    黄金价格数据标准化模型

    Attributes:
        price: 黄金价格（美元/盎司），保留2位小数（使用Decimal确保精度）
        timestamp: 报价时间（服务器返回的时间戳）
        source: 数据来源标识（用于日志和展示）

    校验规则：
    - 价格必须为正数
    - 价格必须在合理区间内（500-5000美元）
    - 报价时间必须在60秒内新鲜
    """
    price: Decimal
    timestamp: datetime
    source: str

    def is_valid(self) -> bool:
        """
        验证价格数据合法性

        三重校验：
        1. 价格正数检查
        2. 价格合理区间检查（防止接口返回脏数据）
        3. 时间戳新鲜度检查（确保数据实时性）

        Returns:
            True: 数据合法
            False: 数据异常
        """
        if self.price <= 0:
            return False

        if not (MIN_PRICE <= self.price <= MAX_PRICE):
            return False

        diff = (datetime.now() - self.timestamp).total_seconds()
        if diff > MAX_TIMESTAMP_DIFF:
            return False

        return True


@dataclass
class ITickConfig:
    """
    iTick API配置参数

    Attributes:
        token: iTick API访问令牌（必填）
        region: 地区代码，默认 "GB"（外汇市场）
        code: 贵金属交易代码，默认 "XAUUSD"（黄金兑美元）
        use_websocket: 是否使用WebSocket实时推送模式，默认 False（使用REST）
        websocket_ping_interval: WebSocket心跳间隔（秒），默认30秒
        request_timeout: HTTP请求超时时间（秒），默认10秒
    """
    token: str = ""
    region: str = "GB"
    code: str = "XAUUSD"
    use_websocket: bool = False
    websocket_ping_interval: int = 30
    request_timeout: int = 10

    def validate(self) -> Tuple[bool, str]:
        """
        验证配置参数

        Returns:
            Tuple[bool, str]: (是否有效, 错误信息)
        """
        if not self.token or not self.token.strip():
            return False, "iTick API Token未配置，请在插件设置中填写"

        if not self.code or not self.code.strip():
            return False, "交易代码不能为空"

        if self.region not in ("GB", "HK", "US", "CN"):
            logger.warning(f"地区代码 {self.region} 可能不受支持")

        return True, ""


class GoldPriceAPI:
    """
    黄金价格API客户端（基于iTick）

    设计要点：
    1. 双模式支持：REST轮询 + WebSocket实时推送
    2. 连接复用：使用aiohttp.ClientSession复用连接
    3. 超时控制：可配置的请求超时时间
    4. 异常隔离：每种异常单独捕获，防止一处异常影响全局
    5. WebSocket心跳：自动维持连接活跃

    请求流程（REST模式）：
    fetch_price() -> _fetch_itick_rest() -> 返回GoldPrice或None

    请求流程（WebSocket模式）：
    start_websocket() -> 连接认证 -> 订阅 -> 接收消息 -> on_price_callback
    """

    BASE_URL = "https://api.itick.org"
    WS_URL = "wss://api.itick.org/forex"

    DEFAULT_HEADERS = {
        "accept": "application/json"
    }

    def __init__(self, config: Optional[ITickConfig] = None):
        """
        初始化API客户端

        Args:
            config: iTick API配置，None时使用默认配置（需要外部设置token）
        """
        self.config = config or ITickConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_thread: Optional[Thread] = None
        self._ws_running = Event()
        self._ws_connected = Event()
        self._latest_price: Optional[GoldPrice] = None
        self._price_callback: Optional[callable] = None
        self._callback_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_CALLBACK_QUEUE_SIZE)
        self._callback_task: Optional[asyncio.Task] = None
        self._websocket_module: Optional[object] = None
        self._websocket_available: Optional[bool] = None
        self._ws_error_occurred: bool = False

    def _check_websocket_available(self) -> bool:
        """检查WebSocket库是否可用"""
        if self._websocket_available is not None:
            return self._websocket_available

        try:
            import websocket
            self._websocket_module = websocket
            self._websocket_available = True
            logger.debug("WebSocket库检查通过")
        except ImportError:
            self._websocket_module = None
            self._websocket_available = False
            logger.warning("websocket-client库未安装，WebSocket功能不可用")

        return self._websocket_available

    def set_config(self, config: ITickConfig) -> None:
        """
        设置或更新配置

        Args:
            config: 新的配置对象
        """
        self.config = config

    def set_price_callback(self, callback: callable) -> None:
        """
        设置价格回调函数（WebSocket模式）

        Args:
            callback: 回调函数，签名为 callback(price: GoldPrice) 或 async def callback(price: GoldPrice)
        """
        self._price_callback = callback

    def _parse_price_data(self, price_data: dict, source_suffix: str = "") -> Optional[GoldPrice]:
        """
        统一的价格数据解析方法

        Args:
            price_data: 价格数据字典，包含 ld（价格）和 t（时间戳）
            source_suffix: 来源标识后缀，如 "-WS" 表示WebSocket

        Returns:
            GoldPrice对象或None（解析失败时）
        """
        latest_price = price_data.get("ld")
        if latest_price is None:
            return None

        try:
            price = Decimal(str(latest_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            timestamp_ms = price_data.get("t")
            if timestamp_ms:
                try:
                    timestamp = datetime.fromtimestamp(int(timestamp_ms) / 1000)
                except (ValueError, OSError, TypeError):
                    timestamp = datetime.now()
            else:
                timestamp = datetime.now()

            source = f"iTick{source_suffix}({self.config.region}/{self.config.code})"
            return GoldPrice(price=price, timestamp=timestamp, source=source)
        except (ValueError, TypeError) as e:
            logger.warning(f"价格数据解析失败: {e}")
            return None

    def _start_callback_processor(self) -> bool:
        """
        启动回调处理器（在事件循环中调用）

        Returns:
            True: 启动成功
            False: 启动失败（无运行中的事件循环或任务已存在）
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("无法获取运行中的事件循环，跳过回调处理器初始化")
            return False

        if self._callback_task is None or self._callback_task.done():
            self._callback_task = loop.create_task(self._process_callbacks())
            logger.debug("回调处理器任务已创建")

        return True

    async def _process_callbacks(self) -> None:
        """处理回调队列中的异步任务"""
        while self._ws_running.is_set():
            try:
                callback, price = await asyncio.wait_for(
                    self._callback_queue.get(),
                    timeout=1.0
                )
                if asyncio.iscoroutinefunction(callback):
                    await callback(price)
                else:
                    await asyncio.to_thread(callback, price)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"回调处理异常: {e}")

    def _get_headers(self) -> dict:
        """获取HTTP请求头（包含认证token）"""
        headers = self.DEFAULT_HEADERS.copy()
        if self.config.token:
            headers["token"] = self.config.token
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        获取或创建HTTP会话

        连接复用策略：
        - 单个实例复用同一个session
        - 自动处理session过期重建
        - 设置可配置的超时时间

        Returns:
            可用的aiohttp.ClientSession实例
        """
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self._get_headers())
        return self._session

    async def close(self) -> None:
        """
        关闭HTTP会话和WebSocket连接

        调用时机：
        - 插件卸载时
        - 监控任务停止时
        确保释放所有网络资源
        """
        await asyncio.to_thread(self.stop_websocket)

        if self._callback_task and not self._callback_task.done():
            self._callback_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._callback_task),
                    timeout=2.0
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                self._callback_task = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("iTick API客户端已关闭")

    async def fetch_price(self) -> Optional[GoldPrice]:
        """
        获取黄金价格的统一入口

        实现逻辑：
        1. 优先使用REST API获取价格
        2. 解析响应数据
        3. 转换为GoldPrice对象

        Returns:
            GoldPrice对象：获取成功
            None：获取失败
        """
        return await self._fetch_itick_rest()

    async def _fetch_itick_rest(self) -> Optional[GoldPrice]:
        """
        iTick REST API获取实时报价

        API端点：
        GET /forex/quote?region={region}&code={code}

        响应字段：
        - ld: 最新价（latest price）
        - o: 开盘价（open price）
        - h: 最高价（high price）
        - l: 最低价（low price）
        - t: 时间戳（timestamp）

        Returns:
            GoldPrice对象：成功
            None：失败（包括网络超时、响应格式错误、数据为空等）
        """
        valid, error = self.config.validate()
        if not valid:
            logger.error(f"iTick配置无效: {error}")
            return None

        try:
            session = await self._get_session()

            url = f"{self.BASE_URL}/forex/quote"
            params = {
                "region": self.config.region,
                "code": self.config.code
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning(f"iTick API返回状态码 {resp.status}: {error_text}")
                    return None

                data = await resp.json()

                if data.get("code") != 0:
                    error_msg = data.get("msg", "未知错误")
                    logger.warning(f"iTick API业务错误: {error_msg}")
                    return None

                price_data = data.get("data", {})
                if not price_data:
                    logger.warning("iTick API返回数据为空")
                    return None

                gold_price = self._parse_price_data(price_data)
                if gold_price is None:
                    logger.warning("iTick API价格数据解析失败")
                    return None

                self._latest_price = gold_price
                logger.debug(f"iTick获取价格成功: ${gold_price.price}")
                return gold_price

        except asyncio.TimeoutError:
            logger.warning("iTick API请求超时")
        except aiohttp.ClientError as e:
            logger.warning(f"iTick API请求失败: {e}")
        except (ValueError, TypeError) as e:
            logger.warning(f"iTick API数据解析失败: {e}")
        except Exception as e:
            logger.error(f"iTick API异常: {e}")

        return None

    def start_websocket(self) -> bool:
        """
        启动WebSocket实时价格订阅

        WebSocket流程：
        1. 在独立线程中建立连接
        2. 连接成功后自动认证
        3. 发送订阅请求
        4. 接收并处理消息
        5. 定期发送心跳保持连接

        Returns:
            True: 启动成功
            False: 启动失败（可能已运行或配置无效）
        """
        if not self.config.use_websocket:
            logger.warning("WebSocket模式未启用，请设置 use_websocket=true")
            return False

        valid, error = self.config.validate()
        if not valid:
            logger.error(f"iTick WebSocket配置无效: {error}")
            return False

        if not self._check_websocket_available():
            logger.error("WebSocket库不可用，无法启动WebSocket连接")
            return False

        if self._ws_running.is_set():
            logger.warning("WebSocket已在运行")
            return False

        self._ws_running.set()
        if not self._start_callback_processor():
            logger.warning("WebSocket回调处理器启动失败，但继续启动连接")
        self._ws_thread = Thread(target=self._websocket_loop, daemon=True)
        self._ws_thread.start()
        logger.info("iTick WebSocket已启动")
        return True

    def stop_websocket(self) -> None:
        """
        停止WebSocket连接

        停止顺序：
        1. 先设置停止标志
        2. 关闭WebSocket连接
        3. 等待线程结束
        """
        self._ws_running.clear()
        self._ws_connected.clear()

        ws = getattr(self, '_ws', None)
        if ws:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"WebSocket关闭异常: {e}")

        ws_thread = getattr(self, '_ws_thread', None)
        if ws_thread and ws_thread.is_alive():
            ws_thread.join(timeout=3)

        self._ws_thread = None
        self._ws = None
        logger.info("iTick WebSocket已停止")

    def _websocket_loop(self) -> None:
        """
        WebSocket主循环（在独立线程中运行）

        使用websocket-client库实现
        需要处理：
        - 连接建立
        - 认证
        - 订阅
        - 心跳
        - 消息接收
        - 重连
        """
        try:
            import websocket
        except ImportError:
            logger.error("websocket-client库未安装，请运行: pip install websocket-client")
            return

        ws = None
        reconnect_attempts = 0
        max_reconnect_attempts = 5

        while self._ws_running.is_set() and reconnect_attempts < max_reconnect_attempts:
            try:
                headers = {"token": self.config.token} if self.config.token else {}

                ws = websocket.WebSocketApp(
                    self.WS_URL,
                    header=headers,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close
                )

                ws.ws_running = self._ws_running
                ws.ping_interval = self.config.websocket_ping_interval

                self._ws = ws
                self._ws_error_occurred = False
                ws.run_forever(ping_interval=self.config.websocket_ping_interval)

                if self._ws_running.is_set() and self._ws_error_occurred:
                    reconnect_attempts += 1
                    logger.info(f"WebSocket断开，尝试重连 ({reconnect_attempts}/{max_reconnect_attempts})")
                    time.sleep(3)

            except Exception as e:
                self._ws_error_occurred = True
                logger.error(f"WebSocket异常: {e}")

        if reconnect_attempts >= max_reconnect_attempts:
            logger.error("WebSocket重连次数耗尽，停止重连")
            self._ws_thread = None
            self._ws = None

    def _on_ws_open(self, ws) -> None:
        """WebSocket连接建立回调"""
        self._ws_connected.set()
        logger.info("iTick WebSocket连接已建立")

        subscribe_msg = {
            "ac": "subscribe",
            "params": f"{self.config.code}${self.config.region}",
            "types": "quote,tick"
        }
        ws.send(json.dumps(subscribe_msg))
        logger.info(f"已订阅: {self.config.code}${self.config.region}")

    def _on_ws_message(self, ws, message: str) -> None:
        """WebSocket消息接收回调"""
        try:
            data = json.loads(message)

            msg_type = data.get("type", "")

            if msg_type == "ping":
                pong_msg = {"ac": "pong", "params": str(int(time.time() * 1000))}
                ws.send(json.dumps(pong_msg))
                return

            if msg_type in ("quote", "tick") and "data" in data:
                price_data = data["data"]
                self._process_ws_price(price_data)

        except json.JSONDecodeError:
            logger.warning(f"WebSocket消息解析失败: {message[:100]}")
        except Exception as e:
            logger.error(f"WebSocket消息处理异常: {e}")

    def _process_ws_price(self, price_data: dict) -> None:
        """
        处理WebSocket价格数据

        Args:
            price_data: WebSocket推送的价格数据
        """
        gold_price = self._parse_price_data(price_data, source_suffix="-WS")
        if gold_price is None:
            return

        self._latest_price = gold_price

        if self._price_callback:
            self._queue_callback(self._price_callback, gold_price)

        logger.debug(f"WebSocket价格更新: ${gold_price.price}")

    def _queue_callback(self, callback: callable, gold_price: GoldPrice) -> None:
        """线程安全地投递回调到队列"""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            if loop.is_running():
                loop.call_soon_threadsafe(
                    self._put_callback_to_queue,
                    callback,
                    gold_price
                )
            else:
                self._put_callback_to_queue(callback, gold_price)
        except RuntimeError:
            logger.warning("无法获取事件循环，跳过回调")

    def _put_callback_to_queue(self, callback: callable, gold_price: GoldPrice) -> None:
        """
        将回调放入队列

        Args:
            callback: 回调函数
            gold_price: 价格数据
        """
        try:
            self._callback_queue.put_nowait((callback, gold_price))
        except asyncio.QueueFull:
            logger.warning("回调队列已满，跳过此次回调")

    def _on_ws_error(self, ws, error) -> None:
        """WebSocket错误回调"""
        self._ws_error_occurred = True
        logger.warning(f"WebSocket错误: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg) -> None:
        """WebSocket关闭回调"""
        self._ws_connected.clear()
        logger.info(f"WebSocket连接关闭: {close_status_code} - {close_msg}")

    def is_websocket_connected(self) -> bool:
        """检查WebSocket是否已连接"""
        return self._ws_connected.is_set()

    def get_latest_price(self) -> Optional[GoldPrice]:
        """获取最新一次获取的价格（REST或WebSocket）"""
        return self._latest_price


async def fetch_gold_price_with_retry(
    api: GoldPriceAPI,
    retry_count: int = 2
) -> Tuple[Optional[GoldPrice], bool]:
    """
    带重试机制的黄金价格获取

    重试策略：
    1. 最多重试 retry_count + 1 次（包括首次尝试）
    2. 每次重试之间等待1秒
    3. 任一次成功即返回成功
    4. 全部失败才返回失败

    Args:
        api: GoldPriceAPI实例
        retry_count: 最大重试次数（默认2次，即最多尝试3次）

    Returns:
        Tuple[price, all_failed]:
            - price: GoldPrice对象或None
            - all_failed: True表示全部尝试失败，False表示至少成功一次
    """
    last_error = None

    for attempt in range(retry_count + 1):
        try:
            price = await api.fetch_price()

            if price and price.is_valid():
                return price, False
            else:
                last_error = "价格数据无效"

        except Exception as e:
            last_error = str(e)

        if attempt < retry_count:
            logger.info(f"价格获取失败 (尝试 {attempt + 1}/{retry_count + 1}): {last_error}")
            await asyncio.sleep(1)

    logger.error(f"价格获取全部失败: {last_error}")
    return None, True
