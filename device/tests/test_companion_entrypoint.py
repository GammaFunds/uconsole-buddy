import json
import os
import subprocess
import sys
import asyncio
from io import StringIO
from pathlib import Path

import pytest
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
    original_asyncio_run = asyncio.run

    class FakeCompanion:
        def __init__(self):
            events.append("construct")

        async def run(self):
            events.append("coroutine")
            return None

        async def aclose(self):
            events.append("close")

    def configure_logging():
        events.append("logging")

    def load_symbols():
        events.append("imports")
        monkeypatch.setattr(companion_main, "Companion", FakeCompanion)

    def run_asyncio(coroutine):
        assert coroutine is not None
        result = original_asyncio_run(coroutine)
        events.append("asyncio.run")
        return result

    monkeypatch.setattr(companion_main, "_configure_application_logging", configure_logging)
    monkeypatch.setattr(companion_main, "_load_application_symbols", load_symbols)
    monkeypatch.setattr(companion_main.asyncio, "run", run_asyncio)

    assert companion_main._run_application() == 0
    assert events == [
        "logging", "imports", "construct", "coroutine", "close", "asyncio.run"
    ]


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


class CompanionFakeTask:
    def __init__(self, events, *, error=None, cancelled=False):
        self.events = events
        self.error = error
        self._cancelled = cancelled
        self._done = error is not None or cancelled

    def done(self):
        return self._done

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self.events.append("task.cancel")
        self._cancelled = True
        self._done = True
        return True

    def __await__(self):
        async def observe():
            self.events.append("task.await")
            if self._cancelled:
                raise asyncio.CancelledError
            if self.error is not None:
                raise self.error

        return observe().__await__()


class FakeBLE:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error
        self.calls = 0

    async def aclose(self):
        self.calls += 1
        self.events.append("ble.close")
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error


class CompanionTaskCleanupError(Exception):
    pass


class BleCleanupError(Exception):
    pass


