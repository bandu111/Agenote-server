"""LangChain 只适配 Hermes 调用协议，不增加另一个 AgentExecutor 规划器。"""

from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool


def hermes_runnable(provider):
    async def invoke(task):
        return await provider.run(task["role"], task["system"], task["payload"])
    return RunnableLambda(invoke)


def execution_tool(tools, owner, plan, step, observations):
    """协议适配只绑定当前账号及批准计划，不向模型开放任意 Service。"""
    from app.modules.execution.schemas import ARGUMENTS
    async def invoke(**values):
        return await tools.call(owner, plan, {**step, 'args': values}, observations)
    return StructuredTool.from_function(name=step['tool'],
        description='执行当前计划中已校验参数的单一步骤，返回服务端核验回执。',
        args_schema=ARGUMENTS[step['tool']], coroutine=invoke)
