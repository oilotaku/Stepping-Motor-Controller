"""PostToolUse hook：Python 檔案被編輯後立即做語法檢查。

stdin 收到 Claude Code 的 hook JSON，取出被改動的檔案路徑；
只處理 .py，語法錯誤時以 decision=block 回報，讓錯誤當場浮現而不是
等到下次執行才炸開。

輸出契約（stdout 印 JSON）：
  正常  → 不輸出，exit 0
  語法錯 → {"decision": "block", "reason": ..., "systemMessage": ...}
"""

import ast
import json
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 不是預期的輸入就安靜略過，別擋住正常流程

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    raw = (
        tool_input.get("file_path")
        or tool_response.get("filePath")
        or ""
    )
    if not raw:
        return 0

    path = Path(raw)
    if path.suffix.lower() != ".py" or not path.is_file():
        return 0

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # 讀不到就別擋，但讓使用者知道
        json.dump({"systemMessage": f"skip {path.name}: {exc}"}, sys.stdout)
        return 0

    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        detail = f"{path.name}:{exc.lineno} {exc.msg}"
        if exc.text:
            detail += f"\n    {exc.text.rstrip()}"
        # 一律用 ensure_ascii=True（預設）：這台機器的 stdout 是 cp950，
        # 直接輸出中文會 UnicodeEncodeError，emoji 更是必炸。
        # JSON 的 \uXXXX escape 是純 ASCII，任何編碼都能安全傳遞，
        # 消費端解析後仍還原成中文。
        json.dump(
            {
                "decision": "block",
                "reason": (
                    f"Python 語法錯誤，這次編輯讓 {path.name} 無法解析：\n"
                    f"{detail}\n請立即修正。"
                ),
                "systemMessage": f"語法錯誤 {detail.splitlines()[0]}",
            },
            sys.stdout,
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
