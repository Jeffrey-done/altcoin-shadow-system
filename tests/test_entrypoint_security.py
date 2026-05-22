from pathlib import Path


def test_permission_hardening_not_nested_under_skip_compile_check():
    p = Path(__file__).resolve().parents[1] / 'docker-entrypoint.sh'
    src = p.read_text(encoding='utf-8')

    assert 'SKIP_PY_COMPILE_CHECK' in src
    assert 'python3 -m compileall -q /app' in src
    assert 'chmod 600 "$sf"' in src

    # 权限加固块应在 compileall 条件块之外，避免 SKIP=1 时被跳过
    compile_if = src.find('if [ "${SKIP_PY_COMPILE_CHECK:-0}" != "1" ]; then')
    compile_fi = src.find('\nfi\n', compile_if)
    chmod_block = src.find('for sf in /app/.env')

    assert compile_if != -1 and compile_fi != -1 and chmod_block != -1
    assert chmod_block > compile_fi
