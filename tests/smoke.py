# -*- coding: utf-8 -*-
"""冒烟测试：依次跑每个阶段的 --mock 演示，验证全课程无 API Key 可运行。

用法：python tests/smoke.py
每个条目：(阶段目录, 参数列表, 允许的退出码集合)。
lint 允许 exit 1——workspace 有 error 级 issue 时它就该返回 1（CI 语义）。
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEKNORA_ENGINE = r"C:\Desktop\Project\WeKnora\internal\agent\engine.go"

CASES = [
    ("s01_llm_as_function", [WEKNORA_ENGINE, "--mock"], {0}),
    ("s02_doc_to_page", ["scan"], {0}),
    ("s03_map_reduce", ["status"], {0}),
    ("s05_incremental_update", ["status"], {0}),
    ("s06_wiki_lint", ["lint"], {0, 1}),
    ("s07_agent_loop", ["--mock-loop", "--max-iterations", "2"], {0}),
    ("s08_file_tools_permissions", ["--mock"], {0}),
    ("s09_wiki_qa_agent", ["query", "Agent 引擎的循环是怎么工作的？", "--mock"], {0}),
    ("s10_todo_subagent",
     ["query", "上传文档到向量入库的链路？", "--mock"], {0}),
    ("s11_context_engineering",
     ["query", "迭代控制与收尾机制？", "--mock", "--compress-threshold", "3000"], {0}),
    ("s12_fixer_and_complete", ["status"], {0}),
]


def main():
    failed = []
    for stage, args, allowed in CASES:
        script = ROOT / stage / "code.py"
        start = time.time()
        result = subprocess.run([sys.executable, str(script)] + args,
                                cwd=script.parent, capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=300)
        elapsed = time.time() - start
        ok = result.returncode in allowed
        print(f"{'PASS' if ok else 'FAIL'}  {stage:<28} "
              f"exit={result.returncode} ({elapsed:.1f}s)")
        if not ok:
            failed.append(stage)
            tail = (result.stdout + result.stderr).strip().splitlines()[-8:]
            for line in tail:
                print(f"      {line}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
