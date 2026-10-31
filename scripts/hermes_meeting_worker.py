"""独立进程运行 Hermes，标准输入接收任务，标准输出只返回结构化结果。"""

import contextlib
import io
import json
import logging
import os
import sys


def main(agent_class=None):
    task = json.loads(sys.stdin.read())
    original_stdout = sys.stdout
    logging.disable(logging.CRITICAL)
    streaming = task.get('stream', False)
    def send(value):
        original_stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
        original_stdout.flush()

    def delta(text):
        # SDK 的 None 表示本轮流结束，不是正文；不转发推理链或工具数据。
        if isinstance(text, str) and text:
            send({'type': 'delta', 'text': text})
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            if agent_class is None:
                from run_agent import AIAgent
            else:
                AIAgent = agent_class

            agent = AIAgent(
                model=task["model"],
                base_url=task.get('base_url', 'https://api.deepseek.com'),
                provider="custom",
                api_key=os.environ[task.get('key_env', 'DEEPSEEK_API_KEY')],
                enabled_toolsets=[],
                max_iterations=1,
                max_tokens=task.get("max_tokens", 1800),
                quiet_mode=True,
                verbose_logging=False,
                save_trajectories=False,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
                reasoning_config=task.get('reasoning', {'enabled': False}),
                run_budget_seconds=60,
                stream_delta_callback=delta if streaming else None,
            )
            # 会议分析不执行工具、不读工作空间，也不把账户会话交给运行时持久化。
            agent.tools = []
            agent._persist_disabled = True
            if streaming:
                send({'type': 'generation_ready'})
            result = agent.run_conversation(
                task["input"], system_message=task["system"]
            )
            response = result.get("final_response", "")
            if hasattr(agent, "close"):
                agent.close()
        if streaming:
            send({'type': 'done', 'ok': True, 'response': response})
        else:
            original_stdout.write(json.dumps({"ok": True, "response": response}, ensure_ascii=False))
    except Exception as error:
        if streaming:
            send({'type': 'error', 'ok': False, 'error_type': type(error).__name__})
        else:
            original_stdout.write(json.dumps({"ok": False, "error_type": type(error).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    if '--preload' in sys.argv:
        # 只预载 SDK，不读任何账户数据；每个进程仍只处理一次请求，避免跨会话记忆。
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from run_agent import AIAgent
        sys.stdout.write('{"type":"ready"}\n')
        sys.stdout.flush()
        main(AIAgent)
    else:
        main()
