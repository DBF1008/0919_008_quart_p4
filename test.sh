#!/usr/bin/env bash
# 手动运行单元测试（离线环境）。
#
# 用法:
#   ./test.sh                 运行当前离线环境可通过的单元测试
#   ./test.sh --full          运行 tests/ 下全部测试（含因离线 stub 限制
#                             而无法通过的用例，仅用于排查）
#   ./test.sh <pytest 参数>   透传给 pytest，例如:
#                             ./test.sh tests/test_graceful_shutdown.py -v
#
# 环境说明:
#   本仓库处于离线环境，第三方依赖（flask/werkzeug/hypercorn/pygments/
#   blinker）以编译好的 .pyc 形式存放在 offline_check/stubs/ 中。
#   本脚本会先将其平铺为可无源码导入的形式（写入临时目录，不污染仓库），
#   再设置 PYTHONPATH 后调用 .venv_local 中的 pytest。
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv_local/bin/python}"
STUB_LIB="${STUB_LIB:-${TMPDIR:-/tmp}/quart_offline_stub_lib}"

# 1. 准备离线依赖 stub：把 __pycache__/*.cpython-*.pyc 平铺为 <name>.pyc
if [ ! -e "$STUB_LIB/.ready" ]; then
    mkdir -p "$STUB_LIB"
    cp -R offline_check/stubs/* "$STUB_LIB"/
    (
        cd "$STUB_LIB"
        find . -name '*.cpython-*.pyc' | while read -r f; do
            dir=$(dirname "$(dirname "$f")")
            base=$(basename "$f")
            base=${base%.cpython-*.pyc}
            mkdir -p "$dir"
            cp "$f" "$dir/$base.pyc"
        done
    )
    touch "$STUB_LIB/.ready"
fi

export PYTHONPATH="src:$STUB_LIB"

# pyproject.toml 的 addopts 依赖未安装的 pytest-cov，这里覆盖掉
PYTEST_BASE=(-o "addopts=" -p no:cacheprovider)

if [ "$#" -gt 0 ] && [ "$1" != "--full" ]; then
    # 透传自定义参数
    exec "$PYTHON" -m pytest "${PYTEST_BASE[@]}" "$@"
fi

if [ "${1:-}" = "--full" ]; then
    # 完整套件。注意：以下文件因离线 stub（werkzeug/hypercorn 等）
    # 功能不全或缺少 hypothesis 而无法通过，与本次改动无关：
    #   tests/test_app.py tests/test_asgi.py tests/test_testing.py
    #   tests/test_basic.py tests/test_blueprints.py tests/test_cli.py
    #   tests/test_exceptions.py tests/test_formparser.py（挂起）
    #   tests/test_helpers.py tests/test_sessions.py
    #   tests/test_static_hosting.py tests/test_templating.py
    #   tests/test_utils.py tests/test_views.py
    #   tests/wrappers/（全部）
    exec "$PYTHON" -m pytest "${PYTEST_BASE[@]}" tests/
fi

# 默认：运行离线环境下可通过的全部单元测试
exec "$PYTHON" -m pytest "${PYTEST_BASE[@]}" -v \
    tests/test_graceful_shutdown.py \
    tests/test_background_tasks.py \
    tests/test_ctx.py \
    tests/test_debug.py \
    tests/test_routing.py \
    tests/test_sync.py
