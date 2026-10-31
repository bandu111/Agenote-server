"""LangGraph 只执行 Hermes 给定的受限步骤，不自行再次规划。"""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt


class ToolState(TypedDict, total=False):
    approved: bool
    index: int
    observations: dict
    status: str


def build_tool_workflow(saver, tools, owner, plan):
    graph = StateGraph(ToolState)

    def approval(state):
        if not any(s['tool'] not in {'knowledge_search', 'knowledge_read', 'progress_read', 'task_read'} for s in plan.content['steps']):
            return {'approved': True}
        allowed = interrupt({'kind': 'tool_plan', 'title': plan.content['title'], 'fingerprint': plan.content['fingerprint']})
        return {'approved': allowed is True, 'status': 'approved' if allowed is True else 'declined'}

    async def execute(state):
        index = state.get('index', 0)
        step = plan.content['steps'][index]
        from app.integrations.hermes.langchain_adapter import execution_tool
        from app.modules.execution.tools import resolve_args
        observations = state.get('observations', {})
        tool = execution_tool(tools, owner, plan, step, observations)
        result = await tool.ainvoke(resolve_args(step['args'], observations))
        if result.get('verified') is not True: raise ValueError('tool_receipt_unverified')
        return {'observations': {**state.get('observations', {}), step['id']: result}, 'index': index + 1}

    def finished(state): return {'status': 'completed'}
    graph.add_node('approval', approval)
    graph.add_node('execute', execute)
    graph.add_node('finished', finished)
    graph.add_edge(START, 'approval')
    graph.add_conditional_edges('approval', lambda s: 'execute' if s['approved'] else END)
    graph.add_conditional_edges('execute', lambda s: 'execute' if s['index'] < len(plan.content['steps']) else 'finished')
    graph.add_edge('finished', END)
    return graph.compile(checkpointer=saver)
