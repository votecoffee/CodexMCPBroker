"""Optional SDK matrix: an explicit separate interpreter, never a runtime upgrade."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

if version("mcp") != "2.3.0":
    raise pytest.UsageError("Run this profile with the broker's locked mcp==2.3.0 environment")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from universal_tools.cli import process_options  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--sdk22-root", help="Isolated SDK 2.2 overlay directory; see VERIFICATION.md")
    parser.addoption("--sdk2-python", help="Separate Python environment containing mcp==2.3.0")


@pytest.fixture(scope="session")
def sdk2_python(request):
    value = request.config.getoption("--sdk2-python")
    if not value:
        raise pytest.UsageError("This optional profile requires --sdk2-python; see VERIFICATION.md")
    python = Path(value).resolve()
    if not python.is_file():
        raise pytest.UsageError("--sdk2-python must name an existing Python executable")
    result = subprocess.run(
        [str(python), "-c", "from importlib.metadata import version; print(version('mcp'))"],
        capture_output=True,
        text=True,
        timeout=20,
        **process_options(),
    )
    if result.returncode or result.stdout.strip() != "2.3.0":
        raise pytest.UsageError("The separate server environment must contain mcp==2.3.0")
    return python


@pytest.fixture(scope="session")
def sdk22_root(request):
    value = request.config.getoption("--sdk22-root")
    if not value or not Path(value).is_dir():
        raise pytest.UsageError("This profile requires --sdk22-root; see VERIFICATION.md")
    return Path(value).resolve()
