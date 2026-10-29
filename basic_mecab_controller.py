# Copyright: Ren Tatsumoto <tatsu at autistici.org> and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import functools
import os
import subprocess
import tempfile
from typing import Optional

try:
    from .mecab_exe_finder import IS_ANDROID, IS_WIN, SUPPORT_DIR, find_executable
    from .mecab_library import InProcessMecab, MecabUnavailableError, describe_os_error, describe_platform
except ImportError:
    from mecab_exe_finder import IS_ANDROID, IS_WIN, SUPPORT_DIR, find_executable
    from mecab_library import InProcessMecab, MecabUnavailableError, describe_os_error, describe_platform

INPUT_BUFFER_SIZE = str(819200)
MECAB_RC_PATH = os.path.join(SUPPORT_DIR, "mecabrc")
# "in_process" or "subprocess": how MeCab runs when the code that makes the controller does not
# say. For trying one backend in place of the other without touching that code.
BACKEND_ENV = "MECAB_CONTROLLER_BACKEND"


@functools.cache
def startup_info():
    if IS_WIN:
        # Prevents a console window from popping up on Windows
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        si = None
    return si


@functools.cache
def find_best_dic_dir():
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
    return SUPPORT_DIR


def normalize_for_platform(popen: list[str]) -> list[str]:
    if IS_WIN:
        popen = [os.path.normpath(x) for x in popen]
    return popen


def check_mecab_rc():
    if not os.path.isfile(MECAB_RC_PATH):
        with open(MECAB_RC_PATH, "w") as f:
            # create mecabrc if it doesn't exist
            f.write("")


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


def expr_to_bytes(expr: str) -> bytes:
    return expr.encode("utf-8", "ignore") + b"\n"


def mecab_output_to_str(outs: bytes) -> str:
    return outs.rstrip(b"\r\n").decode("utf-8", "replace")


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


def prepend_library_path() -> None:
    for library_path in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        try:
            os.environ[library_path] = f"{SUPPORT_DIR}:{os.environ[library_path]}"
        except KeyError:
            os.environ[library_path] = SUPPORT_DIR


class BasicMecabController:
    _mecab_cmd: list[str] = [
        find_executable("mecab"),
        "--dicdir=" + find_best_dic_dir(),
        "--rcfile=" + MECAB_RC_PATH,
        "--userdic=" + os.path.join(SUPPORT_DIR, "user_dic.dic"),
        "--input-buffer-size=" + INPUT_BUFFER_SIZE,
    ]
    _mecab_args: list[str] = []
    _verbose: bool
    _in_process: Optional[InProcessMecab]

    def __init__(
        self,
        mecab_cmd: Optional[list[str]] = None,
        mecab_args: Optional[list[str]] = None,
        verbose: bool = False,
        in_process: Optional[bool] = None,
    ) -> None:
        """
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
        if not in_process:
            prepend_library_path()
        if self._verbose:
            print("mecab cmd:", self._mecab_cmd, "(in process)" if in_process else "")

    @property
    def in_process(self) -> bool:
        return self._in_process is not None

    def run(self, expr: str) -> str:
        if self._in_process is not None:
            outs = self._in_process.run(expr_to_bytes(expr))
        else:
            outs = self._run_executable(expr)

        str_out = mecab_output_to_str(outs)
        if "tagger.cpp" in str_out and "no such file or directory" in str_out:
            raise RuntimeError("Please ensure your Windows user name contains only English characters.")
        return str_out

    def _run_executable(self, expr: str) -> bytes:
        try:
            proc = subprocess.Popen(
                self._mecab_cmd,
                bufsize=-1,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=startup_info(),
            )
        except OSError as e:
            exe = self._mecab_cmd[0]
            raise MecabUnavailableError(
                f"MeCab could not be started on {describe_platform()}: {exe}: {describe_os_error(e, exe)}"
            ) from e

        try:
            outs, errs = proc.communicate(expr_to_bytes(expr), timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            outs, errs = proc.communicate()
        return outs


def main():
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
