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
from typing import Optional, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass
from threading import Lock
import aiohttp
from astrbot.api import logger
from .api import GoldPriceAPI, GoldPrice, fetch_gold_price_with_retry
from .data import DataManager, AlertRule

MAX_SEND_FAIL_COUNT = 3


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
        context,
        data_manager: DataManager,
        config: MonitorConfig,
        api: GoldPriceAPI,
        # 问题15修复：更新类型签名，使用4个参数
        send_message_func: Callable[[str, str, str, bool], Awaitable[bool]],
        notify_admin_func: Callable[[str], Awaitable[None]] | None = None
    ):
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
        self._send_fail_users: dict = {}
        # 问题2修复：添加线程锁保护并发访问
        self._send_fail_lock = Lock()

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
        """
        停止价格监控任务
        
        问题3修复：移除错误的asyncio.shield()调用
        asyncio.shield()用于保护任务不被取消，但会阻止stop()正确终止
        问题6修复：不在此处关闭API，由主类统一管理API生命周期
        问题13修复：添加超时机制，确保任务能被正确取消
        """
        self._running = False
        self._stopped = True
        
        if self._task:
            self._task.cancel()
            try:
                # 问题13修复：添加超时机制
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
                await asyncio.sleep(5)
            except Exception as e:
                logger.critical(f"未预期的监控异常: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _process_price(self, current_price: Decimal) -> None:
        """
        处理当前价格，检查是否触发用户的提醒

        修复问题12：改为分批处理，避免触发任务被跳过导致漏报
        问题14修复：统一将 float_range 转换为 Decimal 进行运算，避免精度问题
        """
        MAX_TRIGGER_BATCH = 50

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

        修复问题12：提取价格判断逻辑为独立方法，降低圈复杂度
        修复问题11：增加DEBUG级别日志

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

        if crossed and not alert.is_locked and not out_of_range:
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
        """检查价格是否穿过设定价"""
        if self._last_price is None:
            return False

        target_price = alert.price
        prev = self._last_price
        curr = current_price

        crossed_up = prev < target_price <= curr
        crossed_down = prev > target_price >= curr
        if crossed_up or crossed_down:
            logger.info(f"价格穿过: 设定价=${target_price}, 前次=${prev}, 当前=${curr}")
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
            return True

    async def _trigger_alert(self, alert: AlertRule, current_price: Decimal) -> None:
        """
        触发提醒

        修复问题11：只在发送成功后才锁定，避免用户没收到提醒却被锁定
        """
        logger.info(f"触发提醒: 用户 {alert.user_id}, 设定价 ${alert.price}, 当前价 ${current_price}")

        try:
            send_success = await self._send_alert_messages(alert, current_price)
            if send_success:
                self.data_manager.lock_alert(alert.user_id, alert.price)
            else:
                logger.warning(f"发送失败，不锁定提醒: 用户 {alert.user_id}")
        except Exception as e:
            logger.error(f"发送提醒消息失败: 用户 {alert.user_id}, 错误: {e}")
            raise

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

        修复问题11：返回发送结果，调用方根据结果决定是否锁定

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
                self._clear_send_fail(alert.user_id)
            else:
                fail_count += 1
                count = self.data_manager.increment_send_fail(alert.user_id, alert.price)
                self._record_send_fail(alert.user_id, count)

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

    def _record_send_fail(self, user_id: str, count: int) -> None:
        """
        记录发送失败
        
        问题2修复：使用锁保护并发访问，防止竞态条件
        """
        with self._send_fail_lock:
            self._send_fail_users[user_id] = count

    def _clear_send_fail(self, user_id: str) -> None:
        """
        清除发送失败记录
        
        问题2修复：使用锁保护并发访问，防止竞态条件
        """
        with self._send_fail_lock:
            if user_id in self._send_fail_users:
                del self._send_fail_users[user_id]

    def is_running(self) -> bool:
        """检查监控是否运行中"""
        return self._running and not self._stopped

    def get_last_price(self) -> Optional[Decimal]:
        """获取最后价格"""
        return self._last_price
