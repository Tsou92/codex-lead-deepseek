"""Headless CLI compatibility between the modern ``--json`` runner and the
official ``latest`` one-shot runner.

The npm ``latest`` tag can point at an older ``@deepseek-ai/dsh`` whose headless
profile only accepts a positional task, streams reasoning to stderr and prints
the final assistant text to stdout.  This module probes that capability without
assuming a concrete version, then builds the extra patch rows and command
arguments the legacy adapter needs.

The probe is deliberately explicit: an unknown or failing ``--help`` interface
raises :class:`CompatibilityError` instead of silently losing validation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = [
    "CompatibilityError",
    "LEGACY",
    "LEGACY_READY_SERVICE",
    "MODERN",
    "PROBE_TIMEOUT_SECONDS",
    "clear_probe_cache",
    "headless_arguments",
    "legacy_patch",
    "probe_headless_mode",
    "stdin_payload",
]

PROBE_TIMEOUT_SECONDS = 15
MODERN = "modern"
LEGACY = "legacy"
# Service the compat plugin publishes after swapping internals.stdout.  The
# official runner is patched to inject it, so it cannot capture the original
# process stdout before the JSONL sink is installed.
LEGACY_READY_SERVICE = "leadseekLegacyReady"

# One probe per process: the installed runtime cannot change mid-process.
_PROBE_CACHE: dict[tuple, str] = {}


class CompatibilityError(ValueError):
    """The headless CLI capability could not be determined or adapted."""


def clear_probe_cache() -> None:
    """Drop the in-process probe cache (used by tests)."""
    _PROBE_CACHE.clear()


def probe_headless_mode(command, cwd, environment, run=subprocess.run,
                        timeout=PROBE_TIMEOUT_SECONDS):
    """Return :data:`MODERN` when ``--profile headless --help`` offers ``--json``.

    Runs the installed CLI's headless help only (no model, no task) and inspects
    stdout and stderr.  A missing, failing or empty help interface raises
    :class:`CompatibilityError` so an unknown runtime is never misclassified.
    """
    key = tuple(command)
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    probe = list(command) + ["--profile", "headless", "--help"]
    try:
        completed = run(probe, cwd=cwd, env=environment, capture_output=True,
                        text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CompatibilityError(
            "Harness headless --help 在 %d 秒内没有响应，无法判定 --json 能力" % timeout
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise CompatibilityError(
            "无法运行 Harness headless --help: " + str(error)) from error
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if completed.returncode != 0:
        raise CompatibilityError(
            "Harness headless --help 退出码 %d，无法判定 --json 能力"
            % completed.returncode)
    if not output.strip():
        raise CompatibilityError(
            "Harness headless --help 没有任何输出，无法判定 --json 能力")
    mode = MODERN if "--json" in output else LEGACY
    _PROBE_CACHE[key] = mode
    return mode


def headless_arguments(mode, patch_path):
    """Profile arguments for *mode*; the prompt is never part of the argv."""
    arguments = ["--profile", "headless", "--patch", str(patch_path)]
    if mode == MODERN:
        arguments.append("--json")
    return arguments


def stdin_payload(mode, prompt):
    """Modern profile reads the task from stdin; the legacy profile gets none."""
    return "" if mode == LEGACY else prompt


def legacy_patch(root, prompt, directory):
    """Patch rows adapting the official legacy profile to the JSONL bridge.

    ``headless-startup`` is disabled and ``headless-runner`` replaces its
    ``headlessStartup`` injection with :data:`LEGACY_READY_SERVICE`, while the
    task text is passed through the patch config (a restricted local
    ``patch.json``) instead of argv.  The required compat plugin is inserted so
    the runner's public ``internals.stdout`` object is swapped to a JSONL sink
    before the runner mounts (it waits on the ready service).
    """
    root = Path(root)
    return [
        {"id": "headless-startup", "disabled": True},
        {"id": "headless-runner", "inject": [LEGACY_READY_SERVICE],
         "config": {"task": prompt}},
        {"insert": [{
            "id": "leadseek-legacy-headless",
            "name": str(root / "plugins/legacy-headless.mjs"),
            "required": True,
            "config": {"directory": str(directory)},
        }]},
    ]
