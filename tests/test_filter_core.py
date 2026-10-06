"""filter_core 纯逻辑单测（stdlib unittest，零 astrbot 依赖）。

运行：
    cd <插件目录> && python3 -m unittest tests.test_filter_core -v
"""

import asyncio
import pathlib
import sys
import unittest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(_PLUGIN_DIR), str(_PLUGIN_DIR / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import filter_core  # noqa: E402

try:  # 作为 tests 包的一部分导入
    from tests.fakes import ExplodingState, FakeEvent, FakeQueue
except ImportError:  # 兼容 -s tests -t tests 的发现方式
    from fakes import ExplodingState, FakeEvent, FakeQueue  # type: ignore[no-redef]


class TestNormalizeBlacklist(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(filter_core.normalize_blacklist(None), set())
        self.assertEqual(filter_core.normalize_blacklist([]), set())
        self.assertEqual(filter_core.normalize_blacklist(""), set())

    def test_whitespace_and_types(self):
        self.assertEqual(
            filter_core.normalize_blacklist([" 123 ", "", "  ", 456]),
            {"123", "456"},
        )

    def test_scalar_and_other_containers(self):
        self.assertEqual(filter_core.normalize_blacklist(999), {"999"})
        self.assertEqual(filter_core.normalize_blacklist("999"), {"999"})
        self.assertEqual(filter_core.normalize_blacklist(("a", "b")), {"a", "b"})
        self.assertEqual(filter_core.normalize_blacklist({"x": 1}), {"x"})


class TestIsBlocked(unittest.TestCase):
    def test_group_id_hit(self):
        self.assertTrue(filter_core.is_blocked("1000000000", "", {"1000000000"}))

    def test_umo_hit(self):
        umo = "aiocqhttp:GroupMessage:1000000000"
        self.assertTrue(filter_core.is_blocked("", umo, {umo}))

    def test_miss_and_empty_blacklist(self):
        self.assertFalse(filter_core.is_blocked("555", "", {"1000000000"}))
        self.assertFalse(filter_core.is_blocked("1000000000", "", set()))


class TestEventKeys(unittest.TestCase):
    def test_normal(self):
        event = FakeEvent(group_id=123456789, umo="aiocqhttp:GroupMessage:123456789")
        self.assertEqual(
            filter_core.event_keys(event),
            ("123456789", "aiocqhttp:GroupMessage:123456789"),
        )

    def test_none_group(self):
        event = FakeEvent(group_id=None, umo="aiocqhttp:FriendMessage:1")
        self.assertEqual(
            filter_core.event_keys(event),
            ("", "aiocqhttp:FriendMessage:1"),
        )

    def test_getter_raises_is_swallowed(self):
        event = FakeEvent(group_id=1, umo="u", boom=True)
        self.assertEqual(filter_core.event_keys(event), ("", "u"))


class TestGate(unittest.TestCase):
    def setUp(self):
        self.queue = FakeQueue()
        self.drops = []
        self.errors = []

    def _install(self, *, enable=True, blacklist=("1000000000",), log=True):
        state = filter_core.build_state(
            enable=enable,
            blacklist=blacklist,
            log=log,
        )
        ok = filter_core.install_gate(
            self.queue,
            state,
            on_drop=lambda event, gid, umo: self.drops.append((gid, umo)),
            on_error=self.errors.append,
        )
        self.assertTrue(ok, "闸门应安装成功")
        return state

    # ---------------- 基础行为 ---------------- #
    def test_blacklisted_event_dropped(self):
        self._install()
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [], "黑名单群消息必须不入队")
        self.assertEqual(len(self.drops), 1)
        self.assertEqual(self.drops[0][0], "1000000000")

    def test_normal_group_passthrough(self):
        self._install()
        event = FakeEvent(group_id=55555555)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "非黑名单消息必须原样入队")
        self.assertEqual(self.drops, [])

    def test_umo_form_entry_matched(self):
        umo = "aiocqhttp:GroupMessage:1000000000"
        self._install(blacklist=(umo,))
        event = FakeEvent(group_id=None, umo=umo)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [])
        self.assertEqual(len(self.drops), 1)

    def test_disabled_gate_passthrough(self):
        self._install(enable=False)
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "闸门关闭时必须透传")

    def test_empty_blacklist_passthrough(self):
        self._install(blacklist=())
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event])

    def test_async_put_channel_also_gated(self):
        self._install()
        blocked = FakeEvent(group_id=1000000000)
        normal = FakeEvent(group_id=555)
        asyncio.run(self.queue.put(blocked))
        asyncio.run(self.queue.put(normal))
        self.assertEqual(self.queue.items, [normal], "异步 put 通道也必须被拦截")

    def test_stats_counted(self):
        state = self._install()
        self.queue.put_nowait(FakeEvent(group_id=1000000000))
        self.queue.put_nowait(FakeEvent(group_id=1000000000))
        self.assertEqual(state["stats"]["dropped_count"], 2)
        self.assertEqual(state["stats"]["last_dropped_target"], "1000000000")
        self.assertIsNotNone(state["stats"]["last_dropped_at"])
        self.assertEqual(filter_core.gate_dropped_count(self.queue), 2)

    # ---------------- 异常安全 ---------------- #
    def test_event_getter_exception_still_forwards(self):
        self._install()
        event = FakeEvent(group_id=1, umo="u", boom=True)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "取值异常时必须放行")
        self.assertEqual(len(self.errors), 0, "取值异常已在 event_keys 内部消化")

    def test_state_provider_exception_still_forwards(self):
        self._install()
        self.queue.__dict__[filter_core.GATE_ATTR] = ExplodingState()
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "状态读取异常时必须放行")
        self.assertEqual(len(self.errors), 1, "异常应上报 on_error")

    def test_drop_callback_exception_still_blocks(self):
        state = filter_core.build_state(
            enable=True,
            blacklist=("1000000000",),
            log=True,
        )
        filter_core.install_gate(
            self.queue,
            state,
            on_drop=lambda event, gid, umo: (_ for _ in ()).throw(RuntimeError("log boom")),
            on_error=self.errors.append,
        )
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [], "日志回调异常不应导致消息被放行")
        self.assertEqual(len(self.errors), 1)

    # ---------------- 装配语义 ---------------- #
    def test_install_is_idempotent_and_keeps_original(self):
        self._install()
        self._install()
        original = self.queue.__dict__[filter_core.ORIG_PUT_NOWAIT_ATTR]
        self.assertIs(
            getattr(original, "__func__", None),
            FakeQueue.put_nowait,
            "重复安装后保存的必须仍是队列的原始方法，不能是包装函数",
        )
        event = FakeEvent(group_id=555)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "同一条消息只能入队一次（未叠加包装）")

    def test_reinstall_replaces_state(self):
        self._install(blacklist=("111",))
        self._install(blacklist=("222",))
        stale = FakeEvent(group_id=111)
        fresh = FakeEvent(group_id=222)
        self.queue.put_nowait(stale)
        self.queue.put_nowait(fresh)
        self.assertEqual(self.queue.items, [stale], "重新安装后应以最新状态为准")

    def test_failed_install_on_unsupported_target(self):
        state = filter_core.build_state(enable=True, blacklist=("1",), log=False)
        self.assertFalse(filter_core.install_gate(None, state))
        self.assertFalse(filter_core.install_gate(object(), state))

    def test_armed_and_set_enabled(self):
        self._install()
        self.assertTrue(filter_core.gate_armed(self.queue))
        self.assertTrue(filter_core.set_gate_enabled(self.queue, False))
        self.assertFalse(filter_core.gate_armed(self.queue))
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event], "运行时关闭后应透传")

    def test_disable_gate_restores_original_methods(self):
        self._install()
        filter_core.disable_gate(self.queue)
        self.assertFalse(filter_core.gate_armed(self.queue))
        self.assertIs(
            getattr(self.queue.put_nowait, "__func__", None),
            FakeQueue.put_nowait,
            "停用后队列方法必须还原为原始实现",
        )
        self.assertIs(
            getattr(self.queue.put, "__func__", None),
            FakeQueue.put,
        )
        event = FakeEvent(group_id=1000000000)
        self.queue.put_nowait(event)
        self.assertEqual(self.queue.items, [event])


class TestPriorityInvariant(unittest.TestCase):
    """"最高优先级"必须可被测试守住。"""

    def test_pipeline_priority_beats_any_int(self):
        self.assertEqual(filter_core.PIPELINE_PRIORITY, float("inf"))
        self.assertGreater(filter_core.PIPELINE_PRIORITY, sys.maxsize)
        self.assertGreater(filter_core.PIPELINE_PRIORITY, sys.maxsize - 1)
        self.assertGreater(filter_core.PIPELINE_PRIORITY, 99999)


if __name__ == "__main__":
    unittest.main()
