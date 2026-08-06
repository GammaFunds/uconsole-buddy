import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import companion.main as companion_main


def test_importing_companion_main_is_application_import_safe(tmp_path):
    device_path = Path(__file__).resolve().parents[1]
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    code = "\n".join(
        (
            "import asyncio",
            "import json",
            "import pathlib",
            "import sys",
            f"sys.path.insert(0, {str(device_path)!r})",
            "import companion.main",
            "try:",
            "    asyncio.get_running_loop()",
            "    loop = True",
            "except RuntimeError:",
            "    loop = False",
            "print(json.dumps({",
            "    'loop': loop,",
            "    'application_modules': sorted(name for name in sys.modules if name in {",
            "        'companion.ble_nus', 'companion.notify', 'companion.state',",
            "        'companion.ui', 'companion.protocol', 'companion.i18n',",
            "    }),",
            "    'log_exists': pathlib.Path('companion.log').exists(),",
            "}))",
        )
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "loop": False,
        "application_modules": [],
        "log_exists": False,
    }


def test_application_runner_orders_logging_imports_construction_and_asyncio(monkeypatch):
    events = []

    class FakeCompanion:
        def __init__(self):
            events.append("construct")

        def run(self):
            events.append("coroutine")
            return object()

    def configure_logging():
        events.append("logging")

    def load_symbols():
        events.append("imports")
        companion_main.Companion = FakeCompanion

    def run_asyncio(coroutine):
        events.append("asyncio.run")
        assert coroutine is not None
        return None

    monkeypatch.setattr(companion_main, "_configure_application_logging", configure_logging)
    monkeypatch.setattr(companion_main, "_load_application_symbols", load_symbols)
    monkeypatch.setattr(companion_main.asyncio, "run", run_asyncio)

    assert companion_main._run_application() == 0
    assert events == ["logging", "imports", "construct", "coroutine", "asyncio.run"]


def test_entrypoint_validates_lease_before_runner_and_releases_after_runner(monkeypatch):
    events = []

    class RuntimeContext:
        def __enter__(self):
            events.append("runtime_enter")
            return object()

        def __exit__(self, exc_type, exc_value, traceback):
            events.append("runtime_exit")
            return False

    class FakeLock:
        def runtime(self):
            events.append("runtime_context")
            return RuntimeContext()

    def validate(lease):
        events.append("validate")
        assert lease is not None

    def runner():
        events.append("runner")
        assert events == [
            "lock_factory",
            "runtime_context",
            "runtime_enter",
            "validate",
            "runner",
        ]
        return 0

    def factory():
        events.append("lock_factory")
        return FakeLock()

    monkeypatch.setattr(companion_main, "validate_gerald_runtime_lease", validate)

    assert companion_main._run_gerald_entrypoint(
        lock_factory=factory,
        application_runner=runner,
        stderr=StringIO(),
    ) == 0
    assert events == [
        "lock_factory",
        "runtime_context",
        "runtime_enter",
        "validate",
        "runner",
        "runtime_exit",
    ]


def test_entrypoint_maps_busy_context_to_sanitized_retryable_result():
    called = []

    class BusyContext:
        def __enter__(self):
            raise companion_main.GeraldLifecycleBusy("secret path and errno")

        def __exit__(self, exc_type, exc_value, traceback):
            called.append("exit")
            return False

    class BusyLock:
        def runtime(self):
            return BusyContext()

    stderr = StringIO()
    result = companion_main._run_gerald_entrypoint(
        lock_factory=BusyLock,
        application_runner=lambda: called.append("runner"),
        stderr=stderr,
    )

    assert result == 75
    assert stderr.getvalue() == (
        "GERALD_ENTRYPOINT status=BUSY code=lifecycle_busy\n"
    )
    assert called == []


