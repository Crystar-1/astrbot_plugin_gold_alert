"""
指令模块：处理用户和管理员输入的指令
"""

from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import TYPE_CHECKING, Optional
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
from .data import AlertRule
from .constants import MIN_PRICE, MAX_PRICE

if TYPE_CHECKING:
    from .monitor import PriceMonitor
    from .main import GoldAlert


def _parse_price(price_str: str, check_range: bool = True) -> Decimal | None:
    """
    解析价格字符串为Decimal类型

    Args:
        price_str: 价格字符串
        check_range: 是否检查合理范围

    Returns:
        Decimal类型的价格，解析失败返回None
    """
    try:
        price = Decimal(str(price_str)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if price <= 0:
            return None
        if check_range and (price < MIN_PRICE or price > MAX_PRICE):
            logger.warning(f"价格超出合理范围: {price} (期望: {MIN_PRICE}-{MAX_PRICE})")
            return None
        return price
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_price(price_str: str) -> Decimal | None:
    """
    解析价格字符串为Decimal类型（带边界检查）

    Args:
        price_str: 价格字符串

    Returns:
        Decimal类型的价格，解析失败返回None
    """
    return _parse_price(price_str, check_range=True)


def parse_price_for_delete(price_str: str) -> Decimal | None:
    """
    解析价格字符串为Decimal类型（无边界限制）

    Args:
        price_str: 价格字符串

    Returns:
        Decimal类型的价格，解析失败返回None
    """
    return _parse_price(price_str, check_range=False)


class GoldAlertCommands:
    """黄金提醒指令处理器"""

    def __init__(self, star_instance: "GoldAlert") -> None:
        self.star = star_instance
        self.context = star_instance.context
        self.data_manager = star_instance.data_manager
        self.api = star_instance.api

    @property
    def monitor(self) -> "Optional[PriceMonitor]":
        """延迟获取monitor，避免初始化时为None"""
        return self.star.monitor

    def _get_monitor_safe(self) -> "Optional[PriceMonitor]":
        """安全获取monitor实例，带空值检查"""
        monitor = self.star.monitor
        if monitor is None:
            logger.warning("Monitor尚未初始化，无法执行操作")
        return monitor

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        return event.session_id

    def _is_group(self, event: AstrMessageEvent) -> bool:
        """判断是否为群聊场景"""
        is_group = getattr(event, 'is_group', None)
        if is_group is not None:
            return bool(is_group)

        origin = getattr(event, 'unified_msg_origin', None)
        if origin:
            origin_str = str(origin).lower()
            if "groupmessage" in origin_str or "channel" in origin_str:
                return True

        session_id = getattr(event, 'session_id', '') or ''
        return self._session_id_indicates_group(session_id)

    @staticmethod
    def _session_id_indicates_group(session_id: str) -> bool:
        """根据session_id判断是否为群聊"""
        if not session_id:
            return False
        session_lower = session_id.lower()
        group_indicators = ('group', 'channel', '@g.', '@c.')
        return any(ind in session_lower for ind in group_indicators)

    def _send_error(self, event: AstrMessageEvent, message: str):
        """统一的错误消息发送"""
        return event.plain_result(f"❌ {message}")

    def _send_success(self, event: AstrMessageEvent, message: str):
        """统一的成功消息发送"""
        return event.plain_result(f"✅ {message}")

    def _send_warning(self, event: AstrMessageEvent, message: str):
        """统一的警告消息发送"""
        return event.plain_result(f"⚠️ {message}")

    def _send_info(self, event: AstrMessageEvent, message: str):
        """统一的信息消息发送"""
        return event.plain_result(f"📋 {message}")

    async def cmd_gold_price(self, event: AstrMessageEvent):
        """查询当前黄金价格"""
        from .api import fetch_gold_price_with_retry

        try:
            retry_count = getattr(self.star, 'retry_count', 2)
            price, failed = await fetch_gold_price_with_retry(self.api, retry_count)
            if failed or not price:
                yield self._send_error(event, "无法获取金价，请稍后重试")
                return

            from datetime import datetime
            time_str = price.timestamp.strftime("%Y-%m-%d %H:%M:%S")

            result = f"""📊 伦敦金实时价格
💰 现价：{price.price} 美元/盎司
🕐 时间：{time_str}
📡 来源：{price.source}"""
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"查询金价失败: {e}")
            yield self._send_error(event, "获取金价时发生错误，请稍后重试")

    async def cmd_gold_add(self, event: AstrMessageEvent, price_str: str):
        """添加价格提醒"""
        price = parse_price(price_str)
        if price is None:
            yield self._send_error(event, "无效的价格，请输入500-5000之间的正数（如：1900.50）")
            return

        user_id = self._get_user_id(event)
        session_id = self._get_session_id(event)
        is_group = self._is_group(event)

        alert = AlertRule(
            price=price,
            user_id=user_id,
            session_id=session_id,
            is_group=is_group
        )

        added = self.data_manager.add_alert(alert)
        if added:
            yield self._send_success(event, f"已设置提醒：当黄金价格穿过 {price} 美元/盎司时通知您")
        else:
            yield self._send_warning(event, "该价格提醒已存在，无需重复添加")

    async def cmd_gold_ls(self, event: AstrMessageEvent):
        """查看当前用户所有提醒"""
        user_id = self._get_user_id(event)
        alerts = self.data_manager.get_user_alerts(user_id)

        if not alerts:
            yield self._send_info(event, "您当前没有设置任何黄金价格提醒")
            return

        lines = ["📋 您的黄金价格提醒："]
        for i, alert in enumerate(alerts, 1):
            status = "🔒 已锁定" if alert.is_locked else "✅ 可用"
            disabled = "⛔ 已禁用" if alert.is_disabled else ""
            lines.append(f"{i}. 💰 {alert.price} 美元/盎司 {status} {disabled}")

        lines.append("")
        lines.append("指令：/gold add <价格> 添加 | /gold rm <价格> 删除")
        yield event.plain_result("\n".join(lines))

    async def cmd_gold_rm(self, event: AstrMessageEvent, price_str: str):
        """删除指定价格提醒"""
        price = parse_price_for_delete(price_str)
        if price is None:
            yield self._send_error(event, "无效的价格格式")
            return

        user_id = self._get_user_id(event)
        removed = self.data_manager.remove_alert(user_id, price)

        if removed:
            yield self._send_success(event, f"已删除提醒：{price} 美元/盎司")
        else:
            yield self._send_warning(event, "该价格提醒不存在")

    async def cmd_gold_rmall(self, event: AstrMessageEvent):
        """删除所有提醒"""
        user_id = self._get_user_id(event)
        count = self.data_manager.remove_user_all_alerts(user_id)

        if count > 0:
            yield self._send_success(event, f"已删除全部 {count} 条提醒")
        else:
            yield self._send_info(event, "您当前没有设置任何提醒")

    async def cmd_admin_list(self, event: AstrMessageEvent):
        """管理员查看所有用户提醒"""
        all_alerts = self.data_manager.admin_list_all_alerts()

        if not all_alerts:
            yield self._send_info(event, "目前没有任何用户设置提醒")
            return

        lines = ["📋 所有用户的黄金价格提醒：", ""]
        for user_id, alerts in all_alerts.items():
            lines.append(f"👤 用户 {user_id} ({len(alerts)} 条提醒)：")
            for alert in alerts:
                status = "🔒 锁定" if alert.get("is_locked") else "✅ 可用"
                disabled = "⛔ 禁用" if alert.get("is_disabled") else ""
                price_val = alert.get('price')
                lines.append(f"   💰 {price_val} 美元/盎司 {status} {disabled}")
            lines.append("")

        yield event.plain_result("\n".join(lines))

    async def cmd_admin_rm(self, event: AstrMessageEvent, price_str: str, user_id: str):
        """管理员删除指定用户的提醒"""
        price = parse_price(price_str)
        if price is None:
            yield self._send_error(event, "无效的价格格式")
            return

        removed, _ = self.data_manager.admin_remove_alert(user_id, price)

        if removed:
            await self._notify_user(user_id, f"📋 【管理员操作通知】\n操作类型：删除\n操作内容：删除您的黄金价格提醒（{price} 美元/盎司）\n操作结果：成功")
            yield self._send_success(event, f"已删除用户 {user_id} 的提醒：{price}")
        else:
            yield self._send_warning(event, "未找到该用户的提醒")

    async def cmd_admin_restart(self, event: AstrMessageEvent):
        """管理员重启监控"""
        monitor = self._get_monitor_safe()
        if monitor is None:
            yield self._send_error(event, "监控器未初始化")
            return

        if monitor.is_running():
            yield self._send_warning(event, "监控任务已在运行中")
            return

        success = await monitor.restart()
        if success:
            yield self._send_success(event, "价格监控任务已重启")
        else:
            yield self._send_error(event, "重启失败")

    async def cmd_admin_stop(self, event: AstrMessageEvent):
        """管理员停止监控"""
        monitor = self._get_monitor_safe()
        if monitor is None:
            yield self._send_error(event, "监控器未初始化")
            return

        if not monitor.is_running():
            yield self._send_warning(event, "监控任务已停止")
            return

        await monitor.stop()
        yield self._send_success(event, "价格监控任务已停止")

    async def _notify_user(self, user_id: str, message: str) -> bool:
        """向指定用户发送通知消息"""
        if hasattr(self.star, 'send_user_message'):
            return await self.star.send_user_message(user_id, message)
        return False
