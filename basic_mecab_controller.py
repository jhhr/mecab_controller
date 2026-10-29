# Copyright: Ren Tatsumoto <tatsu at autistici.org> and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import contextlib
import functools
import os
import subprocess
import tempfile
import typing

try:
    from .mecab_exe_finder import IS_ANDROID, IS_WIN, SUPPORT_DIR, find_executable
    from .mecab_library import InProcessMecab, MecabUnavailableError, describe_platform
except ImportError:
    from mecab_exe_finder import IS_ANDROID, IS_WIN, SUPPORT_DIR, find_executable
    from mecab_library import InProcessMecab, MecabUnavailableError, describe_platform

INPUT_BUFFER_SIZE = str(819200)
MECAB_TIMEOUT_SEC = 5
MECAB_RC_PATH = SUPPORT_DIR / "mecabrc"
# "in_process" or "subprocess": how MeCab runs when the code that makes the controller does not
# say. For trying one backend in place of the other without touching that code.
BACKEND_ENV = "MECAB_CONTROLLER_BACKEND"


@functools.cache
def startup_info() -> typing.Any:
    """Return Windows startup settings that suppress the console window."""
    if IS_WIN:
        # Prevents a console window from popping up on Windows
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        si = None
    return si


@functools.cache
def find_best_dic_dir() -> str:
    """
    If the user has mecab-ipadic-neologd (or mecab-ipadic) installed, pick its system dictionary.
    """
    possible_locations = (
        "/usr/lib/mecab/dic/mecab-ipadic-neologd",
        "/usr/local/lib/mecab/dic/mecab-ipadic-neologd",
        "/opt/homebrew/lib/mecab/dic/mecab-ipadic-neologd",
        "/usr/lib/mecab/dic/ipadic",
        "/usr/local/lib/mecab/dic/ipadic",  # for `brew install mecab-ipadic`
        "/opt/homebrew/lib/mecab/dic/ipadic",
    )
    for directory in possible_locations:
        if os.path.isdir(directory):
            return directory
    return str(SUPPORT_DIR)


def normalize_for_platform(popen: list[str]) -> list[str]:
    """Normalize command paths for the current platform."""
    if IS_WIN:
        popen = [os.path.normpath(x) for x in popen]
    return popen


def check_mecab_rc() -> None:
    """Create the bundled mecabrc file when it does not exist."""
    if MECAB_RC_PATH.is_file():
        return
    MECAB_RC_PATH.write_text("", encoding="utf-8")


def mend_line_endings(path: str) -> None:
    """
    Give a bundled MeCab config file back the LF line endings it has in the repository.
    Git on Windows checks text files out with CRLF (core.autocrlf), and the mecab executable
    there reads them in text mode, which drops the CR again. Everywhere else MeCab reads the
    bytes as they are, so a blank line is "\\r", which its rc parser rejects ("format error"):
    an add-on packaged from a Windows checkout could start MeCab on Windows only, and on
    Android in process not at all. The repository's .gitattributes keeps new checkouts from
    doing this; this mends a file that came from an older one. A file that cannot be mended
    is left alone, and MeCab then says what is wrong with it.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return
    if b"\r\n" not in data:
        return
    directory = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.replace(b"\r\n", b"\n"))
        os.replace(tmp, path)  # whole or not at all: another controller may be reading it
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def in_process_by_default() -> bool:
    """
    Android cannot start the executable (see mecab_library.py), so MeCab runs in process there.
    Elsewhere the executable stays the default: it is what has always run there, and a crash in
    a process of its own cannot take the host application down with it.
    """
    backend = os.environ.get(BACKEND_ENV, "")
    if backend == "in_process":
        return True
    if backend == "subprocess":
        return False
    if backend:
        raise ValueError(f"{BACKEND_ENV} must be 'in_process' or 'subprocess', not {backend!r}")
    return IS_ANDROID


class MecabProcessError(RuntimeError):
    """Raised when MeCab launch, communication, timeout, or exit validation fails."""


class MecabProcessOutput(typing.NamedTuple):
    """Capture the separate standard streams emitted by a MeCab process."""

    stdout: str
    stderr: str


def mecab_subprocess_environment() -> dict[str, str]:
    """Return a private environment that loads AJT's bundled MeCab library first."""
    environment = os.environ.copy()
    for library_path in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        try:
            environment[library_path] = f"{SUPPORT_DIR}{os.pathsep}{environment[library_path]}"
        except KeyError:
            environment[library_path] = str(SUPPORT_DIR)
    return environment


def kill_and_drain_process(proc: subprocess.Popen[str]) -> MecabProcessOutput:
    """Terminate a failed MeCab process, reap it, and return its remaining output."""
    with contextlib.suppress(OSError):
        proc.kill()
    try:
        return MecabProcessOutput(*proc.communicate())
    except OSError:
        return MecabProcessOutput("", "")


def format_error_msg(error: str, output: MecabProcessOutput) -> str:
    """Append MeCab's standard-error output to an error message when available."""
    if output.stderr:
        return f"{error} stderr: {output.stderr}"
    return error


