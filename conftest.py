"""
Various common utilities for testing.
"""

import contextlib
import os
import pathlib
import re
import sys
from collections.abc import Sequence

import pytest

PYODIDE_ROOT = pathlib.Path(__file__).parents[0].resolve()
DIST_PATH = PYODIDE_ROOT / "dist"

sys.path.append(str(PYODIDE_ROOT / "src" / "py"))

# importing this fixture has a side effect of making the safari webdriver reused during the session
from pytest_pyodide import get_global_config
from pytest_pyodide.runner import use_global_safari_service  # noqa: F401
from pytest_pyodide.utils import package_is_built as _package_is_built

os.environ["IN_PYTEST"] = "1"


def set_configs():
    pytest_pyodide_config = get_global_config()

    pytest_pyodide_config.set_flags(
        "chrome",
        pytest_pyodide_config.get_flags("chrome")
        + [
            # Note: in Chrome > 128 (or so?) WebAssemblyExperimentalJSPI no
            # longer implies type reflection. If we passed
            # `--enable-experimental-webassembly-features`
            # here it would enable type reflection. We'd like to make sure that
            # everything works in the configuration where JSPI exists but type
            # reflection does not, so we don't pass it.
            "--enable-features=WebAssemblyExperimentalJSPI",
        ],
    )

    pytest_pyodide_config.set_flags(
        "node",
        pytest_pyodide_config.get_flags("node")
        + ["--experimental-wasm-stack-switching"],
    )

    # There are a bunch of global objects that occasionally enter the hiwire cache
    # but never leave. The refcount checks get angry about them if they aren't preloaded.
    # We need to go through and touch them all once to keep everything okay.
    pytest_pyodide_config.set_initialize_script("""
        pyodide.globals.get;
        pyodide.runPython("import pyodide_js._api.config; del pyodide_js");
        pyodide._api.importlib.invalidate_caches;
        pyodide._api.package_loader.get_install_dir;
        pyodide._api.package_loader.unpack_buffer;
        pyodide._api.package_loader.get_dynlibs;
        pyodide._api.pyodide_code.eval_code;
        pyodide._api.pyodide_code.eval_code_async;
        pyodide._api.pyodide_code.relaxed_call
        pyodide._api.pyodide_code.find_imports;
        pyodide._api.pyodide_ffi.register_js_module;
        pyodide._api.pyodide_ffi.unregister_js_module;
        pyodide.pyimport("pyodide.ffi.wrappers").destroy();
        pyodide.pyimport("pyodide.http").destroy();
        pyodide.pyimport("pyodide_js._api");
    """)

    pytest_pyodide_config.set_load_pyodide_script(
        "chrome",
        """
        let pyodide = await loadPyodide({
            fullStdLib: false,
            jsglobals : self,
        });
        """,
    )

    pytest_pyodide_config.set_load_pyodide_script(
        "node",
        """
        const {readFileSync} = require("fs");
        let snap = readFileSync("snapshot.bin");
        snap = new Uint8Array(snap.buffer);
        let pyodide = await loadPyodide({
            fullStdLib: false,
            jsglobals: self,
            _loadSnapshot: snap,
        });
        """,
    )
    pytest_pyodide_config.add_node_extra_globals(["URL", "Headers", "Response"])


set_configs()

only_node = pytest.mark.xfail_browsers(
    chrome="node only", firefox="node only", safari="node only"
)
only_chrome = pytest.mark.xfail_browsers(
    node="chrome only", firefox="chrome only", safari="chrome only"
)

requires_jspi = pytest.mark.xfail_browsers(
    firefox="requires jspi", safari="requires jspi"
)


def pytest_addoption(parser):
    group = parser.getgroup("general")
    group.addoption(
        "--run-xfail",
        action="store_true",
        help="If provided, tests marked as xfail will be run",
    )
    group.addoption(
        "--skip-passed",
        action="store_true",
        help=(
            "If provided, tests that passed on the last run will be skipped. "
            "CAUTION: this will skip tests even if tests are modified"
        ),
    )


def maybe_skip_test(item, delayed=False):
    """If necessary skip test at the fixture level, to avoid
    loading the selenium_standalone fixture which takes a long time.
    """
    browsers = "|".join(["firefox", "chrome", "node", "safari"])
    is_common_test = str(item.fspath).endswith("test_packages_common.py")

    skip_msg = None
    # Testing a package. Skip the test if the package is not built.
    match = re.match(
        r".*/packages/(?P<name>[\w\-]+)/test_[\w\-]+\.py", str(item.parent.fspath)
    )
    if match and not is_common_test:
        package_name = match.group("name")
        if not package_is_built(package_name) and re.match(
            rf"test_[\w\-\.]+\[({browsers})[^\]]*\]", item.name
        ):
            skip_msg = f"package '{package_name}' is not built."

    # TODO: also use this hook to skip doctests we cannot run (or run them
    # inside the selenium wrapper)

    if skip_msg is not None:
        if delayed:
            item.add_marker(pytest.mark.skip(reason=skip_msg))
        else:
            pytest.skip(skip_msg)


def pytest_configure(config):
    """Monkey patch the function cwd_relative_nodeid

    returns the description of a test for the short summary table. Monkey patch
    it to reduce the verbosity of the test names in the table.  This leaves
    enough room to see the information about the test failure in the summary.
    """
    global CONFIG  # noqa: PLW0602

    old_cwd_relative_nodeid = config.cwd_relative_nodeid

    def cwd_relative_nodeid(*args):
        result = old_cwd_relative_nodeid(*args)
        result = result.replace("src/tests/", "")
        result = result.replace("packages/", "")
        result = result.replace("::test_", "::")
        return result

    config.cwd_relative_nodeid = cwd_relative_nodeid

    pytest.pyodide_dist_dir = config.getoption("--dist-dir")  # type:ignore[attr-defined]