def test_companion_aclose_aggregates_task_then_ble_failure_and_remains_exact_once(
    monkeypatch,
):
    events = []
    task_failure = CompanionTaskCleanupError("companion task cleanup failure")
    ble_failure = BleCleanupError("ble cleanup failure")
    ble = FakeBLE(events, ble_failure)
    companion = bare_companion(ble, events)
    task = CompanionFakeTask(events, error=task_failure)

    async def foreign_work():
        await asyncio.sleep(60)

    with monkeypatch.context() as patch:
        patch.setattr(
            companion_main.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        companion._create_owned_task(asyncio.sleep(0))

    async def exercise():
        foreign = asyncio.create_task(foreign_work())
        with pytest.raises(ExceptionGroup) as raised:
            await companion.aclose()
        assert foreign.cancelled() is False
        foreign.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreign

        with pytest.raises(ExceptionGroup) as repeated:
            await companion.aclose()
        return raised.value, repeated.value

    first_error, repeated_error = asyncio.run(exercise())
    assert first_error.exceptions == (task_failure, ble_failure)
    assert type(first_error.exceptions[0]) is CompanionTaskCleanupError
    assert str(first_error.exceptions[0]) == "companion task cleanup failure"
    assert type(first_error.exceptions[1]) is BleCleanupError
    assert str(first_error.exceptions[1]) == "ble cleanup failure"
    assert repeated_error.exceptions == (task_failure, ble_failure)
    assert events == ["task.await", "ble.close"]
    assert ble.calls == 1
    assert companion._closed is False


def bare_companion(ble, events):
    companion = object.__new__(companion_main.Companion)
    companion.ble = ble
    companion._owned_tasks = []
    companion._closing = False
    companion._close_lock = asyncio.Lock()
    companion._close_attempted = False
    companion._close_error = None
    companion._closed = False
    return companion


def test_companion_owned_task_cleanup_precedes_ble_close(monkeypatch):
    events = []
    companion = bare_companion(FakeBLE(events), events)
    task = CompanionFakeTask(events)

    with monkeypatch.context() as patch:
        patch.setattr(
            companion_main.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        companion._create_owned_task(asyncio.sleep(0))

    asyncio.run(companion.aclose())

    assert events == ["task.cancel", "task.await", "ble.close"]
    assert companion._owned_tasks == []
    assert companion._closed is True


def test_companion_completed_task_failure_still_delegates_ble_cleanup(monkeypatch):
    events = []
    task_failure = RuntimeError("companion task failure")
    companion = bare_companion(FakeBLE(events), events)
    task = CompanionFakeTask(events, error=task_failure)

    with monkeypatch.context() as patch:
        patch.setattr(
            companion_main.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        companion._create_owned_task(asyncio.sleep(0))

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(companion.aclose())

    assert raised.value.exceptions == (task_failure,)
    assert events == ["task.await", "ble.close"]
    assert companion._closed is False


def test_companion_task_failures_follow_registration_order(monkeypatch):
    events = []
    first_failure = RuntimeError("first task failure")
    second_failure = RuntimeError("second task failure")
    companion = bare_companion(FakeBLE(events), events)
    tasks = [
        CompanionFakeTask(events, error=first_failure),
        CompanionFakeTask(events, error=second_failure),
    ]

    with monkeypatch.context() as patch:
        patch.setattr(
            companion_main.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), tasks.pop(0))[1],
        )
        companion._create_owned_task(asyncio.sleep(0))
        companion._create_owned_task(asyncio.sleep(0))

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(companion.aclose())

    assert raised.value.exceptions == (first_failure, second_failure)
    assert events == ["task.await", "task.await", "ble.close"]


def test_companion_completed_and_already_cancelled_tasks_are_observed_safely():
    events = []
    companion = bare_companion(FakeBLE(events), events)
    completed = CompanionFakeTask(events)
    completed._done = True
    companion._owned_tasks = [completed, CompanionFakeTask(events, cancelled=True)]

    asyncio.run(companion.aclose())

    assert events == ["task.await", "task.await", "ble.close"]
    assert companion._closed is True


def test_companion_foreign_task_is_not_cancelled_or_awaited():
    events = []
    companion = bare_companion(FakeBLE(events), events)

    async def wait_forever():
        await asyncio.sleep(60)

    async def exercise():
        foreign = asyncio.create_task(wait_forever())
        await companion.aclose()
        assert foreign.cancelled() is False
        foreign.cancel()
        try:
            await foreign
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert events == ["ble.close"]


def test_companion_ble_close_is_exactly_once_even_when_it_fails():
    events = []
    ble_failure = RuntimeError("ble close failure")
    ble = FakeBLE(events, ble_failure)
    companion = bare_companion(ble, events)

    for _ in range(2):
        with pytest.raises(ExceptionGroup) as raised:
            asyncio.run(companion.aclose())
        assert raised.value.exceptions == (ble_failure,)

    assert ble.calls == 1
    assert events == ["ble.close"]
    assert companion._closed is False


def test_companion_successful_ble_close_is_exactly_once():
    events = []
    ble = FakeBLE(events)
    companion = bare_companion(ble, events)

    asyncio.run(companion.aclose())
    asyncio.run(companion.aclose())

    assert ble.calls == 1
    assert events == ["ble.close"]
    assert companion._closed is True


def test_companion_concurrent_close_calls_delegate_ble_once():
    events = []
    companion = bare_companion(FakeBLE(events), events)

    async def exercise():
        await asyncio.gather(companion.aclose(), companion.aclose())

    asyncio.run(exercise())

    assert companion.ble.calls == 1
    assert events == ["ble.close"]


def test_run_application_preserves_run_failure_after_successful_cleanup(monkeypatch):
    events = []
    run_failure = RuntimeError("run failure")

    class FakeCompanion:
        def __init__(self):
            self.ble = None

        async def run(self):
            events.append("run")
            raise run_failure

        async def aclose(self):
            events.append("close")

    monkeypatch.setattr(companion_main, "Companion", FakeCompanion)
    monkeypatch.setattr(companion_main, "_configure_application_logging", lambda: None)
    monkeypatch.setattr(companion_main, "_load_application_symbols", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        companion_main._run_application()

    assert raised.value is run_failure
    assert events == ["run", "close"]


def test_run_application_closes_after_ble_start_failure(monkeypatch):
    events = []
    start_failure = RuntimeError("BLE start failure")

    class FakeCompanion:
        def __init__(self):
            pass

        async def run(self):
            events.append("run")
            raise start_failure

        async def aclose(self):
            events.append("close")

    monkeypatch.setattr(companion_main, "Companion", FakeCompanion)
    monkeypatch.setattr(companion_main, "_configure_application_logging", lambda: None)
    monkeypatch.setattr(companion_main, "_load_application_symbols", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        companion_main._run_application()

    assert raised.value is start_failure
    assert events == ["run", "close"]


def test_run_application_keeps_run_failure_primary_when_cleanup_fails(monkeypatch):
    events = []
    run_failure = RuntimeError("run failure")
    cleanup_failure = RuntimeError("cleanup failure")

    class FakeCompanion:
        def __init__(self):
            self.ble = None

        async def run(self):
            events.append("run")
            raise run_failure

        async def aclose(self):
            events.append("close")
            raise cleanup_failure

    monkeypatch.setattr(companion_main, "Companion", FakeCompanion)
    monkeypatch.setattr(companion_main, "_configure_application_logging", lambda: None)
    monkeypatch.setattr(companion_main, "_load_application_symbols", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        companion_main._run_application()

    assert raised.value is run_failure
    assert raised.value.__notes__
    assert "cleanup failure" in raised.value.__notes__[0]
    assert events == ["run", "close"]


def test_run_application_propagates_cleanup_failure_after_successful_run(monkeypatch):
    events = []
    cleanup_failure = RuntimeError("cleanup failure")

    class FakeCompanion:
        def __init__(self):
            self.ble = None

        async def run(self):
            events.append("run")

        async def aclose(self):
            events.append("close")
            raise cleanup_failure

    monkeypatch.setattr(companion_main, "Companion", FakeCompanion)
    monkeypatch.setattr(companion_main, "_configure_application_logging", lambda: None)
    monkeypatch.setattr(companion_main, "_load_application_symbols", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        companion_main._run_application()

    assert raised.value is cleanup_failure
    assert events == ["run", "close"]


def test_entrypoint_releases_lease_after_application_cleanup(monkeypatch):
    events = []

    class Context:
        def __enter__(self):
            events.append("lease_enter")
            return object()

        def __exit__(self, exc_type, exc_value, traceback):
            events.append("lease_exit")
            return False

    class Lock:
        def runtime(self):
            return Context()

    def validate(lease):
        events.append("validate")

    def runner():
        events.extend(("run", "close_begin", "close_end"))
        return 0

    monkeypatch.setattr(companion_main, "validate_gerald_runtime_lease", validate)
    result = companion_main._run_gerald_entrypoint(
        lock_factory=Lock,
        application_runner=runner,
        stderr=StringIO(),
    )

    assert result == 0
    assert events == [
        "lease_enter",
        "validate",
        "run",
        "close_begin",
        "close_end",
        "lease_exit",
    ]


class RealLifecycleRunFailure(BaseException):
    pass


class RealLifecycleCleanupFailure(BaseException):
    pass


def run_real_lifecycle(monkeypatch, events, *, run_failure=None, close_failure=None):
    lease_state = {"held": False}

    class RuntimeContext:
        def __enter__(self):
            events.append("lease_enter")
            lease_state["held"] = True
            return object()

        def __exit__(self, exc_type, exc_value, traceback):
            events.append("lease_exit")
            lease_state["held"] = False
            return False

    class FakeLock:
        def runtime(self):
            return RuntimeContext()

    class FakeCompanion:
        async def run(self):
            assert lease_state["held"] is True
            events.append("companion_run")
            if run_failure is not None:
                events.append("run_failure")
                raise run_failure

        async def aclose(self):
            assert lease_state["held"] is True
            events.append("companion_close_begin")
            if close_failure is not None:
                events.append("close_failure")
                raise close_failure
            events.append("companion_close_end")

    monkeypatch.setattr(companion_main, "Companion", FakeCompanion)
    monkeypatch.setattr(companion_main, "_configure_application_logging", lambda: None)
    monkeypatch.setattr(companion_main, "_load_application_symbols", lambda: None)
    monkeypatch.setattr(companion_main, "validate_gerald_runtime_lease", lambda lease: None)
    return companion_main._run_gerald_entrypoint(
        lock_factory=FakeLock,
        application_runner=companion_main._run_application,
        stderr=StringIO(),
    )


def test_real_lifecycle_normal_path_holds_lease_during_run_and_close(monkeypatch):
    events = []

    assert run_real_lifecycle(monkeypatch, events) == 0
    assert events == [
        "lease_enter",
        "companion_run",
        "companion_close_begin",
        "companion_close_end",
        "lease_exit",
    ]


def test_real_lifecycle_run_failure_closes_before_lease_exit(monkeypatch):
    events = []
    run_failure = RealLifecycleRunFailure("run failure")

    with pytest.raises(RealLifecycleRunFailure) as raised:
        run_real_lifecycle(monkeypatch, events, run_failure=run_failure)

    assert raised.value is run_failure
    assert str(raised.value) == "run failure"
    assert events == [
        "lease_enter",
        "companion_run",
        "run_failure",
        "companion_close_begin",
        "companion_close_end",
        "lease_exit",
    ]


def test_real_lifecycle_cleanup_failure_exits_lease_after_close_attempt(monkeypatch):
    events = []
    close_failure = RealLifecycleCleanupFailure("cleanup failure")

    with pytest.raises(RealLifecycleCleanupFailure) as raised:
        run_real_lifecycle(monkeypatch, events, close_failure=close_failure)

    assert raised.value is close_failure
    assert events == [
        "lease_enter",
        "companion_run",
        "companion_close_begin",
        "close_failure",
        "lease_exit",
    ]


def test_real_lifecycle_run_failure_remains_primary_when_close_fails(monkeypatch):
    events = []
    run_failure = RealLifecycleRunFailure("run failure")
    close_failure = RealLifecycleCleanupFailure("cleanup failure")

    with pytest.raises(RealLifecycleRunFailure) as raised:
        run_real_lifecycle(
            monkeypatch,
            events,
            run_failure=run_failure,
            close_failure=close_failure,
        )

    assert raised.value is run_failure
    assert str(raised.value) == "run failure"
    assert raised.value.__notes__ == [
        "Companion cleanup failure: RealLifecycleCleanupFailure('cleanup failure')"
    ]
    assert events == [
        "lease_enter",
        "companion_run",
        "run_failure",
        "companion_close_begin",
        "close_failure",
        "lease_exit",
    ]


def test_real_lifecycle_ble_start_failure_still_closes_before_lease_exit(monkeypatch):
    events = []
    start_failure = RealLifecycleRunFailure("BLE start failure")

    with pytest.raises(RealLifecycleRunFailure) as raised:
        run_real_lifecycle(monkeypatch, events, run_failure=start_failure)

    assert raised.value is start_failure
    assert events == [
        "lease_enter",
        "companion_run",
        "run_failure",
        "companion_close_begin",
        "companion_close_end",
        "lease_exit",
    ]
