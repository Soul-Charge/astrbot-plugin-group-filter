"""astrbot_plugin_group_filter —— 监控群源头静默与阻断过滤器（双层拦截）。

- **L0 入队闸门**：包装 AstrBot 事件队列的 ``put_nowait`` / ``put``，
  在黑名单群消息进入 ``EventBus``、进入任何插件的 ``event_filter`` 之前直接丢弃。
  这是唯一能真正保证"所有其他插件都在它后面"的位置（详见随附改造计划文档）。
- **L1 处理器层兜底**：``priority=float("inf")`` 的 handler，
  在闸门失效（框架升级／被其他插件覆盖）时第一时间 ``stop_event()``。
"""

import time
import weakref

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 常规加载路径：插件属于 data.plugins.<dir> 命名空间包
    from . import filter_core
except ImportError:  # pragma: no cover - 兼容非包方式导入
    import filter_core  # type: ignore[no-redef]


PLUGIN_NAME = "astrbot_plugin_group_filter"


@register(
    PLUGIN_NAME,
    "NaE",
    "监控群源头静默与阻断过滤器 - 入队前闸门 + 处理器层最高优先级双层拦截，杜绝误回复与Token消耗",
    "1.1.0",
)
class GroupFilterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._sync_gate(reason="plugin_init")

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _gate_enabled(self) -> bool:
        return bool(self.config.get("adapter_gate", True))

    def _log_enabled(self) -> bool:
        return bool(self.config.get("log_intercept", True))

    def _blacklist(self) -> set[str]:
        return filter_core.normalize_blacklist(
            self.config.get("blacklisted_groups", []),
        )

    def _save(self) -> None:
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception:
                logger.error("[GroupFilter] 保存插件配置失败", exc_info=True)

    def _wake_prefix(self) -> str:
        """读取 AstrBot 的唤醒前缀；为空时指令不能带前缀（例如不能写 /ignoregroup）。"""
        try:
            prefixes = self.context.get_config().get("wake_prefix") or []
        except Exception:
            prefixes = []
        if isinstance(prefixes, str):
            return prefixes
        try:
            return str(prefixes[0]) if prefixes else ""
        except Exception:
            return ""

    def _cmd_form(self, name: str) -> str:
        return f"{self._wake_prefix()}{name}"

    # ------------------------------------------------------------------ #
    # L0：入队闸门
    # ------------------------------------------------------------------ #
    def _get_queue(self):
        """拿到 AstrBot 的事件队列（所有平台适配器共用同一份）。"""
        manager = getattr(self.context, "platform_manager", None)
        queue = getattr(manager, "event_queue", None)
        if queue is not None and hasattr(queue, "put_nowait"):
            return queue

        getter = getattr(self.context, "get_event_queue", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                logger.debug("[GroupFilter] get_event_queue() 调用失败", exc_info=True)
        return None

    def _sync_gate(self, reason: str = "sync") -> bool:
        """安装 / 刷新入队闸门（幂等，任何异常都不影响插件加载）。"""
        queue = self._get_queue()
        if queue is None:
            logger.warning(
                "[GroupFilter] 未找到事件队列，L0 入队闸门未装载"
                "（L1 处理器层拦截仍生效）",
            )
            return False

        state = filter_core.build_state(
            enable=self._enabled() and self._gate_enabled(),
            blacklist=self._blacklist(),
            log=self._log_enabled(),
            plugin=weakref.ref(self),
        )
        try:
            installed = filter_core.install_gate(
                queue,
                state,
                on_drop=self._on_gate_drop,
                on_error=self._on_gate_error,
            )
        except Exception:
            logger.error("[GroupFilter] 装载入队闸门失败", exc_info=True)
            return False

        if installed:
            logger.info(
                f"[GroupFilter][Gate] 入队闸门已装载/刷新（{reason}）："
                f"拦截={'开' if state['enable'] else '关'}，"
                f"黑名单={sorted(state['blacklist']) or '空'}",
            )
        return installed

    def _on_gate_drop(self, event, group_id: str, umo: str) -> None:
        sender = ""
        try:
            sender = event.get_sender_name() or event.get_sender_id() or ""
        except Exception:
            sender = ""
        logger.info(
            f"[GroupFilter][Gate] 监控群 [{group_id or umo}] 消息已在入队前丢弃"
            f"（发送者: {sender}），不会进入任何插件与 LLM",
        )

    @staticmethod
    def _on_gate_error(exc: Exception) -> None:
        logger.error(f"[GroupFilter][Gate] 闸门内部异常（该消息已放行）: {exc!r}")

    # ------------------------------------------------------------------ #
    # L1：处理器层兜底
    # ------------------------------------------------------------------ #
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=filter_core.PIPELINE_PRIORITY,
    )
    async def intercept_group_message(self, event: AstrMessageEvent):
        """最高优先级兜底：闸门失效时，在处理器层终止事件传播。"""
        if not self._enabled():
            return

        blacklist = self._blacklist()
        if not blacklist:
            return

        group_id, umo = filter_core.event_keys(event)
        if not filter_core.is_blocked(group_id, umo, blacklist):
            return

        if self._log_enabled():
            try:
                sender = event.get_sender_name() or event.get_sender_id()
            except Exception:
                sender = ""
            logger.info(
                f"[GroupFilter][Pipeline] 监控群 [{group_id or umo}] 消息在处理器层拦截"
                f"（发送者: {sender}），后续插件与 LLM 均不可见",
            )
        # 立即掐断事件传播：后续所有插件 handler 与主回复管道都不可见该消息
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 生命周期：最大化闸门的存活与自愈能力
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        self._sync_gate(reason="initialize")

    async def terminate(self) -> None:
        queue = self._get_queue()
        if queue is not None:
            filter_core.disable_gate(queue)
        logger.info("[GroupFilter] 插件已停用，入队闸门已撤销并还原队列方法")

    @filter.on_platform_loaded()
    async def on_platform_loaded_hook(self):
        self._sync_gate(reason="platform_loaded")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded_hook(self, *args, **kwargs):
        self._sync_gate(reason="plugin_loaded")

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    @staticmethod
    def _handler_priority(handler) -> object:
        extras = getattr(handler, "extras_configs", None)
        if isinstance(extras, dict):
            return extras.get("priority", 0)
        return getattr(handler, "priority", 0)

    def _handler_order_lines(self) -> list[str]:
        """列出 AdapterMessageEvent 处理器顺序，用于验证"本插件排第一"。"""
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )

            handlers = list(
                star_handlers_registry.get_handlers_by_event_type(
                    EventType.AdapterMessageEvent,
                ),
            )
        except Exception as exc:
            return [f"• 无法读取处理器注册表: {exc!r}"]

        lines: list[str] = []
        mine = None
        for index, handler in enumerate(handlers):
            module = getattr(handler, "handler_module_path", "") or ""
            name = getattr(handler, "handler_name", "") or ""
            is_mine = (
                module.endswith("astrbot_plugin_group_filter.main")
                and name == "intercept_group_message"
            )
            if is_mine and mine is None:
                mine = index
            if index < 5 or is_mine:
                mark = "  ← 本插件" if is_mine else ""
                lines.append(
                    f"  {index + 1}. {name} (priority={self._handler_priority(handler)}){mark}",
                )

        if mine == 0:
            lines.append("  ✅ 本插件 handler 排在第 1 位")
        elif mine is None:
            lines.append("  ⚠️ 未在注册表中找到本插件 handler（插件是否已启用？）")
        else:
            lines.append(
                f"  ⚠️ 检测到 {mine} 个更靠前的处理器，本插件位于第 {mine + 1} 位",
            )
        return lines

    def _status_text(self) -> str:
        queue = self._get_queue()
        state = filter_core.gate_state(queue) if queue is not None else None
        armed = filter_core.gate_armed(queue) if queue is not None else False
        dropped = filter_core.gate_dropped_count(queue) if queue is not None else 0
        blacklist = sorted(self._blacklist())

        plugin_alive = "-"
        ref = (state or {}).get("plugin")
        if ref is not None:
            try:
                plugin_alive = "是" if ref() is not None else "否（实例已回收）"
            except Exception:
                plugin_alive = "-"

        lines = [
            "【GroupFilter 状态】",
            f"• 插件总开关: {'开启' if self._enabled() else '关闭'}",
            f"• 入队闸门(adapter_gate): {'开启' if self._gate_enabled() else '关闭'}"
            f" | 已装载并生效: {'是' if armed else '否'}",
            f"• 拦截日志: {'开启' if self._log_enabled() else '关闭'}",
            f"• 指令形式: {self._cmd_form('ignoregroup')} ..."
            f"（唤醒前缀: {self._wake_prefix() or '无'}）",
            f"• 累计丢弃: {dropped} 条",
            f"• 黑名单({len(blacklist)}): {', '.join(blacklist) if blacklist else '空'}",
            f"• 闸门状态版本: {(state or {}).get('version', '-')} | 插件实例存活: {plugin_alive}",
        ]

        stats = (state or {}).get("stats") or {}
        if stats.get("last_dropped_at"):
            stamp = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(stats["last_dropped_at"]),
            )
            lines.append(
                f"• 最近一次拦截: {stamp} @ {stats.get('last_dropped_target') or '-'}",
            )

        lines.append("")
        lines.append("【处理器优先级顺序（AdapterMessageEvent 前 5）】")
        lines.extend(self._handler_order_lines())
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 管理指令：/ignoregroup [status|list|add|del|gate] ...
    # ------------------------------------------------------------------ #
    @filter.command("ignoregroup")
    async def ignore_group_command(self, event: AstrMessageEvent):
        """管理员指令：ignoregroup [status|list|add|del|gate] <参数>（前缀随 wake_prefix 配置而定）"""
        if event.role != "admin":
            yield event.plain_result("只有管理员可执行群静默配置指令。")
            return

        parts = event.message_str.strip().split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        target = parts[2].strip() if len(parts) > 2 else ""

        if action == "add":
            if not target:
                yield event.plain_result("格式错误，请使用: /ignoregroup add <群号>")
                return
            current = [str(x) for x in self.config.get("blacklisted_groups", [])]
            if target in current:
                yield event.plain_result(f"群 [{target}] 已在静默黑名单中。")
                return
            current.append(target)
            self.config["blacklisted_groups"] = current
            self._save()
            self._sync_gate(reason="blacklist_add")
            yield event.plain_result(
                f"已将群 [{target}] 加入静默黑名单，入队闸门已即时生效。",
            )

        elif action in ("del", "remove", "rm"):
            if not target:
                yield event.plain_result("格式错误，请使用: /ignoregroup del <群号>")
                return
            current = [str(x) for x in self.config.get("blacklisted_groups", [])]
            remaining = [x for x in current if x != target]
            if len(remaining) == len(current):
                yield event.plain_result(f"群 [{target}] 不在静默黑名单内。")
                return
            self.config["blacklisted_groups"] = remaining
            self._save()
            self._sync_gate(reason="blacklist_del")
            yield event.plain_result(
                f"已将群 [{target}] 移出静默黑名单，该群消息恢复正常流转。",
            )

        elif action == "gate":
            value = target.lower()
            if value not in ("on", "off"):
                yield event.plain_result("格式错误，请使用: /ignoregroup gate <on|off>")
                return
            self.config["adapter_gate"] = value == "on"
            self._save()
            self._sync_gate(reason="gate_switch")
            yield event.plain_result(
                f"入队闸门已{'开启' if value == 'on' else '关闭'}"
                f"（关闭后仅保留处理器层最高优先级拦截）。",
            )

        elif action == "list":
            blacklist = sorted(self._blacklist())
            if not blacklist:
                yield event.plain_result(
                    "当前 AstrBot 没有被静默的群。\n可使用 /ignoregroup add <群号> 添加。",
                )
                return
            body = "\n".join(f"  • {g}" for g in blacklist)
            yield event.plain_result(f"当前 AstrBot 源头静默黑名单群列表：\n{body}")

        elif action == "status":
            yield event.plain_result(self._status_text())

        else:
            cmd = self._cmd_form("ignoregroup")
            prefix = self._wake_prefix()
            hint = (
                f"\n（注意：当前唤醒前缀为空，请直接发送上面的指令，不要加 /）"
                if not prefix
                else f"\n（当前唤醒前缀：{prefix}）"
            )
            yield event.plain_result(
                f"用法：\n"
                f"  {cmd} status         查看闸门状态与处理器优先级顺序\n"
                f"  {cmd} list           查看静默黑名单\n"
                f"  {cmd} add <群号>     加入静默黑名单\n"
                f"  {cmd} del <群号>     移出静默黑名单\n"
                f"  {cmd} gate <on|off>  开关入队闸门（处理器层拦截始终保留）"
                f"{hint}",
            )