def pytest_collection_modifyitems(config, items):
    """Called after collect is completed.
    Parameters
    ----------
    config : pytest config
    items : list of collected items
    """
    prev_test_result = {}
    if config.getoption("--skip-passed"):
        cache = config.cache
        prev_test_result = cache.get("cache/lasttestresult", {})

    # Skip long_running tests unless in CI environment
    is_ci = os.environ.get("CI", "").lower() in ("true", "1", "yes")

    for item in items:
        if prev_test_result.get(item.nodeid) in ("passed", "warnings", "skip_passed"):
            item.add_marker(pytest.mark.skip(reason="previously passed"))
            continue

        # Skip long_running tests unless explicitly running them or in CI
        if item.get_closest_marker("long_running") and not is_ci:
            # Check if user explicitly wants to run long_running tests
            markexpr = config.getoption("-m", default="")
            if "long_running" not in markexpr:
                item.add_marker(
                    pytest.mark.skip(
                        reason="long_running test skipped (use '-m long_running' to run or set CI=1)"
                    )
                )
                continue

        maybe_skip_test(item, delayed=True)


# Save test results to a cache
# Code adapted from: https://github.com/pytest-dev/pytest/blob/main/src/_pytest/pastebin.py
@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter):
    tr = terminalreporter
    cache = tr.config.cache
    assert cache

    test_result = {}
    for status in tr.stats:
        if status in ("warnings", "deselected"):
            continue

        for test in tr.stats[status]:
            if test.when != "call":  # discard results from setup/teardown
                continue

            try:
                if test.longrepr and test.longrepr[2] in "previously passed":
                    test_result[test.nodeid] = "skip_passed"
                else:
                    test_result[test.nodeid] = test.outcome
            except Exception:
                pass

    cache.set("cache/lasttestresult", test_result)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """We want to run extra verification at the start and end of each test to
    check that we haven't leaked memory. According to pytest issue #5044, it's
    not possible to "Fail" a test from a fixture (no matter what you do, pytest
    sets the test status to "Error"). The approach suggested there is hook
    pytest_runtest_call as we do here. To get access to the selenium fixture, we
    imitate the definition of pytest_pyfunc_call:
    https://github.com/pytest-dev/pytest/blob/6.2.2/src/_pytest/python.py#L177

    Pytest issue #5044:
    https://github.com/pytest-dev/pytest/issues/5044
    """
    browser = None
    for fixture in item._fixtureinfo.argnames:
        if fixture.startswith("selenium"):
            browser = item.funcargs[fixture]
            break

    if not browser or not browser.pyodide_loaded:
        result = yield
        return result

    trace_pyproxies = pytest.mark.skip_pyproxy_check.mark not in item.own_markers
    trace_hiwire_refs = (
        trace_pyproxies and pytest.mark.skip_refcount_check.mark not in item.own_markers
    )
    yield from extra_checks_test_wrapper(
        browser, trace_hiwire_refs, trace_pyproxies, item
    )


def extra_checks_test_wrapper(browser, trace_hiwire_refs, trace_pyproxies, item):
    """Extra conditions for test to pass:
    1. No explicit request for test to fail
    2. No leaked JsRefs
    3. No leaked PyProxys
    """
    browser.clear_force_test_fail()
    init_num_keys = browser.get_num_hiwire_keys()
    if trace_pyproxies:
        browser.enable_pyproxy_tracing()
        init_num_proxies = browser.get_num_proxies()
    err = False
    try:
        result = yield
    except Exception:
        err = True
        raise
    finally:
        # Suppress any errors if an error was raised so we keep the original error
        with contextlib.suppress(Exception) if err else contextlib.nullcontext():
            browser.disable_pyproxy_tracing()
            browser.restore_state()
            # The method_call_singleton holds onto the last called JS method,
            # clear it so we don't fail refcount check
            browser.run_js("pyodide._module._clear_method_call_singleton();")

    if browser.force_test_fail:
        raise Exception("Test failure explicitly requested but no error was raised.")
    if trace_pyproxies and trace_hiwire_refs:
        delta_proxies = browser.get_num_proxies() - init_num_proxies
        delta_keys = browser.get_num_hiwire_keys() - init_num_keys
        if delta_proxies > 0:
            pxs = browser.run_js(
                """
                return Array.from(pyodide._module.pyproxy_alloc_map.entries(), ([x, s]) => [x.type, x.toString(), "Traceback at creation:" + s.replace("Error", "")])
                """
            )
            capman = item.config.pluginmanager.getplugin("capturemanager")
            with capman.item_capture("call", item):
                print("\n" + "!" * 40)
                print("leaked proxies:")
                for row in pxs:
                    print(*row)

        assert (delta_proxies, delta_keys) == (0, 0) or delta_keys < 0
    if trace_hiwire_refs:
        delta_keys = browser.get_num_hiwire_keys() - init_num_keys
        assert delta_keys <= 0
    return result


def package_is_built(package_name):
    return _package_is_built(package_name, pytest.pyodide_dist_dir)  # type:ignore[attr-defined]


def strip_assertions_stderr(messages: Sequence[str]) -> list[str]:
    """Strip additional messages on stderr included when ASSERTIONS=1"""
    res = []
    for msg in messages:
        if msg.strip() in [
            "sigaction: signal type not supported: this is a no-op.",
            "Calling stub instead of siginterrupt()",
            "warning: no blob constructor, cannot create blobs with mimetypes",
            "warning: no BlobBuilder",
        ]:
            continue
        res.append(msg)
    return res
