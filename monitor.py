"""
监控模块：价格监控的核心业务逻辑实现

本模块实现：
1. MonitorConfig 配置类：监控相关参数的标准化封装
2. PriceMonitor 类：价格监控器，处理价格获取、触发判断、消息发送

核心业务流程：
┌─────────────────────────────────────────────────────────────┐
│                    监控主循环                                │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐     │
│  │ 获取价格 │ -> │ 处理价格 │ -> │ 检查每个用户提醒  │     │
│  └──────────┘    └──────────┘    └──────────────────┘     │
│                                              │              │
│                    ┌─────────────────────────┼───────────┐ │
│                    ▼                         ▼           ▼ │
│            ┌───────────────┐      ┌────────────┐  ┌─────┐ │
│            │ 价格穿过设定价 │      │ 超出浮动范围│  │锁定 │ │
│            │ + 未锁定 -> 触发 │      │ + 已锁定 -> 解锁│超时 │ │
│            └───────────────┘      └────────────┘  └─────┘ │
└─────────────────────────────────────────────────────────────┘
"""

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass
import aiohttp
from astrbot.api import logger
from .api import GoldPriceAPI, GoldPrice, fetch_gold_price_with_retry
from .data import DataManager, AlertRule
from .constants import MAX_SEND_FAIL_COUNT, MAX_TRIGGER_BATCH, RETRY_DELAY_SHORT, RETRY_DELAY_LONG

if TYPE_CHECKING:
    from astrbot.api.star import Context


@dataclass
class MonitorConfig:
    """价格监控器配置参数"""
    query_interval: int = 12
    float_range: float = 10.0
    lock_duration: int = 300
    alert_interval: int = 5
    alert_count: int = 3
    retry_count: int = 2


