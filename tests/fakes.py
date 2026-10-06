"""测试替身（零 astrbot 依赖）。"""


class FakeEvent:
    """模拟 AstrMessageEvent 的最小接口。"""

    def __init__(
        self,
        group_id=None,
        umo: str = "",
        sender_name: str = "Tester",
        sender_id: str = "10001",
        boom: bool = False,
    ):
        self._group_id = group_id
        self.unified_msg_origin = umo
        self._sender_name = sender_name
        self._sender_id = sender_id
        self._boom = boom
        self.stopped = False

    def get_group_id(self):
        if self._boom:
            raise RuntimeError("get_group_id boom")
        return self._group_id

    def get_sender_name(self):
        return self._sender_name

    def get_sender_id(self):
        return self._sender_id

    def stop_event(self):
        self.stopped = True


class FakeQueue:
    """模拟 asyncio.Queue 的最小接口：同步 put_nowait + 异步 put。"""

    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)

    async def put(self, item):
        self.items.append(item)


class ExplodingState(dict):
    """``get()`` 抛异常的状态字典，用于验证闸门的异常安全。"""

    def get(self, *args, **kwargs):
        raise RuntimeError("state boom")
