"""astrbot_plugin_group_filter 的纯逻辑内核（零 astrbot 依赖，可脱离框架单测）。

三部分职责：

1. 黑名单归一化与命中判定 —— L0 闸门与 L1 处理器层共用同一份语义；
2. 入队闸门（真正最高优先级）—— 包装事件队列的 ``put_nowait`` / ``put``，
   在黑名单群消息进入 EventBus、进入任何插件的 event_filter 之前直接丢弃；
3. 闸门装配 / 停用 / 查询工具 —— 状态锚点挂在**队列对象**上，
   因为插件热重载会清空 ``sys.modules`` 并重置模块级全局变量，而队列对象跨重载存活。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

#: L1 处理器层优先级。注册表按 ``-priority`` 排序，``float("inf")`` 严格大于
#: 任何整数优先级（包括 ``sys.maxsize`` 与 ``sys.maxsize - 1``）。
PIPELINE_PRIORITY = float("inf")

#: 状态锚点属性名（挂在事件队列实例上）
GATE_ATTR = "_astrbot_group_filter_gate"
#: 原始方法的保存位置（只保存一次，保证不叠加多层包装）
ORIG_PUT_NOWAIT_ATTR = "_astrbot_group_filter_orig_put_nowait"
ORIG_PUT_ATTR = "_astrbot_group_filter_orig_put"

GATE_VERSION = "1.1.0"


# --------------------------------------------------------------------------- #
# 一、判定逻辑
# --------------------------------------------------------------------------- #
def normalize_blacklist(raw: Any) -> set[str]:
    """把配置里的黑名单归一化成"去空白后的字符串集合"。

    兼容：None / 字符串 / 数字 / 列表 / 元组 / 集合 / 字典（取 key）。
    """
    if raw is None:
        return set()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, dict):
        items = list(raw.keys())
    else:
        try:
            items = list(raw)
        except TypeError:
            items = [raw]

    result: set[str] = set()
    for item in items:
        try:
            text = str(item).strip()
        except Exception:  # pragma: no cover - 极端脏数据
            continue
        if text:
            result.add(text)
    return result


def event_keys(event: Any) -> tuple[str, str]:
    """提取 ``(group_id, unified_msg_origin)``，任何异常都退化为空串。"""
    group_id = ""
    try:
        raw_group = event.get_group_id()
    except Exception:
        raw_group = None
    if raw_group is not None:
        try:
            group_id = str(raw_group).strip()
        except Exception:  # pragma: no cover
            group_id = ""

    try:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
    except Exception:  # pragma: no cover
        umo = ""

    return group_id, umo


def is_blocked(group_id: str, umo: str, blacklist: Iterable[str]) -> bool:
    """群号命中 或 会话源命中，即视为需要拦截。"""
    if not blacklist:
        return False
    if group_id and group_id in blacklist:
        return True
    return bool(umo and umo in blacklist)


def extract_event(args: tuple, kwargs: dict) -> Any:
    """从入队调用的参数里取出事件对象（兼容位置参数与 ``item=`` 关键字）。"""
    if args:
        return args[0]
    return kwargs.get("item")


# --------------------------------------------------------------------------- #
# 二、闸门包装
# --------------------------------------------------------------------------- #
def _evaluate(args: tuple, kwargs: dict, get_state: Callable[[], Any]) -> Any:
    """命中黑名单时返回 ``(state, event, group_id, umo)``，否则返回 ``None``。"""
    state = get_state()
    if not isinstance(state, dict) or not state.get("enable"):
        return None
    event = extract_event(args, kwargs)
    if event is None:
        return None
    group_id, umo = event_keys(event)
    if not is_blocked(group_id, umo, state.get("blacklist") or ()):
        return None
    return state, event, group_id, umo


def _record_drop(
    state: dict,
    event: Any,
    group_id: str,
    umo: str,
    on_drop: Callable[..., Any] | None,
    on_error: Callable[[Exception], Any],
) -> None:
    stats = state.get("stats")
    if isinstance(stats, dict):
        try:
            stats["dropped_count"] = int(stats.get("dropped_count") or 0) + 1
            stats["last_dropped_at"] = time.time()
            stats["last_dropped_target"] = group_id or umo
        except Exception:  # pragma: no cover
            pass
    if state.get("log") and on_drop is not None:
        try:
            on_drop(event, group_id, umo)
        except Exception as exc:  # 记日志失败也绝不能影响拦截本身
            on_error(exc)


def make_gate(
    original: Callable[..., Any],
    get_state: Callable[[], Any],
    on_drop: Callable[..., Any] | None = None,
    on_error: Callable[[Exception], Any] | None = None,
    *,
    is_async: bool = False,
) -> Callable[..., Any]:
    """构造闸门包装函数：命中黑名单则丢弃（不入队），否则原样转发。

    异常绝对安全：闸门内部任何异常都被吞掉并交给 ``on_error``，
    然后**继续转发**——绝不让闸门炸掉消息接入链路。
    """

    def _error(exc: Exception) -> None:
        if on_error is None:
            return
        try:
            on_error(exc)
        except Exception:  # pragma: no cover
            pass

    if is_async:

        async def gate(*args: Any, **kwargs: Any) -> Any:
            try:
                hit = _evaluate(args, kwargs, get_state)
            except Exception as exc:
                _error(exc)
                hit = None
            if hit is not None:
                _record_drop(hit[0], hit[1], hit[2], hit[3], on_drop, _error)
                return None
            return await original(*args, **kwargs)

        return gate

    def gate_sync(*args: Any, **kwargs: Any) -> Any:
        try:
            hit = _evaluate(args, kwargs, get_state)
        except Exception as exc:
            _error(exc)
            hit = None
        if hit is not None:
            _record_drop(hit[0], hit[1], hit[2], hit[3], on_drop, _error)
            return None
        return original(*args, **kwargs)

    return gate_sync


# --------------------------------------------------------------------------- #
# 三、装配 / 停用 / 查询
# --------------------------------------------------------------------------- #
def build_state(
    *,
    enable: bool,
    blacklist: Any,
    log: bool,
    plugin: Any = None,
) -> dict:
    """构造闸门状态锚点。"""
    return {
        "enable": bool(enable),
        "blacklist": normalize_blacklist(blacklist),
        "log": bool(log),
        "plugin": plugin,
        "version": GATE_VERSION,
        "stats": {
            "dropped_count": 0,
            "last_dropped_at": None,
            "last_dropped_target": None,
        },
    }


def install_gate(
    queue: Any,
    state: dict,
    on_drop: Callable[..., Any] | None = None,
    on_error: Callable[[Exception], Any] | None = None,
) -> bool:
    """幂等安装 / 升级闸门。

    - 原始方法只保存一次，保证不叠加多层包装；
    - 每次调用都把队列方法重新绑定为**最新实现**的包装函数，
      因此插件热重载会自动升级旧包装代码；
    - 状态从队列的 ``__dict__`` 里实时读取，旧包装也能看到最新状态。
    """
    if queue is None:
        return False
    qdict = getattr(queue, "__dict__", None)
    if not isinstance(qdict, dict):
        return False

    orig_nowait = qdict.get(ORIG_PUT_NOWAIT_ATTR)
    if orig_nowait is None:
        orig_nowait = getattr(queue, "put_nowait", None)
        if orig_nowait is None:
            return False
        qdict[ORIG_PUT_NOWAIT_ATTR] = orig_nowait

    orig_put = qdict.get(ORIG_PUT_ATTR)
    if orig_put is None:
        orig_put = getattr(queue, "put", None)
        if orig_put is not None:
            qdict[ORIG_PUT_ATTR] = orig_put

    qdict[GATE_ATTR] = state

    def get_state() -> Any:
        return qdict.get(GATE_ATTR)

    try:
        queue.put_nowait = make_gate(orig_nowait, get_state, on_drop, on_error)
        if orig_put is not None:
            queue.put = make_gate(
                orig_put,
                get_state,
                on_drop,
                on_error,
                is_async=True,
            )
    except Exception:
        return False
    return True


def gate_state(queue: Any) -> dict | None:
    """读取当前闸门状态（未装载返回 None）。"""
    state = getattr(queue, GATE_ATTR, None)
    return state if isinstance(state, dict) else None


def gate_armed(queue: Any) -> bool:
    """闸门是否处于"已装载 且 拦截开启"状态。"""
    state = gate_state(queue)
    return bool(state and state.get("enable"))


def gate_dropped_count(queue: Any) -> int:
    """累计丢弃条数。"""
    state = gate_state(queue)
    if not state:
        return 0
    stats = state.get("stats") or {}
    try:
        return int(stats.get("dropped_count") or 0)
    except Exception:  # pragma: no cover
        return 0


def set_gate_enabled(queue: Any, enabled: bool) -> bool:
    """运行时开关闸门（不重新装载，保留统计）。"""
    state = gate_state(queue)
    if not state:
        return False
    state["enable"] = bool(enabled)
    return True


def disable_gate(queue: Any) -> None:
    """停用闸门并把队列方法还原为原始实现（插件被禁用 / 卸载时调用）。

    还原而非仅置 ``enable=False``，是为了不给框架留下无用的 monkey-patch 残留。
    """
    if queue is None:
        return
    qdict = getattr(queue, "__dict__", None)
    if not isinstance(qdict, dict):
        return

    state = qdict.get(GATE_ATTR)
    if isinstance(state, dict):
        state["enable"] = False

    orig_nowait = qdict.get(ORIG_PUT_NOWAIT_ATTR)
    if orig_nowait is not None:
        try:
            queue.put_nowait = orig_nowait
        except Exception:  # pragma: no cover
            pass

    orig_put = qdict.get(ORIG_PUT_ATTR)
    if orig_put is not None:
        try:
            queue.put = orig_put
        except Exception:  # pragma: no cover
            pass