class PriceMonitor:
    """价格监控器"""

    def __init__(
        self,
        context: "Context",
        data_manager: DataManager,
        config: MonitorConfig,
        api: GoldPriceAPI,
        send_message_func: Callable[[str, str, str, bool], Awaitable[bool]],
        notify_admin_func: Callable[[str], Awaitable[None]] | None = None
    ) -> None:
        self.context = context
        self.data_manager = data_manager
        self.config = config
        self.send_message = send_message_func
        self._notify_admin = notify_admin_func
        self.api = api
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stopped = False
        self._last_price: Optional[Decimal] = None

    async def start(self) -> bool:
        """启动价格监控任务"""
        if self._running:
            logger.warning("监控任务已在运行")
            return False

        self._running = True
        self._stopped = False
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("价格监控任务已启动")
        return True

    async def stop(self) -> None:
        """停止价格监控任务"""
        self._running = False
        self._stopped = True
        
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None
        
        logger.info("价格监控任务已停止")

    async def restart(self) -> bool:
        """重启监控任务"""
        await self.stop()
        self._last_price = None
        return await self.start()

    async def _monitor_loop(self) -> None:
        """监控主循环"""
        while self._running:
            try:
                price, failed = await fetch_gold_price_with_retry(
                    self.api,
                    self.config.retry_count
                )

                if failed or not price:
                    logger.error("价格获取失败，监控继续")
                    await asyncio.sleep(self.config.query_interval)
                    continue

                current_price = price.price
                logger.debug(f"当前金价: ${current_price} (来源: {price.source})")

                await self._process_price(current_price)
                self._last_price = current_price

                await asyncio.sleep(self.config.query_interval)

            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except asyncio.TimeoutError:
                logger.warning("价格查询超时")
                await asyncio.sleep(self.config.query_interval)
            except aiohttp.ClientError as e:
                logger.error(f"HTTP客户端错误: {e}", exc_info=True)
                await asyncio.sleep(self.config.query_interval)
            except ValueError as e:
                logger.error(f"数据解析错误: {e}", exc_info=True)
                await asyncio.sleep(RETRY_DELAY_SHORT)
            except Exception as e:
                logger.critical(f"未预期的监控异常: {e}", exc_info=True)
                await asyncio.sleep(RETRY_DELAY_SHORT)

    async def _process_price(self, current_price: Decimal) -> None:
        """处理当前价格，检查是否触发用户的提醒"""
        alerts = self.data_manager.get_all_alerts()

        trigger_batch = []
        unlock_tasks = []

        for alert in alerts:
            if alert.is_disabled:
                continue

            action = self._evaluate_alert_action(alert, current_price)

            if action == "trigger":
                trigger_batch.append((alert, current_price))

            elif action == "unlock":
                logger.info(f"价格超出范围，解锁提醒: 用户 {alert.user_id}, 价格 {alert.price}, 当前价 ${current_price}")
                unlock_tasks.append(self._notify_out_of_range(alert, current_price))
                self.data_manager.unlock_alert(alert.user_id, alert.price)

            elif action == "timeout_unlock":
                logger.info(f"锁定超时解锁: 用户 {alert.user_id}, 价格 {alert.price}")
                self.data_manager.unlock_alert(alert.user_id, alert.price)

        if unlock_tasks:
            await asyncio.gather(*unlock_tasks, return_exceptions=True)

        if trigger_batch:
            logger.info(f"准备触发 {len(trigger_batch)} 个提醒任务")
            for i in range(0, len(trigger_batch), MAX_TRIGGER_BATCH):
                batch = trigger_batch[i:i + MAX_TRIGGER_BATCH]
                batch_num = i // MAX_TRIGGER_BATCH + 1
                total_batches = (len(trigger_batch) + MAX_TRIGGER_BATCH - 1) // MAX_TRIGGER_BATCH
                logger.info(f"处理第 {batch_num}/{total_batches} 批触发任务 ({len(batch)} 个)")

                tasks = [
                    asyncio.create_task(self._trigger_alert_safe(alert, price))
                    for alert, price in batch
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

    def _evaluate_alert_action(self, alert: AlertRule, current_price: Decimal) -> str:
        """
        评估单个提醒的状态

        Returns:
            "trigger": 应该触发提醒
            "unlock": 应该解锁（价格超出范围）
            "timeout_unlock": 应该解锁（锁定超时）
            "no_action": 无需操作
        """
        target_price = alert.price
        float_range_decimal = Decimal(str(self.config.float_range))
        lower_bound = target_price - float_range_decimal
        upper_bound = target_price + float_range_decimal

        crossed = self._check_cross(alert, current_price)
        out_of_range = current_price < lower_bound or current_price > upper_bound

        logger.debug(
            f"评估提醒: 用户={alert.user_id}, 设定价=${target_price}, "
            f"当前价=${current_price}, 范围=[${lower_bound}, ${upper_bound}], "
            f"穿过={crossed}, 锁定={alert.is_locked}, 超出范围={out_of_range}"
        )

        if crossed and not alert.is_locked:
            return "trigger"
        elif out_of_range and alert.is_locked:
            return "unlock"
        elif alert.is_locked and self._check_lock_duration(alert):
            return "timeout_unlock"
        return "no_action"

    def _is_out_of_range(self, alert: AlertRule, current_price: Decimal) -> bool:
        """
        检查当前价格是否超出浮动范围

        Args:
            alert: 提醒规则
            current_price: 当前价格

        Returns:
            True: 超出范围
            False: 在范围内
        """
        float_range_decimal = Decimal(str(self.config.float_range))
        lower_bound = alert.price - float_range_decimal
        upper_bound = alert.price + float_range_decimal
        return current_price < lower_bound or current_price > upper_bound

    def _check_cross(self, alert: AlertRule, current_price: Decimal) -> bool:
        """
        检查价格是否穿过设定价
        
        支持两种情况：
        1. 相邻两次查询价格穿过设定价（精准穿过）
        2. 跨周期触发：两次查询价格分别在设定价两侧（如上次1899，当前1901，设定价1900）
        """
        if self._last_price is None:
            return False

        target_price = alert.price
        prev = self._last_price
        curr = current_price

        # 情况1：精准穿过（前次<设定价<=当前 或 前次>设定价>=当前）
        crossed_up = prev < target_price <= curr
        crossed_down = prev > target_price >= curr
        
        # 情况2：跨周期触发（价格跳过设定价）
        # 上次1899，当前1901，设定价1900 -> 触发
        skipped_up = prev < target_price and curr > target_price
        skipped_down = prev > target_price and curr < target_price
        
        if crossed_up or crossed_down or skipped_up or skipped_down:
            cross_type = ""
            if crossed_up:
                cross_type = "上涨穿过"
            elif crossed_down:
                cross_type = "下跌穿过"
            elif skipped_up:
                cross_type = "上涨跳过"
            elif skipped_down:
                cross_type = "下跌跳过"
            logger.info(f"价格{cross_type}: 设定价=${target_price}, 前次=${prev}, 当前=${curr}")
            return True
        return False

    def _check_lock_duration(self, alert: AlertRule) -> bool:
        """检查锁定是否超时"""
        if not alert.lock_time:
            return True

        try:
            lock_dt = datetime.strptime(alert.lock_time, "%Y-%m-%d %H:%M:%S")
            elapsed = (datetime.now() - lock_dt).total_seconds()
            return elapsed >= self.config.lock_duration
        except (ValueError, TypeError):
            # 解析失败时保守处理：不自动解锁，等待下次正常判断
            logger.warning(f"锁定时间解析失败: {alert.lock_time}，保持锁定状态")
            return False

    async def _trigger_alert(self, alert: AlertRule, current_price: Decimal) -> None:
        """触发提醒"""
        logger.info(f"触发提醒: 用户 {alert.user_id}, 设定价 ${alert.price}, 当前价 ${current_price}")

        try:
            send_success = await self._send_alert_messages(alert, current_price)
            if send_success:
                self.data_manager.lock_alert(alert.user_id, alert.price)
            else:
                logger.warning(f"发送失败，锁定提醒以防止重复触发: 用户 {alert.user_id}")
                self.data_manager.lock_alert(alert.user_id, alert.price)
                self.data_manager.increment_send_fail(alert.user_id, alert.price)
        except Exception as e:
            logger.error(f"发送提醒消息失败: 用户 {alert.user_id}, 错误: {e}")
            logger.warning(f"异常情况下仍锁定提醒: 用户 {alert.user_id}")
            try:
                self.data_manager.lock_alert(alert.user_id, alert.price)
            except Exception:
                pass

    async def _trigger_alert_safe(self, alert: AlertRule, current_price: Decimal) -> None:
        """
        安全触发提醒（捕获异常，防止影响其他用户）
        """
        try:
            await self._trigger_alert(alert, current_price)
        except Exception as e:
            logger.error(f"触发提醒失败: 用户 {alert.user_id}, 错误: {e}")

    async def _send_alert_messages(self, alert: AlertRule, current_price: Decimal) -> bool:
        """
        发送提醒消息

        Returns:
            True: 至少发送成功一次
            False: 全部发送失败
        """
        target_price = alert.price
        success_count = 0
        fail_count = 0

        for i in range(self.config.alert_count):
            if not self._running:
                break

            message = f"🔔 黄金到达设定价格：{target_price}，当前价格：{current_price}"
            sent = await self.send_message(alert.session_id, message, alert.user_id, alert.is_group)

            if sent:
                success_count += 1
                self.data_manager.reset_send_fail(alert.user_id, alert.price)
            else:
                fail_count += 1
                count = self.data_manager.increment_send_fail(alert.user_id, alert.price)

                if count >= MAX_SEND_FAIL_COUNT:
                    self.data_manager.disable_alert(alert.user_id, alert.price)
                    await self._notify_admin_disabled(alert.user_id, alert.price)
                    logger.warning(f"用户 {alert.user_id} 提醒被禁用（连续发送失败）")
                    break

            if i < self.config.alert_count - 1:
                await asyncio.sleep(self.config.alert_interval)

        logger.info(f"提醒发送完成: 用户 {alert.user_id}, 成功 {success_count}, 失败 {fail_count}")
        return success_count > 0

    async def _notify_out_of_range(self, alert: AlertRule, current_price: Decimal) -> None:
        """通知价格超出浮动范围"""
        message = f"⚠️ 黄金价格已超出您设定提醒的浮动范围（±{self.config.float_range}美元），当前价格：{current_price}"
        await self.send_message(alert.session_id, message, alert.user_id, alert.is_group)

    async def _notify_admin_disabled(self, user_id: str, price: Decimal) -> None:
        """通知管理员用户被禁用"""
        if self._notify_admin:
            await self._notify_admin(
                f"⚠️ 用户 {user_id} 提醒已被禁用，原因：连续{MAX_SEND_FAIL_COUNT}次消息发送失败（价格: {price}）"
            )

    def is_running(self) -> bool:
        """检查监控是否运行中"""
        return self._running and not self._stopped

    def get_last_price(self) -> Optional[Decimal]:
        """获取最后价格"""
        return self._last_price
