#!/usr/bin/env bash
#
# test.sh - 手动运行本次 graceful shutdown 改动的全部单元测试。
#
# 用法:
#   ./test.sh            # 运行全部相关测试(新增 + 相关回归)
#   ./test.sh new        # 仅运行新增的 graceful shutdown 测试
#   ./test.sh regression # 仅运行相关现有回归测试
#   ./test.sh all        # 运行整个 tests/ 测试套件
#
# 环境:
#   默认使用项目根目录 .venv 中的解释器, 可通过 PYTHON 环境变量覆盖:
#   PYTHON=/path/to/python ./test.sh

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="python3"
fi

echo "==> 使用解释器: $PYTHON"
"$PYTHON" --version

NEW_TESTS="tests/test_graceful_shutdown.py"
REGRESSION_TESTS="tests/test_app.py tests/test_asgi.py tests/test_background_tasks.py tests/test_basic.py tests/test_testing.py"

run() {
    echo ""
    echo "==> 运行: $1"
    # shellcheck disable=SC2086
    "$PYTHON" -m pytest $1 -v --tb=short
}

case "${1:-}" in
    new)
        run "$NEW_TESTS"
        ;;
    regression)
        run "$REGRESSION_TESTS"
        ;;
    all)
        run "tests/"
        ;;
    "")
        run "$NEW_TESTS"
        run "$REGRESSION_TESTS"
        ;;
    *)
        echo "未知参数: $1 (可选: new | regression | all)" >&2
        exit 2
        ;;
esac

echo ""
echo "==> 测试完成"
