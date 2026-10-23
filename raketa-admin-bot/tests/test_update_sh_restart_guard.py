"""Обёртка deploy/update.sh: после каких ключей служба перезапускается.

Диагностические ключи сами службу не перезапускают («только проверка»). Но если
вместе с ними пришёл ключ-выключатель, служба обязана перезапуститься — иначе
новое значение в switches.env не дойдёт до работающего процесса.

Проверяем сам кусок обёртки: вырезаем условие «только проверка» и выполняем его
в bash с теми же `set -euo pipefail`, что и у обёртки.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

UPDATE_SH = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"


def _guard_block() -> str:
    text = UPDATE_SH.read_text(encoding="utf-8")
    start = text.index("# Диагностика ничего не меняет")
    end = text.index('say "6. Перезапуск"', start)
    return text[start:end]


def _restarts(**flags: str) -> bool:
    block = _guard_block()
    # Обёртка заводит все переменные ключей пустыми — так же и здесь, под set -u.
    names = sorted(set(re.findall(r"\$([A-Z_]+)", block)))
    lines = ["set -euo pipefail"]
    lines += [f'{name}=""' for name in names]
    lines += [f'{name}="{value}"' for name, value in flags.items()]
    lines += [block, "echo RESTART"]
    result = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True,
                            text=True, timeout=10, check=True)
    return "RESTART" in result.stdout


def test_diagnostics_alone_does_not_restart():
    assert _restarts(SHOW_LEADS="1") is False


def test_autocall_switch_with_diagnostics_restarts():
    assert _restarts(SHOW_LEADS="1", AUTOCALL="1") is True


def test_autocall_button_switch_with_diagnostics_restarts():
    """--autocall-button-on вместе с диагностикой — служба перезапускается."""
    assert _restarts(SHOW_LEADS="1", AUTOCALL_BUTTON="1") is True
    assert _restarts(SHOW_LEADS="1", AUTOCALL_BUTTON="0") is True


def test_autocall_recheck_switch_with_diagnostics_restarts():
    """--autocall-recheck-on вместе с диагностикой — служба перезапускается."""
    assert _restarts(SHOW_LEADS="1", AUTOCALL_RECHECK="1") is True
    assert _restarts(SHOW_LEADS="1", AUTOCALL_RECHECK="0") is True
