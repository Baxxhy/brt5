#!/usr/bin/env python3
"""Apply environment-only compatibility fixes, then run official SWTBench.

The official checkout, test commands, parsers, and F2P grading remain unchanged.
Only repository/image provisioning commands are made reproducible and retryable.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import runpy
import hashlib
import subprocess
import sys
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.constants import MAP_VERSION_TO_INSTALL
from src.exec_spec import ExecSpec


APT_OPTIONS = (
    "-o Acquire::Retries=8 -o Acquire::http::Timeout=120 "
    "-o Acquire::https::Timeout=120"
)


def install_retryable_source_fetches() -> None:
    """Serve immutable raw GitHub files locally, with retried HTTP fallback."""
    original_get = requests.get
    retry = Retry(
        total=10,
        connect=10,
        read=10,
        status=10,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    repo_root = Path(
        os.environ.get(
            "BRT_SWE_REPOS", "/root/Baxxhy/BugReproduce/swe_repos"
        )
    )

    def local_or_retry_get(url: str, *args, **kwargs):
        parsed = urlparse(url)
        parts = parsed.path.lstrip("/").split("/", 3)
        if parsed.netloc == "raw.githubusercontent.com" and len(parts) == 4:
            owner, repo, commit, relative = parts
            candidates = (repo_root / repo, repo_root / f"{owner}__{repo}")
            for local_repo in candidates:
                if not (local_repo / ".git").exists():
                    continue
                result = subprocess.run(
                    ["git", "-C", str(local_repo), "show", f"{commit}:{relative}"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                if result.returncode == 0:
                    response = requests.Response()
                    response.status_code = 200
                    response.url = url
                    response._content = result.stdout.encode("utf-8")
                    response.encoding = "utf-8"
                    return response
        kwargs.setdefault("timeout", (30, 120))
        return session.get(url, *args, **kwargs)

    # The official utilities imported the requests module, so replacing the
    # module-level convenience function covers both direct and recursive files.
    if requests.get is original_get:
        requests.get = local_or_retry_get


def _append_once(spec: dict, key: str, command: str) -> None:
    values = spec.setdefault(key, [])
    if command not in values:
        values.append(command)


def apply_environment_compatibility() -> None:
    # Shallow instance-image checkouts do not contain the tags setuptools_scm
    # needs to infer pytest's release.  Its fallback is 0.1.dev1, which makes
    # pytest reject its own tox.ini/pyproject ``minversion`` before collecting
    # either generated or golden tests.  Pin the dataset release explicitly.
    # This changes packaging metadata only; source and test semantics are the
    # same checkout.  Keep the command in eval_commands because buggy-only
    # feedback intentionally skips the otherwise repeated editable install.
    for version, spec in MAP_VERSION_TO_INSTALL["pytest-dev/pytest"].items():
        release = version if version.count(".") >= 2 else f"{version}.0"
        install = (
            f"SETUPTOOLS_SCM_PRETEND_VERSION={release} "
            "python -m pip install -e ."
        )
        spec["install"] = install
        _append_once(spec, "eval_commands", install)

    # pip 25+ removed --no-use-pep517. Scikit-learn 1.3 supports PEP 517.
    for version in ("1.3", "1.4"):
        spec = MAP_VERSION_TO_INSTALL["scikit-learn/scikit-learn"][version]
        spec["install"] = spec["install"].replace(" --no-use-pep517", "")

    # Pylint 2.15's isolated editable backend is too old for modern pip.
    pylint = MAP_VERSION_TO_INSTALL["pylint-dev/pylint"]["2.15"]
    _append_once(
        pylint,
        "pre_install",
        "python -m pip install --retries 10 --timeout 120 'pip<25' 'setuptools<70' wheel",
    )
    pylint["install"] = "python -m pip install --retries 10 --timeout 120 --no-build-isolation -e ."

    # The derived env images already contain all apt dependencies. Build against
    # system FreeType/Qhull and keep incremental artifacts across SWT phases.
    matplotlib_config = (
        "config_template=setup.cfg.template; "
        "if [ -f mplsetup.cfg.template ]; then config_template=mplsetup.cfg.template; fi; "
        "test -f \"$config_template\" && cp \"$config_template\" setup.cfg && "
        "sed -i 's/#system_freetype = False/system_freetype = True/; "
        "s/#system_qhull = False/system_qhull = True/' setup.cfg"
    )
    for version in ("3.3", "3.5", "3.6", "3.7"):
        spec = MAP_VERSION_TO_INSTALL["matplotlib/matplotlib"][version]
        spec["pre_install"] = [matplotlib_config]
        spec["install"] = (
            "python -m pip install --retries 10 --timeout 120 -v --no-build-isolation -e ."
        )

    # Old Sphinx tox environments can otherwise select setuptools 81+, which
    # removed APIs still used by these releases.
    sphinx_setuptools = (
        "python -c \"from pathlib import Path; p=Path('tox.ini'); s=p.read_text(); "
        "p.write_text(s if '\\n    setuptools<81\\n' in s else "
        "s.replace('deps =', 'deps =\\n    setuptools<81', 1))\""
    )
    for spec in MAP_VERSION_TO_INSTALL["sphinx-doc/sphinx"].values():
        _append_once(spec, "pre_install", sphinx_setuptools)
        # docutils no longer vendors ``docutils.utils.roman`` while these
        # Sphinx revisions retain a fallback import of the external package.
        # The missing dependency invalidated the official golden canary too.
        _append_once(
            spec,
            "eval_commands",
            "python -c 'import roman' 2>/dev/null || "
            "python -m pip install --retries 10 --timeout 120 'roman==3.3'",
        )

    # Astropy 4/5 needs a modern setuptools while creating the PEP 660 editable
    # install, but its runtime still imports setuptools.dep_util. Build with 68,
    # then leave 59.8 installed for the tests. The command is also run again by
    # the official eval script, so the ordering must be self-contained.
    for version in ("4.3", "5.1", "5.2"):
        spec = MAP_VERSION_TO_INSTALL["astropy/astropy"][version]
        pin_build_setuptools = (
            r'''sed -i 's/requires = \["setuptools",/requires = ["setuptools==68.0.0",/' pyproject.toml'''
        )
        spec["pre_install"] = [pin_build_setuptools]
        spec["install"] = (
            f"{pin_build_setuptools} && "
            "python -m pip install --retries 10 --timeout 120 "
            "-e .[test] --verbose && "
            "python -m pip install --retries 10 --timeout 120 'setuptools==59.8.0'"
        )


def install_retryable_repo_setup() -> None:
    original = ExecSpec.repo_script_list.fget
    original_env_image_key = ExecSpec.env_image_key.fget
    original_test_command = ExecSpec.test_command.fget

    def retryable(self: ExecSpec) -> list[str]:
        commands = original(self)
        clone = (
            "cd / && git config --global http.version HTTP/1.1 && "
            f"rm -rf {self.repo_directory} && mkdir -p {self.repo_directory} && "
            f"cd {self.repo_directory} && git init && "
            f"git remote add origin https://github.com/{self.repo} && "
            "for attempt in 1 2 3 4 5; do "
            f"timeout --kill-after=15s 300s git fetch --depth=1 --filter=blob:none "
            f"origin {self.base_commit} && break; "
            "test \"$attempt\" = 5 && exit 1; sleep $((attempt * 5)); done && "
            f"git checkout --detach {self.base_commit} && cd /"
        )
        commands[0] = clone
        commands.insert(
            1,
            "export PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PIP_DISABLE_PIP_VERSION_CHECK=1",
        )
        commands.insert(2, "echo brt6-environment-compat-v3")
        return commands

    ExecSpec.repo_script_list = property(retryable)

    def compatible_env_image_key(self: ExecSpec) -> str:
        key = original_env_image_key(self)
        if self.repo == "matplotlib/matplotlib" and self.version in {"3.3", "3.5", "3.6", "3.7"}:
            return f"brt6.compat.{key}"
        return key

    ExecSpec.env_image_key = property(compatible_env_image_key)

    def compatible_test_command(self: ExecSpec) -> str:
        """Undo an upstream ``str.strip`` truncation for SymPy directives.

        The pinned official harness strips the PYTHONWARNINGS prefix with
        ``str.strip(chars)``.  Because ``y`` is one of those characters, every
        trailing ``.py`` test directive becomes ``.p`` when coverage is on.
        Restore only exact directives already computed by the official harness;
        test selection and grading otherwise remain unchanged.
        """
        command = original_test_command(self)
        if self.repo == "astropy/astropy" and self.version == "1.3":
            # Their pinned pytest 3.3 predates --no-header.  The flag affects
            # display only and previously prevented generated and golden tests
            # from collecting at all.
            command = re.sub(r"(?<!\S)--no-header(?!\S)\s*", "", command)
        if self.repo == "sympy/sympy" and self.compute_coverage:
            for directive in self.test_directives or []:
                if not directive.endswith(".py"):
                    continue
                truncated = re.escape(directive[:-1])
                command = re.sub(
                    rf"(?<!\S){truncated}(?!\S)", directive, command
                )
        return command

    ExecSpec.test_command = property(compatible_test_command)


def install_quiet_eval_diagnostics(*, skip_reinstall: bool = False) -> None:
    """Remove high-volume diagnostics without changing test/grading semantics.

    ``git show`` can emit tens of megabytes for a single merge commit and is
    purely informational.  Buggy-only feedback also applies no source patch,
    so its instance image's existing editable install is already current.
    Formal six-phase evaluation keeps the official reinstall step.
    """
    original = ExecSpec.eval_script_list.fget

    def efficient(self: ExecSpec) -> list[str]:
        commands = original(self)
        diagnostics = {"git status", "git show", f"git diff {self.base_commit}"}
        commands = [command for command in commands if command not in diagnostics]
        if skip_reinstall:
            install_command = self.install.get("install")
            if install_command:
                commands = [command for command in commands if command != install_command]
        return commands

    ExecSpec.eval_script_list = property(efficient)

    # The official harness compares the repository diff before and after each
    # test phase, but also logs the complete diff twice.  Keep the comparison
    # value byte-for-byte identical while logging only a compact fingerprint.
    # Neither the returned diff nor this diagnostic log participates in SWT
    # grading, F2P, or patch coverage.
    for module_name in ("run_evaluation", "src.run_evaluation"):
        run_evaluation = sys.modules.get(module_name)
        if run_evaluation is None:
            continue

        def compact_log_git_diff(logger, container, annotation):
            output = (
                container.exec_run("git diff", workdir="/testbed")
                .output.decode("utf-8", errors="replace").strip()
            )
            digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
            logger.info(f"{annotation} bytes={len(output)} sha256={digest}")
            return output

        run_evaluation.log_git_diff = compact_log_git_diff


def install_tolerant_docker_output() -> None:
    """Preserve official parsing while replacing invalid bytes in Docker logs."""
    # Depending on the entry point, the same official file may be imported as
    # either (or both) module names. Patch every loaded copy; otherwise the
    # feedback runner can retain the strict UTF-8 decoder in the other copy.
    modules = []
    for name in ("run_evaluation", "src.run_evaluation"):
        module = sys.modules.get(name)
        if module is not None and module not in modules:
            modules.append(module)
    if not modules:
        raise RuntimeError("official run_evaluation module is not loaded")

    class DockerOutput(bytes):
        def decode(self, encoding="utf-8", errors="strict"):
            if encoding.lower().replace("_", "-") in {"utf-8", "utf8"} and errors == "strict":
                errors = "replace"
            return super().decode(encoding, errors)

    for run_evaluation in modules:
        original = run_evaluation.exec_run_with_timeout

        def tolerant(*args, _original=original, **kwargs):
            return DockerOutput(_original(*args, **kwargs))

        run_evaluation.exec_run_with_timeout = tolerant

        def eval_in_container(
            log_dir,
            container,
            logger,
            eval_script,
            timeout,
            instance_id,
            compute_coverage,
            build_mode,
            _module=run_evaluation,
            _original=original,
        ):
            # Equivalent to the official function except for errors="replace".
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            eval_file = log_dir / "eval.sh"
            eval_file.write_text(eval_script)
            logger.info(
                f"Eval script for {instance_id} written to eval.sh, now applying to container..."
            )
            _module.copy_to_container(
                container, eval_file, Path("/eval.sh"), build_mode=build_mode
            )
            if compute_coverage:
                trace_file = Path(_module.__file__).parent / "auxillary_src" / "trace.py"
                _module.copy_to_container(
                    container, trace_file, Path("/root/trace.py"), build_mode=build_mode
                )
            result = _original(container, "/bin/bash /eval.sh", timeout=timeout)
            test_output = result.decode("utf-8", errors="replace")
            test_output_path = log_dir / "test_output.txt"
            test_output_path.write_text(test_output)
            logger.info(f"Test output for {instance_id} written to {test_output_path}")
            return test_output_path

        run_evaluation.eval_in_container = eval_in_container


if __name__ == "__main__":
    install_retryable_source_fetches()
    apply_environment_compatibility()
    install_retryable_repo_setup()
    install_quiet_eval_diagnostics(skip_reinstall=False)
    install_tolerant_docker_output()
    # src.__init__ imports src.main while loading the official package. Execute
    # that exact official file as the CLI to avoid importing the module twice.
    runpy.run_path(sys.modules["src.main"].__file__, run_name="__main__")
