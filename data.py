"""
数据模块：负责用户提醒规则的数据模型定义和持久化管理

本模块包含两个核心数据类：
- AlertRule: 单个提醒规则的数据结构
- PluginData: 插件整体持久化数据的结构

以及一个核心管理类：
- DataManager: 提供协程安全的数据操作接口
"""

import json
import asyncio
import copy
import threading
from pathlib import Path
from decimal import Decimal, InvalidOperation as DecimalInvalidOperation
from typing import Dict, List, Optional, Union
from dataclasses import dataclass, field, asdict
from datetime import datetime
from astrbot.api import logger


class DecimalEncoder(json.JSONEncoder):
    """自定义JSON编码器，支持Decimal类型序列化为字符串"""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def decimal_decoder(dct: dict) -> dict:
    """JSON反序列化钩子，将字符串转换回Decimal"""
    price_fields = {"price", "latest_price", "current_price"}
    for key in dct:
        if key in price_fields and isinstance(dct[key], (str, float)):
            try:
                dct[key] = Decimal(str(dct[key]))
            except (ValueError, TypeError, DecimalInvalidOperation):
                pass
    return dct


@dataclass
class AlertRule:
    """
    用户提醒规则的数据模型

    Attributes:
        price: 提醒触发价格（美元/盎司），使用Decimal确保精度
        user_id: 用户唯一标识（QQ号）
        session_id: 消息会话ID，用于确定私聊/群聊及具体会话
        is_group: 是否为群聊场景（影响提醒发送方式：私聊@ vs 群聊直接发送）
        created_at: 提醒创建时间，用于记录和追溯
        is_locked: 触发锁定状态，防止重复触发
        lock_time: 锁定开始时间，用于计算锁定超时
        send_fail_count: 连续发送失败计数，超过阈值将禁用提醒
        is_disabled: 是否被禁用（管理员或系统禁用）
    """
    price: Decimal
    user_id: str
    session_id: str
    is_group: bool
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    is_locked: bool = False
    lock_time: Optional[str] = None
    send_fail_count: int = 0
    is_disabled: bool = False

    def to_dict(self) -> dict:
        """将提醒规则转换为字典格式，用于JSON序列化"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AlertRule | None":
        """从字典数据创建提醒规则实例，用于JSON反序列化"""
        if not isinstance(data, dict):
            logger.warning(f"AlertRule反序列化失败：数据不是字典类型")
            return None

        if "price" not in data:
            logger.warning("AlertRule反序列化失败：缺少price字段")
            return None

        try:
            price = Decimal(str(data.get("price", 0)))
            if price <= 0:
                logger.warning("AlertRule反序列化失败：price必须为正数")
                return None
        except (TypeError, ValueError):
            logger.warning("AlertRule反序列化失败：price格式无效")
            return None
        
        try:
            return cls(
                price=price,
                user_id=str(data.get("user_id", "")),
                session_id=str(data.get("session_id", "")),
                is_group=bool(data.get("is_group", False)),
                is_locked=bool(data.get("is_locked", False)),
                lock_time=data.get("lock_time"),
                is_disabled=bool(data.get("is_disabled", False)),
                send_fail_count=int(data.get("send_fail_count", 0)),
                created_at=data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        except (TypeError, ValueError) as e:
            logger.warning(f"AlertRule反序列化失败: {e}")
            return None


@dataclass
class PluginData:
    """
    插件持久化数据的顶层数据结构

    Attributes:
        alerts: 以用户ID为键的提醒规则字典 {user_id: [AlertRule, ...]}
        config: 全局配置数据（预留，目前配置通过AstrBot配置系统管理）
    """
    alerts: Dict[str, List[dict]] = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为可序列化的字典格式"""
        return {
            "alerts": self.alerts,
            "config": self.config
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PluginData":
        """从字典数据恢复插件数据实例"""
        if not isinstance(data, dict):
            logger.warning("PluginData反序列化失败：数据不是字典类型")
            return cls()
        
        alerts = {}
        raw_alerts = data.get("alerts", {})
        
        if isinstance(raw_alerts, dict):
            for user_id, user_alerts in raw_alerts.items():
                if not isinstance(user_alerts, list):
                    logger.warning(f"PluginData反序列化：用户 {user_id} 的提醒数据格式错误，跳过")
                    continue
                
                validated_alerts = []
                for alert_data in user_alerts:
                    if isinstance(alert_data, dict):
                        validated_alerts.append(alert_data)
                
                alerts[user_id] = validated_alerts
        
        return cls(
            alerts=alerts,
            config=data.get("config", {})
        )


class DataManager:
    """
    数据管理器：负责插件数据的持久化存储

    设计要点：
    1. 线程安全的文件I/O：使用类级别threading.Lock配合asyncio.to_thread()，避免阻塞事件循环
    2. 惰性加载：数据在首次访问时才从磁盘加载
    3. 自动保存：任何数据修改后自动持久化到磁盘
    """

    _lock: threading.Lock = threading.Lock()

    def __init__(self, data_file: Path):
        """
        初始化数据管理器

        Args:
            data_file: 持久化数据文件的路径
        """
        self.data_file = data_file
        self._data: Optional[PluginData] = None
        self._initialized = False

    @staticmethod
    def _normalize_price(price: Union[float, Decimal, str, None]) -> Decimal:
        """统一价格格式为Decimal"""
        if price is None:
            raise ValueError("价格不能为None")

        if isinstance(price, Decimal):
            return price

        try:
            return Decimal(str(price))
        except (ValueError, TypeError, DecimalInvalidOperation) as e:
            raise ValueError(f"无效的价格格式: {price}") from e

    def _ensure_dir(self) -> None:
        """确保数据文件目录存在"""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        """初始化数据管理器，加载持久化数据"""
        if self._initialized:
            return

        try:
            if self.data_file.exists():
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f, object_hook=decimal_decoder)
                    self._data = PluginData.from_dict(data)
                    logger.info(f"已加载持久化数据: {len(self._data.alerts)} 个用户有提醒设置")
            else:
                self._data = PluginData()
                self._ensure_dir()
                self._save_sync()
                logger.info("创建新的持久化数据文件")
        except Exception as e:
            logger.error(f"加载持久化数据失败: {e}")
            self._data = PluginData()

        self._initialized = True

    def _save_sync(self) -> None:
        """同步保存数据到文件"""
        self._ensure_dir()
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self._data.to_dict(), f, ensure_ascii=False, indent=2, cls=DecimalEncoder)
        logger.debug("数据已保存")

    async def async_save(self) -> None:
        """
        异步保存数据到磁盘文件

        使用asyncio.to_thread()避免阻塞事件循环
        """
        await asyncio.to_thread(self._save_with_lock)

    def _save_with_lock(self) -> None:
        """带锁的同步保存"""
        with DataManager._lock:
            self._save_sync()

    def save(self) -> None:
        """
        同步保存数据（保持向后兼容）

        注意：在异步上下文中推荐使用 async_save()
        """
        self._save_with_lock()

    def _update_with_lock(self, update_func) -> None:
        """在锁内执行更新操作"""
        with DataManager._lock:
            update_func()
            self._save_sync()

    def get_all_alerts(self) -> List[AlertRule]:
        """获取所有用户的提醒列表"""
        if not self._data:
            return []

        all_alerts = []
        for user_alerts in self._data.alerts.values():
            for alert_data in user_alerts:
                alert = AlertRule.from_dict(alert_data)
                if alert is not None:
                    all_alerts.append(alert)
        return all_alerts

    def get_user_alerts(self, user_id: str) -> List[AlertRule]:
        """获取指定用户的所有提醒规则"""
        if not self._data:
            return []

        alerts = self._data.alerts.get(user_id, [])
        return [alert for a in alerts if (alert := AlertRule.from_dict(a)) is not None]

    def add_alert(self, alert: AlertRule) -> bool:
        """添加新的提醒规则"""
        with DataManager._lock:
            if not self._data:
                return False

            user_id = alert.user_id
            if user_id not in self._data.alerts:
                self._data.alerts[user_id] = []

            alert_price_str = str(alert.price)
            for existing in self._data.alerts[user_id]:
                existing_price = existing.get("price")
                if existing_price is not None and str(existing_price) == alert_price_str:
                    logger.info(f"提醒已存在: 用户 {user_id}, 价格 {alert.price}")
                    return False

            self._data.alerts[user_id].append(alert.to_dict())
            self._save_sync()
            logger.info(f"已添加提醒: 用户 {user_id}, 价格 {alert.price}")
            return True

    def remove_alert(self, user_id: str, price: Union[float, Decimal]) -> bool:
        """删除指定用户的指定价格提醒"""
        price_decimal = self._normalize_price(price)
        with DataManager._lock:
            if not self._data or user_id not in self._data.alerts:
                return False

            original_len = len(self._data.alerts[user_id])
            self._data.alerts[user_id] = [
                a for a in self._data.alerts[user_id]
                if self._normalize_price(a.get("price")) != price_decimal
            ]

            if len(self._data.alerts[user_id]) < original_len:
                if not self._data.alerts[user_id]:
                    del self._data.alerts[user_id]
                self._save_sync()
                logger.info(f"已删除提醒: 用户 {user_id}, 价格 {price}")
                return True
            return False

    def remove_user_all_alerts(self, user_id: str) -> int:
        """删除指定用户的所有提醒"""
        with DataManager._lock:
            if not self._data or user_id not in self._data.alerts:
                return 0

            count = len(self._data.alerts[user_id])
            del self._data.alerts[user_id]
            self._save_sync()
            logger.info(f"已删除用户所有提醒: 用户 {user_id}, 数量 {count}")
            return count

    def update_alert(self, user_id: str, price: Union[float, Decimal], updates: dict) -> bool:
        """更新提醒规则的指定字段"""
        price_decimal = self._normalize_price(price)
        with DataManager._lock:
            if not self._data or user_id not in self._data.alerts:
                return False

            for alert in self._data.alerts[user_id]:
                if self._normalize_price(alert.get("price")) == price_decimal:
                    alert.update(updates)
                    self._save_sync()
                    return True
            return False

    def lock_alert(self, user_id: str, price: Union[float, Decimal]) -> bool:
        """锁定提醒规则"""
        return self.update_alert(user_id, price, {
            "is_locked": True,
            "lock_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    def unlock_alert(self, user_id: str, price: Union[float, Decimal]) -> bool:
        """解锁提醒规则"""
        return self.update_alert(user_id, price, {
            "is_locked": False,
            "lock_time": None
        })

    def unlock_all_alerts(self) -> None:
        """解锁所有提醒规则"""
        with DataManager._lock:
            if not self._data:
                return

            for user_id in self._data.alerts:
                for alert in self._data.alerts[user_id]:
                    alert["is_locked"] = False
                    alert["lock_time"] = None

            self._save_sync()
            logger.info("已解锁所有提醒")

    def increment_send_fail(self, user_id: str, price: Union[float, Decimal]) -> int:
        """增加发送失败计数"""
        price_decimal = self._normalize_price(price)
        with DataManager._lock:
            if not self._data or user_id not in self._data.alerts:
                return 0

            for alert in self._data.alerts[user_id]:
                if self._normalize_price(alert.get("price")) == price_decimal:
                    alert["send_fail_count"] = alert.get("send_fail_count", 0) + 1
                    self._save_sync()
                    return alert["send_fail_count"]
            return 0

    def reset_send_fail(self, user_id: str, price: Union[float, Decimal]) -> None:
        """重置发送失败计数"""
        self.update_alert(user_id, price, {"send_fail_count": 0})

    def disable_alert(self, user_id: str, price: Union[float, Decimal]) -> bool:
        """禁用提醒规则"""
        return self.update_alert(user_id, price, {"is_disabled": True})

    def enable_alert(self, user_id: str, price: Union[float, Decimal]) -> bool:
        """启用提醒规则"""
        return self.update_alert(user_id, price, {"is_disabled": False})

    def get_all_users_with_alerts(self) -> List[str]:
        """获取所有设置了提醒的用户ID列表"""
        if not self._data:
            return []
        return list(self._data.alerts.keys())

    def admin_remove_alert(self, user_id: str, price: Union[float, Decimal]) -> tuple[bool, Optional[str]]:
        """管理员删除指定用户的提醒"""
        price_decimal = self._normalize_price(price)
        with DataManager._lock:
            if not self._data or user_id not in self._data.alerts:
                return False, None

            for alert in self._data.alerts[user_id]:
                if self._normalize_price(alert.get("price")) == price_decimal:
                    original_len = len(self._data.alerts[user_id])
                    self._data.alerts[user_id] = [
                        a for a in self._data.alerts[user_id]
                        if self._normalize_price(a.get("price")) != price_decimal
                    ]
                    if len(self._data.alerts[user_id]) < original_len:
                        if not self._data.alerts[user_id]:
                            del self._data.alerts[user_id]
                        self._save_sync()
                        logger.info(f"管理员删除提醒: 用户 {user_id}, 价格 {price}")
                        return True, user_id
            return False, None

    def admin_list_all_alerts(self) -> Dict[str, List[dict]]:
        """管理员查看所有用户的提醒数据"""
        if not self._data:
            return {}
        with DataManager._lock:
            return copy.deepcopy(self._data.alerts)