def is_windows_username_error(output: MecabProcessOutput) -> bool:
    """Return whether MeCab failed because the Windows user name has non-ASCII characters."""
    diagnostics = f"{output.stdout}\n{output.stderr}"
    return "tagger.cpp" in diagnostics and "no such file or directory" in diagnostics


def communicate_with_mecab(proc: subprocess.Popen[str], expr: str, command: list[str]) -> MecabProcessOutput:
    """Communicate with MeCab and turn timeout or pipe failures into structured errors."""
    try:
        return MecabProcessOutput(*proc.communicate(f"{expr}\n", timeout=MECAB_TIMEOUT_SEC))
    except subprocess.TimeoutExpired as ex:
        raise MecabProcessError(
            format_error_msg(
                f"MeCab command {command!r} timed out after {MECAB_TIMEOUT_SEC} seconds.",
                output=kill_and_drain_process(proc),
            )
        ) from ex
    except OSError as ex:
        raise MecabProcessError(
            format_error_msg(
                f"Unable to communicate with MeCab command {command!r}: {ex}",
                output=kill_and_drain_process(proc),
            )
        ) from ex


def start_mecab_process(command: list[str]) -> subprocess.Popen[str]:
    """Start MeCab with private library paths and UTF-8 text streams."""
    try:
        return subprocess.Popen(
            command,
            bufsize=-1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startup_info(),
            env=mecab_subprocess_environment(),
        )
    except OSError as ex:
        raise MecabProcessError(f"Unable to start MeCab command {command!r} on {describe_platform()}: {ex}") from ex


class BasicMecabController:
    """Run the bundled MeCab executable with AJT's configured dictionaries."""

    _mecab_cmd: list[str] = [
        find_executable("mecab"),
        f"--dicdir={find_best_dic_dir()}",
        f"--rcfile={MECAB_RC_PATH}",
        f"--userdic={SUPPORT_DIR.joinpath('user_dic.dic')}",
        f"--input-buffer-size={INPUT_BUFFER_SIZE}",
    ]
    _mecab_args: list[str] = []
    _verbose: bool
    _in_process: InProcessMecab | None

    def __init__(
        self,
        mecab_cmd: list[str] | None = None,
        mecab_args: list[str] | None = None,
        verbose: bool = False,
        in_process: bool | None = None,
    ) -> None:
        """
        Initialize MeCab with optional command overrides for standalone use and tests.

        in_process: load libmecab into this process instead of starting the executable for each
        call; None leaves it to in_process_by_default(). In process, the options of mecab_cmd
        are given to the library bundled in "support", whatever program the command names.
        """
        super().__init__()
        check_mecab_rc()
        for name in ("dicrc", "mecabrc"):
            mend_line_endings(os.path.join(SUPPORT_DIR, name))
        self._verbose = verbose
        self._mecab_cmd = normalize_for_platform((mecab_cmd or self._mecab_cmd) + (mecab_args or self._mecab_args))
        if in_process is None:
            in_process = in_process_by_default()
        self._in_process = InProcessMecab(self._mecab_cmd) if in_process else None
        if self._verbose:
            print("mecab cmd:", self._mecab_cmd, "(in process)" if in_process else "")

    @property
    def in_process(self) -> bool:
        return self._in_process is not None

    def run(self, expr: str) -> str:
        """Run MeCab for one expression and return its standard output."""
        if self._in_process is not None:
            return self._run_in_process(expr)
        proc = start_mecab_process(self._mecab_cmd)
        output = communicate_with_mecab(proc, expr, self._mecab_cmd)
        if is_windows_username_error(output):
            raise MecabProcessError("Please ensure your Windows user name contains only English characters.")
        if proc.returncode:
            raise MecabProcessError(
                f"MeCab exited with status {proc.returncode}. command: {self._mecab_cmd!r}. stderr: {output.stderr}."
            )
        return output.stdout.strip("\r\n")

    def _run_in_process(self, expr: str) -> str:
        """
        What run() returns from the executable, with MeCab loaded into this process instead.
        The text goes in and comes out as start_mecab_process's text streams pass it: UTF-8 with
        errors replaced, and on the way back \\r\\n and \\r read as \\n (universal newlines).
        """
        assert self._in_process is not None
        try:
            stdout, error = self._in_process.run(f"{expr}\n".encode("utf-8", "replace"))
        except MecabUnavailableError as ex:
            raise MecabProcessError(str(ex)) from ex
        if error is not None:
            # Where the executable would have printed this and exited with a failure.
            raise MecabProcessError(f"MeCab failed in process. command: {self._mecab_cmd!r}. error: {error}")
        text = stdout.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        return text.strip("\r\n")


def main() -> None:
    mecab = BasicMecabController()

    try_expressions = (
        "カリン、自分でまいた種は自分で刈り取れ",
        "昨日、林檎を2個買った。",
        "真莉、大好きだよん＾＾",
        "彼２０００万も使った。",
        "彼二千三百六十円も使った。",
        "千葉",
        "昨日すき焼きを食べました",
        "二人の美人",
        "詳細はお気軽にお問い合わせ下さい。",
        "Lorem ipsum dolor sit amet. Съешь ещё этих мягких французских булок, да выпей же чаю.",
    )

    for expr in try_expressions:
        print(mecab.run(expr))


if __name__ == "__main__":
    main()
