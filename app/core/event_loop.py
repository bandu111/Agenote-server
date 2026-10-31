"""应用服务器使用的事件循环工厂。"""
import asyncio
import sys


def psycopg_compatible_loop_factory() -> asyncio.AbstractEventLoop:
    """创建兼容 Psycopg 异步连接的事件循环。"""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
