"""
主模块：插件入口，定义插件主类和生命周期管理

本模块是 AstrBot 插件的核心入口文件，主要职责：
1. 定义插件主类 GoldAlert
2. 实现插件生命周期管理（初始化/卸载）
3. 注册所有指令路由
4. 权限检查辅助方法
5. 消息发送封装

插件架构：
┌─────────────────────────────────────────────────────────────┐
│                        GoldAlert                            │
├─────────────────────────────────────────────────────────────┤
│  生命周期方法：                                              │
│    __init__()    -> 初始化配置、数据管理器                    │
│    initialize() -> 启动监控任务                             │
│    terminate()  -> 停止监控任务、清理资源                     │
├─────────────────────────────────────────────────────────────┤
│  指令路由：                                                  │
│    /gold price   -> cmd_gold_price() [查询金价]             │
│    /gold add    -> cmd_gold_add()  [添加提醒]               │
│    /gold ls     -> cmd_gold_ls()   [查看提醒]               │
│    /gold rm     -> cmd_gold_rm()  [删除提醒]               │
│    /gold rmall  -> cmd_gold_rmall()[删除全部]              │
│    /admgold     -> 管理员指令...                            │
├─────────────────────────────────────────────────────────────┤
│  内部服务：                                                  │
│    data_manager -> DataManager 实例，数据持久化               │
│    monitor      -> PriceMonitor 实例，价格监控               │
│    commands     -> GoldAlertCommands 实例，指令处理          │
│    api          -> GoldPriceAPI 实例，iTick API客户端       │
└─────────────────────────────────────────────────────────────┘
"""

import os
import functools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event.filter import command_group

from .data import DataManager
from .monitor import PriceMonitor, MonitorConfig
from .commands import GoldAlertCommands
from .api import GoldPriceAPI, ITickConfig


def require_initialized(func):
    """初始化状态检查装饰器 - 插件未成功初始化时拦截用户命令"""
    @functools.wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if self._init_failed:
            yield event.plain_result("❌ 插件初始化失败，请检查配置后重启")
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper


