"""Базовый класс management-команд: всё, что команда пишет в stdout/stderr, дублируется в logs/commands.log.

Так отчёты импорта, обучения и бэктеста сохраняются сами, без ручного перенаправления вывода
(`> train_log.txt`). Файл создаётся при первом запуске и ротируется по размеру (см. LOGGING в settings).
Чувствительные данные маскируются тем же фильтром, что и в остальных логах.
"""
from __future__ import annotations

import logging
import re
import time

from django.core.management.base import BaseCommand

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class LoggedCommand(BaseCommand):
    def execute(self, *args, **options):
        name = self.__class__.__module__.rsplit(".", 1)[-1]
        log = logging.getLogger(f"commands.{name}")
        for stream, level in ((self.stdout, logging.INFO), (self.stderr, logging.ERROR)):
            original = stream.write

            def write(msg="", style_func=None, ending=None, _original=original, _level=level):
                text = _ANSI_RE.sub("", str(msg)).strip()
                if text:
                    log.log(_level, text)
                return _original(msg, style_func, ending)

            stream.write = write
        shown = {k: v for k, v in options.items() if k not in ("stdout", "stderr", "settings", "pythonpath", "traceback", "no_color", "force_color", "skip_checks", "verbosity") and v not in (None, False, "")}
        log.info("start %s %s", name, shown)
        started = time.monotonic()
        try:
            result = super().execute(*args, **options)
        except BaseException as exc:
            log.error("failed %s after %.0fs: %s", name, time.monotonic() - started, exc)
            raise
        log.info("finished %s in %.0fs", name, time.monotonic() - started)
        return result
