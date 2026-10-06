# astrbot_plugin_group_filter

监控群源头静默与阻断过滤器。**入队前闸门（L0）+ 处理器层最高优先级（L1）** 双层拦截，确保黑名单群的消息不会流向任何其他插件与 LLM。

## 为什么需要两层

AstrBot v4 的流水线是洋葱模型，阶段顺序（`astrbot/core/pipeline/stage_order.py`）：

```
WakingCheck → WhitelistCheck → SessionStatusCheck → RateLimit → ContentSafety
→ PreProcess → ProcessStage（插件 handler + LLM）→ ResultDecorate → Respond
```

第 1 阶段 `WakingCheckStage` 会**遍历所有已注册 handler 并逐个执行它们的 `event_filters[].filter(event, cfg)`**，而这个循环：

- 没有 `event.is_stopped()` 判断；
- 不会因为某个 handler 优先级更高而提前退出。

也就是说，**`priority` 只能决定"处理函数体"的执行顺序，拦不住其他插件的过滤器**。例如 `astrbot_plugin_livingmemory` 用 `@filter.custom_filter(PassiveGroupCaptureFilter, False)`，它在过滤器里就把消息写进了 `conversations.db`——无论本插件优先级多高都来不及。

唯一早于第 1 阶段的入口是 `Platform.commit_event()` → `event_queue.put_nowait(event)`。因此：

| 层 | 位置 | 作用 |
|---|---|---|
| **L0 入队闸门** | 事件队列 `put_nowait` / `put` | 黑名单消息**根本不入队**：EventBus 不打印、WakingCheck 过滤器不执行、所有插件与 LLM 都看不到 |
| **L1 处理器层** | `@filter.event_message_type(GROUP_MESSAGE, priority=float("inf"))` | 闸门失效（框架升级 / 被其他插件覆盖）时兜底 `stop_event()` |

L0 的状态锚点挂在**队列对象**上（不是模块全局变量），因为插件热重载会清空 `sys.modules` 并重置模块全局；队列对象跨重载存活。

## 配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 插件总开关 |
| `adapter_gate` | bool | `true` | L0 入队闸门开关；关闭后仅保留 L1 |
| `blacklisted_groups` | list | `[]` | 群号或会话源（`aiocqhttp:GroupMessage:群号`） |
| `log_intercept` | bool | `true` | 是否打印拦截日志 |

## 指令（仅管理员）

```
ignoregroup status         查看闸门状态 + 累计丢弃数 + 处理器优先级顺序（验证本插件是否排第 1）
ignoregroup list           查看静默黑名单
ignoregroup add <群号>     加入静默黑名单（闸门即时生效，无需重载）
ignoregroup del <群号>     移出静默黑名单
ignoregroup gate <on|off>  开关 L0 闸门（逃生阀；L1 始终保留）
```

> **前缀规则**：指令写法取决于 AstrBot 的 `wake_prefix` 配置。
> 本部署 `wake_prefix = []`（未配置前缀），因此必须**直接发送 `ignoregroup ...`，加了 `/` 会被当成普通聊天交给 LLM**。
> 若日后配置了前缀（如 `/`），则写成 `/ignoregroup ...`。指令的用法提示与 `status` 输出都会自动显示当前正确形式。

> 黑名单群内的消息会被丢弃，因此该指令需要在**其他群或私聊**中执行。

## 日志标识

- `[GroupFilter][Gate]` —— L0 闸门丢弃（消息未入队）
- `[GroupFilter][Pipeline]` —— L1 处理器层拦截（闸门失效时的兜底路径）

## 已知限制

- 适配器自身的 `[aiocqhttp] RawMessage <Event, {...}>` 是 **DEBUG 日志**，产生于 `commit_event` 之前，插件无法屏蔽；如需彻底不可见请调高日志级别。
- L0 通过包装事件队列实现，属 AstrBot 的非官方扩展点。若将来框架改动入队实现，闸门可能失效——此时 L1 仍生效，且 `/ignoregroup status` 会显示"已装载并生效: 否"。

## 测试

```bash
# 容器内（与线上运行时一致）
docker exec astrbot sh -lc 'cd /AstrBot/data/plugins/astrbot_plugin_group_filter && \
  python3 -m unittest tests.test_filter_core tests.test_filter -v'

# 宿主 Windows
py -3.10 -m unittest discover -s local-docker/astrbot/data/plugins/astrbot_plugin_group_filter/tests -t local-docker/astrbot/data/plugins/astrbot_plugin_group_filter
```

## 目录结构

```
astrbot_plugin_group_filter/
├── main.py             # Star 插件：L1 handler、闸门装配、管理指令、诊断
├── filter_core.py      # 纯逻辑内核（零 astrbot 依赖）：判定 + 闸门 + 装配工具
├── _conf_schema.json   # WebUI 配置 schema
├── metadata.yaml       # 插件元数据
└── tests/              # stdlib unittest
```
