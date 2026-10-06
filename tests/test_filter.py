"""原有核心语义测试（重写为调用 filter_core 的真实实现，不再复制算法）。

覆盖：
1. 黑名单群消息被拦截；
2. 非黑名单群消息畅通放行；
3. 插件总开关关闭时全部放行；
4. （新增）L0 闸门关闭时，L1 处理器层仍能兜底拦截。

运行：
    cd <插件目录> && python3 -m unittest tests.test_filter -v
"""

import pathlib
import sys
import unittest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(_PLUGIN_DIR), str(_PLUGIN_DIR / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import filter_core  # noqa: E402

try:  # 作为 tests 包的一部分导入
    from tests.fakes import FakeEvent, FakeQueue
except ImportError:  # 兼容 -s tests -t tests 的发现方式
    from fakes import FakeEvent, FakeQueue  # type: ignore[no-redef]


def dispatch(config: dict, event: FakeEvent) -> bool:
    """模拟真实链路：先过 L0 入队闸门，再过 L1 处理器层。

    返回 ``True`` 表示消息被拦截（未入队或被 stop_event）。
    """
    enabled = config.get("enable", True)
    queue = FakeQueue()
    state = filter_core.build_state(
        enable=enabled and config.get("adapter_gate", True),
        blacklist=config.get("blacklisted_groups", []),
        log=config.get("log_intercept", True),
    )
    filter_core.install_gate(queue, state)

    queue.put_nowait(event)  # ---- L0 ----
    if event not in queue.items:
        return True

    # ---- L1：模拟 intercept_group_message ----
    blacklist = filter_core.normalize_blacklist(config.get("blacklisted_groups", []))
    group_id, umo = filter_core.event_keys(event)
    if enabled and filter_core.is_blocked(group_id, umo, blacklist):
        event.stop_event()
    return event.stopped


class TestGroupFilterLayers(unittest.TestCase):
    def setUp(self):
        self.config = {
            "enable": True,
            "adapter_gate": True,
            "blacklisted_groups": ["123456789", "987654321"],
            "log_intercept": False,
        }

    def test_intercept_blacklisted_group(self):
        """黑名单群的消息必须被拦截（L0 闸门先手丢弃）"""
        event = FakeEvent(group_id=123456789, umo="aiocqhttp:GroupMessage:123456789")
        self.assertTrue(dispatch(self.config, event), "黑名单群消息必须被拦截")

    def test_pass_normal_group(self):
        """非黑名单群的正常消息畅通放行"""
        event = FakeEvent(group_id=55555555, umo="aiocqhttp:GroupMessage:55555555")
        self.assertFalse(dispatch(self.config, event), "正常群消息不应被拦截")

    def test_plugin_disabled(self):
        """插件总开关关闭时全部放行"""
        self.config["enable"] = False
        event = FakeEvent(group_id=123456789, umo="aiocqhttp:GroupMessage:123456789")
        self.assertFalse(dispatch(self.config, event), "插件关闭时不应拦截")

    def test_gate_off_falls_back_to_pipeline_layer(self):
        """L0 闸门关闭时，L1 处理器层仍能兜底拦截"""
        self.config["adapter_gate"] = False
        event = FakeEvent(group_id=123456789, umo="aiocqhttp:GroupMessage:123456789")
        self.assertTrue(dispatch(self.config, event), "闸门关闭后 L1 必须兜底")

    def test_umo_entry_matched(self):
        """黑名单支持会话源（umo）形式"""
        self.config["blacklisted_groups"] = ["aiocqhttp:GroupMessage:1000000000"]
        event = FakeEvent(group_id=1000000000, umo="aiocqhttp:GroupMessage:1000000000")
        self.assertTrue(dispatch(self.config, event), "umo 形式条目也必须命中")


if __name__ == "__main__":
    unittest.main()