def test_entrypoint_maps_unsafe_and_invalid_witness_to_same_sanitized_result():
    for error in (
        companion_main.GeraldLifecycleUnsafe,
        companion_main.GeraldLifecycleWitnessInvalid,
    ):
        stderr = StringIO()

        class FailingContext:
            def __enter__(self):
                raise error("secret detail")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class FailingLock:
            def runtime(self):
                return FailingContext()

        assert companion_main._run_gerald_entrypoint(
            lock_factory=FailingLock,
            application_runner=lambda: 0,
            stderr=stderr,
        ) == 78
        assert stderr.getvalue() == (
            "GERALD_ENTRYPOINT status=UNSAFE code=lifecycle_unsafe\n"
        )


def test_entrypoint_normalizes_application_failures_interrupts_and_results(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class Lock:
        def runtime(self):
            return Context()

    monkeypatch.setattr(companion_main, "validate_gerald_runtime_lease", lambda lease: None)

    cases = (
        (lambda: (_ for _ in ()).throw(RuntimeError("secret error")),
         1, "GERALD_ENTRYPOINT status=APPLICATION_ERROR code=application_error\n"),
        (lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
         1, "GERALD_ENTRYPOINT status=INTERRUPTED code=keyboard_interrupt\n"),
        (lambda: None, 1,
         "GERALD_ENTRYPOINT status=APPLICATION_ERROR code=application_contract\n"),
        (lambda: True, 1,
         "GERALD_ENTRYPOINT status=APPLICATION_ERROR code=application_contract\n"),
        (lambda: 256, 1,
         "GERALD_ENTRYPOINT status=APPLICATION_ERROR code=application_contract\n"),
        (lambda: 17, 17, ""),
    )
    for runner, expected_code, expected_stderr in cases:
        stderr = StringIO()
        assert companion_main._run_gerald_entrypoint(
            lock_factory=Lock,
            application_runner=runner,
            stderr=stderr,
        ) == expected_code
        assert stderr.getvalue() == expected_stderr
        assert "secret" not in stderr.getvalue()


def test_main_rejects_arguments_before_constructing_or_acquiring(monkeypatch):
    calls = []

    def unexpected_wrapper(**kwargs):
        calls.append(kwargs)
        return 99

    monkeypatch.setattr(companion_main, "_run_gerald_entrypoint", unexpected_wrapper)
    monkeypatch.setattr(companion_main.sys, "stderr", StringIO())

    assert companion_main.main(["--help"]) == 64
    assert companion_main.sys.stderr.getvalue() == (
        "GERALD_ENTRYPOINT status=USAGE_ERROR code=usage\n"
    )
    assert calls == []


def test_main_empty_arguments_uses_the_shared_entrypoint_wrapper(monkeypatch):
    observed = {}

    def wrapper(**kwargs):
        observed.update(kwargs)
        return 23

    monkeypatch.setattr(companion_main, "_run_gerald_entrypoint", wrapper)

    assert companion_main.main([]) == 23
    assert observed["lock_factory"] is companion_main._default_lock_factory
    assert observed["application_runner"] is companion_main._run_application
    assert observed["stderr"] is sys.stderr


def test_real_temporary_lease_covers_runner_and_rejects_duplicate(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    lock_path = parent / "gerald.lifecycle.lock"
    contender = companion_main.GeraldLifecycleLock(os.geteuid(), lock_path)
    observed = []

    def runner():
        observed.append("runner")
        duplicate = companion_main.GeraldLifecycleLock(os.geteuid(), lock_path)
        try:
            duplicate.acquire_runtime()
        except companion_main.GeraldLifecycleBusy:
            observed.append("busy")
        finally:
            duplicate.release()
        return 0

    result = companion_main._run_gerald_entrypoint(
        lock_factory=lambda: contender,
        application_runner=runner,
        stderr=StringIO(),
    )

    assert result == 0
    assert observed == ["runner", "busy"]
    reacquired = companion_main.GeraldLifecycleLock(os.geteuid(), lock_path)
    lease = reacquired.acquire_runtime()
    try:
        companion_main.validate_gerald_runtime_lease(lease)
    finally:
        reacquired.release()