@register(
    "gold_alert",
    "YourName",
    "伦敦金实时价格监控与到价提醒插件",
    "v1.0.0"
)
class GoldAlert(Star):
    """
    伦敦金提醒插件主类
    
    继承自 Star 基类，享受 AstrBot 插件框架提供的：
    - 生命周期管理
    - 指令自动注册
    - 配置管理
    - 上下文访问
    
    配置参数（从插件配置面板获取）：
    - itick_token: iTick API访问令牌（必填）
    - itick_region: iTick地区代码，默认 "GB"
    - itick_gold_code: 贵金属交易代码，默认 "XAUUSD"
    - use_websocket: 是否使用WebSocket，默认 False
    - query_interval: 价格查询间隔（秒）
    - float_range: 浮动范围（美元）
    - lock_duration: 锁定时长（秒）
    - alert_interval: 提醒发送间隔（秒）
    - alert_count: 提醒总条数
    - retry_count: API重试次数
    - whitelist: 用户白名单
    - admin_list: 管理员列表
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        """
        插件构造函数

        AstrBot 会在加载插件时自动调用此方法

        初始化顺序：
        1. 调用父类构造函数
        2. 保存上下文引用
        3. 加载配置参数
        4. 初始化iTick API客户端
        5. 初始化数据管理器（不加载数据）
        6. 创建指令处理器

        注意：监控任务在此阶段不启动，在 initialize() 中启动

        Args:
            context: AstrBot 运行时上下文
            config: 插件配置（从配置文件自动注入）
        """
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._load_itick_config()
        self._validate_and_apply_monitor_config()
        self._init_permission_config()
        self._init_api_client()
        self._init_data_manager()
        self._init_commands()
        logger.info("伦敦金提醒插件初始化完成")

    def _load_itick_config(self) -> None:
        """加载iTick API配置"""
        env_token = os.environ.get("ITICK_TOKEN")
        if env_token:
            self.itick_token = env_token
            logger.info("iTick Token: 已从环境变量加载")
        else:
            self.itick_token = self.config.get("itick_token", "")

        self.itick_region = self.config.get("itick_region", "GB")
        self.itick_gold_code = self.config.get("itick_gold_code", "XAUUSD")
        self.use_websocket = self.config.get("use_websocket", False)

    def _validate_and_apply_monitor_config(self) -> None:
        """验证并应用监控配置参数"""
        self.query_interval = self.config.get("query_interval", 12)
        self.float_range = self.config.get("float_range", 10.0)
        self.lock_duration = self.config.get("lock_duration", 300)
        self.alert_interval = self.config.get("alert_interval", 5)
        self.alert_count = self.config.get("alert_count", 3)
        self.retry_count = self.config.get("retry_count", 2)

        self.query_interval = self._clamp_value(self.query_interval, 5, 300, 12, "query_interval")
        self.float_range = self._clamp_value(self.float_range, 1.0, 100.0, 10.0, "float_range")
        self.lock_duration = self._clamp_value(self.lock_duration, 60, 3600, 300, "lock_duration")
        self.alert_interval = self._clamp_value(self.alert_interval, 1, 60, 5, "alert_interval")
        self.alert_count = self._clamp_value(self.alert_count, 1, 10, 3, "alert_count")
        self.retry_count = self._clamp_value(self.retry_count, 0, 5, 2, "retry_count")

    def _clamp_value(self, value: any, min_val: any, max_val: any, default: any, name: str) -> any:
        """
        限制值在指定范围内

        Args:
            value: 待验证的值
            min_val: 最小值
            max_val: 最大值
            default: 默认值
            name: 配置项名称

        Returns:
            验证后的值
        """
        if not isinstance(value, (int, float)):
            logger.warning(f"{name} 类型错误，使用默认值 {default}")
            return default

        if value < min_val:
            logger.warning(f"{name} 最小值为{min_val}，已自动调整为{min_val}")
            return min_val
        elif value > max_val:
            logger.warning(f"{name} 最大值为{max_val}，已自动调整为{max_val}")
            return max_val
        return value

    def _init_permission_config(self) -> None:
        """
        初始化权限配置

        统一使用列表格式，与 minimax_alert 插件保持一致
        """
        self.whitelist = self.config.get("whitelist", [])
        self.admin_list = self.config.get("admin_list", [])
        self._whitelist_set = set(self.whitelist) if self.whitelist else set()
        self._admin_set = set(self.admin_list) if self.admin_list else set()

    def _init_api_client(self) -> None:
        """
        初始化API客户端
        """
        self.itick_config = ITickConfig(
            token=self.itick_token,
            region=self.itick_region,
            code=self.itick_gold_code,
            use_websocket=self.use_websocket
        )
        self.api = GoldPriceAPI(self.itick_config)

    def _init_data_manager(self) -> None:
        """
        初始化数据管理器

        数据文件存放在插件数据目录下
        """
        data_dir = StarTools.get_data_dir("gold_alert")
        data_file = data_dir / "gold_alerts.json"
        self.data_manager = DataManager(data_file)

    def _init_commands(self) -> None:
        """
        初始化指令处理器
        """
        self.monitor: PriceMonitor | None = None
        self.commands = GoldAlertCommands(self)
        self._initialized = False
        self._init_failed = False

    async def initialize(self) -> None:
        """
        插件初始化
        
        AstrBot 会在插件加载完成后调用此方法
        
        初始化步骤：
        1. 验证iTick配置
        2. 加载持久化数据（用户提醒等）
        3. 解锁所有提醒（插件重启时的状态重置）
        4. 创建监控配置对象
        5. 创建并启动监控任务
        
        错误处理：
        - 初始化失败只记录日志，不发送通知
        - 监控启动失败会影响插件功能
        """
        try:
            # 验证iTick配置
            valid, error = self.itick_config.validate()
            if not valid:
                logger.error(f"iTick配置验证失败: {error}")
                logger.error("插件启动失败，请检查iTick Token配置")
                self._init_failed = True
                return
            
            logger.info(f"iTick配置: 地区={self.itick_region}, 品种={self.itick_gold_code}")
            logger.info(f"数据模式: {'WebSocket实时推送' if self.use_websocket else 'REST轮询'}")
            
            self.data_manager.initialize()
            self.data_manager.unlock_all_alerts()

            # 创建监控配置
            monitor_config = MonitorConfig(
                query_interval=self.query_interval,
                float_range=self.float_range,
                lock_duration=self.lock_duration,
                alert_interval=self.alert_interval,
                alert_count=self.alert_count,
                retry_count=self.retry_count
            )

            # 创建监控器实例（传入已初始化的API客户端）
            self.monitor = PriceMonitor(
                context=self.context,
                data_manager=self.data_manager,
                config=monitor_config,
                api=self.api,
                send_message_func=self._send_message,
                notify_admin_func=self.notify_admin
            )

            # 启动监控任务
            await self.monitor.start()

            self._initialized = True
            logger.info("伦敦金提醒插件启动成功")
            logger.info(f"查询间隔: {self.query_interval}秒")
            logger.info(f"浮动范围: ±{self.float_range}美元")
            logger.info(f"锁定时长: {self.lock_duration}秒")

        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)
            self._init_failed = True

    async def terminate(self) -> None:
        """
        插件卸载/停止

        AstrBot 会在插件被禁用或机器人关闭时调用此方法

        清理工作：
        1. 停止监控任务（优雅终止）
        2. 关闭API客户端连接
        3. 重置初始化标志
        4. 释放相关资源
        """
        try:
            if self.monitor:
                await self.monitor.stop()
        except Exception as e:
            logger.error(f"停止监控任务失败: {e}", exc_info=True)
        
        try:
            await self.api.close()
        except Exception as e:
            logger.error(f"关闭API客户端失败: {e}", exc_info=True)

        self._initialized = False
        self._init_failed = False
        
        logger.info("伦敦金提醒插件已停止")

    # ==================== 用户指令路由 ====================

    @command_group("gold")
    def gold(self):
        """黄金价格提醒相关指令组"""
        pass

    @gold.command("price")
    @require_initialized
    async def cmd_gold_price(self, event: AstrMessageEvent):
        """查询当前金价 /gold price"""
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_price(event):
            yield result

    @gold.command("add")
    @require_initialized
    async def cmd_gold_add(self, event: AstrMessageEvent, price: str):
        """
        添加价格提醒

        使用方式：/gold add 1900

        Args:
            price: 提醒价格（美元/盎司）
        """
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_add(event, price):
            yield result

    @gold.command("ls")
    @require_initialized
    async def cmd_gold_ls(self, event: AstrMessageEvent):
        """查看我的提醒"""
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_ls(event):
            yield result

    @gold.command("rm")
    @require_initialized
    async def cmd_gold_rm(self, event: AstrMessageEvent, price: str):
        """
        删除提醒

        使用方式：/gold rm 1900

        Args:
            price: 要删除的提醒价格
        """
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_rm(event, price):
            yield result

    @gold.command("rmall")
    @require_initialized
    async def cmd_gold_rmall(self, event: AstrMessageEvent):
        """删除所有提醒"""
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_rmall(event):
            yield result

    # ==================== 管理员指令路由 ====================

    @command_group("admgold")
    def admgold(self):
        """管理员指令组"""
        pass

    @admgold.command("list")
    @require_initialized
    async def cmd_admgold_list(self, event: AstrMessageEvent):
        """查看所有用户的提醒"""
        if not self._check_admin(event):
            yield event.plain_result("❌ 您没有管理员权限，无法执行此操作")
            return
        async for result in self.commands.cmd_admin_list(event):
            yield result

    @admgold.command("rm")
    @require_initialized
    async def cmd_admgold_rm(self, event: AstrMessageEvent, price: float, user_id: str):
        """
        删除指定用户的提醒

        使用方式：/admgold rm 1900 12345678

        Args:
            price: 提醒价格
            user_id: 目标用户QQ号
        """
        if not self._check_admin(event):
            yield event.plain_result("❌ 您没有管理员权限，无法执行此操作")
            return
        async for result in self.commands.cmd_admin_rm(event, str(price), user_id):
            yield result

    @admgold.command("restart")
    @require_initialized
    async def cmd_admgold_restart(self, event: AstrMessageEvent):
        """重启监控"""
        if not self._check_admin(event):
            yield event.plain_result("❌ 您没有管理员权限，无法执行此操作")
            return
        async for result in self.commands.cmd_admin_restart(event):
            yield result

    @admgold.command("stop")
    @require_initialized
    async def cmd_admgold_stop(self, event: AstrMessageEvent):
        """停止监控"""
        if not self._check_admin(event):
            yield event.plain_result("❌ 您没有管理员权限，无法执行此操作")
            return
        async for result in self.commands.cmd_admin_stop(event):
            yield result

    # ==================== 辅助方法 ====================

    def _parse_user_list(self, user_list: str | list) -> set:
        """
        将用户ID列表解析为Set集合

        修改为支持列表格式，与 minimax_alert 插件保持一致
        同时保持向后兼容，仍支持字符串格式（逗号分隔）
        
        Args:
            user_list: 用户ID列表或逗号分隔的字符串

        Returns:
            用户ID的Set集合
        """
        if not user_list:
            return set()

        # 支持列表和元组格式
        if isinstance(user_list, (list, tuple)):
            return {str(uid).strip() for uid in user_list if uid}

        # 保持向后兼容：支持字符串格式
        if isinstance(user_list, str):
            if not user_list.strip():
                return set()
            return {uid.strip() for uid in user_list.split(",") if uid.strip()}

        # 类型不匹配时的容错处理
        logger.warning(f"用户列表类型不支持: {type(user_list)}")
        return set()

    def _check_whitelist(self, event: AstrMessageEvent) -> bool:
        """
        检查用户是否在白名单中

        白名单为空表示不限制，所有用户可用

        Args:
            event: 消息事件

        Returns:
            True: 用户可用
            False: 用户不在白名单中
        """
        if not self._whitelist_set:
            return True

        user_id = str(event.get_sender_id())
        return user_id in self._whitelist_set

    def _check_admin(self, event: AstrMessageEvent) -> bool:
        """
        检查用户是否为管理员

        安全设计：未配置管理员则拒绝所有管理请求

        Args:
            event: 消息事件

        Returns:
            True: 用户是管理员
            False: 用户不是管理员或未配置管理员
        """
        if not self._admin_set:
            logger.warning("管理员列表未配置，拒绝管理指令")
            return False

        user_id = str(event.get_sender_id())

        if user_id not in self._admin_set:
            logger.warning(f"非管理员尝试执行管理指令: {user_id}")
            return False
        return True

    def _get_client(self):
        """获取平台客户端的bot实例"""
        try:
            for adapter in self.context.platform_manager.get_insts():
                if hasattr(adapter, "bot") and adapter.bot and hasattr(adapter.bot, "api"):
                    return adapter.bot
        except Exception as e:
            logger.debug(f"_get_client 遍历适配器异常: {e}")
        return None

    async def _send_message(self, session_id: str, message: str, user_id: str, is_group: bool) -> bool:
        """发送消息给用户（使用平台API直接发送）"""
        try:
            client = self._get_client()
            if not client:
                logger.error("无法获取平台客户端")
                return False

            if is_group:
                message_parts = [{"type": "at", "data": {"qq": user_id}}, {"type": "text", "data": {"text": f" {message}"}}]
                try:
                    await client.api.call_action("send_group_msg", group_id=int(session_id), message=message_parts)
                except ValueError:
                    logger.error(f"群ID格式无效: {session_id}")
                    return False
            else:
                try:
                    await client.api.call_action("send_private_msg", user_id=int(user_id), message=message)
                except ValueError:
                    logger.error(f"用户ID格式无效: {user_id}")
                    return False
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}", exc_info=True)
            return False

    async def send_user_message(self, user_id: str, message: str) -> bool:
        """
        发送私聊消息给管理员
        """
        return await self._send_message("", message, user_id, is_group=False)

    async def notify_admin(self, message: str) -> None:
        """
        通知所有管理员

        通知场景：
        - 系统错误
        - 用户提醒被禁用
        - 监控任务异常

        Args:
            message: 通知内容
        """
        if not self._admin_set:
            return

        for admin_id in self._admin_set:
            await self.send_user_message(admin_id, message)

    # ==================== 中文指令 ====================

    @filter.command("金价")
    @require_initialized
    async def cmd_jin_jia(self, event: AstrMessageEvent):
        """
        查询金价（中文指令别名）

        等同于 /gold
        """
        if not self._check_whitelist(event):
            yield event.plain_result("❌ 您没有权限使用此功能，请联系管理员")
            return
        async for result in self.commands.cmd_gold_price(event):
            yield result