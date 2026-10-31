"""兼容旧启动命令，统一转到 app/main.py 启动。"""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

if __name__ == "__main__":
    runpy.run_module("app.main", run_name="__main__")
