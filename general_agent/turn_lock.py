"""按 key 的异步互斥锁注册表：串行化同一资源（如 session）的并发任务。

用于 POST /chat 的 producer：同一 session 的轮次必须串行（后轮等前轮落库完毕再载入历史），
不同 session 互不阻塞。

回收正确性：进入者在 await 获取锁**之前**即递增引用计数（同步执行，事件循环内无切换点），
故"锁已释放但仍有等待者"时 refcount > 0，锁不会被回收；只有 refcount 归零且锁空闲才删除。
避免了"setdefault 取锁 + finally 判 locked 删锁"在释放与唤醒之间的竞态（会误删导致后到者
另建一把新锁、互斥失效）。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class TurnLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def lock(self, key: str):
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
            self._refs[key] = 0
        # 同步递增：等待者在被唤醒前已计数，保证回收判断安全
        self._refs[key] += 1
        try:
            async with lk:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] <= 0 and not lk.locked():
                self._locks.pop(key, None)
                self._refs.pop(key, None)
