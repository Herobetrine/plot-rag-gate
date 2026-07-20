from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SERVER = PLUGIN_ROOT / "scripts" / "plot_rag_mcp.py"


class McpProcessLifecycleTests(unittest.TestCase):
    def test_server_exits_cleanly_after_stdin_eof(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        for request_id in range(1, 4):
            with self.subTest(request_id=request_id):
                with subprocess.Popen(
                    [sys.executable, "-B", "-X", "utf8", str(SERVER)],
                    cwd=PLUGIN_ROOT,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                ) as process:
                    assert process.stdin is not None
                    assert process.stdout is not None
                    assert process.stderr is not None
                    process.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "ping",
                            }
                        )
                        + "\n"
                    )
                    process.stdin.flush()

                    response = json.loads(process.stdout.readline())
                    self.assertEqual(request_id, response["id"])
                    self.assertEqual({}, response["result"])

                    process.stdin.close()
                    process.wait(timeout=5)
                    stderr = process.stderr.read()

                self.assertEqual(0, process.returncode, stderr)


if __name__ == "__main__":
    unittest.main()
