"""LangGraph 只执行固定业务步骤，持久暂停等待授权和手机回读。"""

from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt


class ExecutionState(TypedDict, total=False):
    action: dict
    approved: bool
    receipt: dict
    status: str


def build_workflow(saver):
    graph = StateGraph(ExecutionState)

    def approval(state):
        # 中断前没有外部副作用，因此重启和恢复可安全重复此节点。
        decision = interrupt({"kind": "approval", "action": state["action"]})
        return {
            "approved": decision is True,
            "status": "approved" if decision is True else "declined",
        }

    def device(state):
        receipt = interrupt({"kind": "device_calendar", "action": state["action"]})
        return {"receipt": receipt}

    def verify(state):
        receipt, action = state["receipt"], state["action"]
        verified = (
            receipt.get("verified") is True
            and bool(receipt.get("eventId"))
            and receipt.get("fingerprint") == action["fingerprint"]
            and receipt.get("title") == action["title"]
            and receipt.get("start") == action["start"]
            and receipt.get("end") == action["end"]
        )
        return {"status": "completed" if verified else "retryable"}

    graph.add_node("approval", approval)
    graph.add_node("device", device)
    graph.add_node("verify", verify)
    graph.add_edge(START, "approval")
    graph.add_conditional_edges(
        "approval", lambda s: "device" if s["approved"] else END
    )
    graph.add_edge("device", "verify")
    graph.add_conditional_edges(
        "verify", lambda s: END if s["status"] == "completed" else "device"
    )
    return graph.compile(checkpointer=saver)
