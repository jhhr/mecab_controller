# Copyright: Ajatt-Tools and contributors; https://github.com/Ajatt-Tools
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""
MeCab run inside this process through ctypes, for where the mecab executable cannot be started.

Kioku (github.com/krmanik/kioku) runs the Anki desktop app on Android, and an add-on there cannot
start a program: its folder is on shared storage, which is mounted noexec, and an app targeting
Android 10 or later may not exec a file from its private storage either. It can still load a
shared library from private storage, the way Python loads its own extension modules there. So on
Android the bundled library is copied into the app's private storage and loaded from there.

InProcessMecab.run() takes the bytes the executable would read from stdin and returns the bytes
it would write, stderr included, for the same command line. That is what keeps a field the
phone writes identical to the one the desktop writes, once the two sync. The executable reads
its input a line at a time into a buffer of --input-buffer-size bytes, tags each line as a C
string and writes each result as it comes; run() does the same with the library's own parse.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
import tempfile
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Optional

# ctypes is imported where a library is loaded: a Python built without it can still run the
# executable, and importing this package must not fail there.
if TYPE_CHECKING:
    import ctypes

try:
    from .mecab_exe_finder import IS_ANDROID, IS_MAC, IS_WIN, SUPPORT_DIR
except ImportError:
    from mecab_exe_finder import IS_ANDROID, IS_MAC, IS_WIN, SUPPORT_DIR


class MecabUnavailableError(RuntimeError):
    """MeCab could not be started, its library could not be loaded, or it refused its options."""


def describe_platform() -> str:
    """The platform as an error message should name it."""
    bits = 64 if sys.maxsize > 2**32 else 32
    try:
        name = platform.platform()
    except Exception:
        name = f"{sys.platform} {platform.machine()}"
    return f"{name} ({sys.platform}), {bits}-bit Python {platform.python_version()}"


def describe_os_error(error: OSError, path: str = "") -> str:
    """The error for a message that names path itself, so without the path a second time."""
    name = type(error).__name__
    winerror = getattr(error, "winerror", None)
    if winerror is not None and error.strerror:
        return f"{name}: [WinError {winerror}] {error.strerror}"
    if error.errno is not None and error.strerror:
        return f"{name}: [Errno {error.errno}] {error.strerror}"
    text = str(error)  # ctypes puts dlerror() here, which starts with the path
    if path and text.startswith(path + ": "):
        text = text[len(path) + 2 :]
    return f"{name}: {text}"


# Android ABI names by (machine, pointer size). A 32-bit process on a 64-bit kernel reports
# armv8l or x86_64 as its machine.
ANDROID_ABIS = {
    ("aarch64", 64): "arm64-v8a",
    ("armv8l", 32): "armeabi-v7a",
    ("armv7l", 32): "armeabi-v7a",
    ("x86_64", 64): "x86_64",
    ("x86_64", 32): "x86",
    ("i686", 32): "x86",
}


def bundled_library_path() -> str:
    """The library in the "support" folder for this platform; it need not exist."""
    if IS_ANDROID:
        bits = 64 if sys.maxsize > 2**32 else 32
        abi = ANDROID_ABIS.get((platform.machine(), bits), platform.machine())
        name = f"libmecab-android-{abi}.so"
    elif IS_WIN:
        name = "libmecab.dll"
    elif IS_MAC:
        name = "libmecab.2.dylib"
    else:
        name = "libmecab.so.1"
    return os.path.join(SUPPORT_DIR, name)


def private_library_dirs() -> list[str]:
    """
    Where on Android a copy of the library can be loaded from, best first.
    The folder the library ships in is usually on shared storage, where nothing can be mapped
    executable. An app's cache and files folders are its own and can: Kioku sets TMPDIR to the
    first and HOME to the second, and python.org's Android testbed sets TMPDIR the same way.
    """
    candidates = [os.environ.get("TMPDIR"), os.path.join(os.path.expanduser("~"), ".cache")]
    dirs: list[str] = []
    for base in candidates:
        if base and os.path.isabs(base):
            directory = os.path.join(base, "mecab_controller")
            if directory not in dirs:
                dirs.append(directory)
    return dirs


def holds(path: str, data: bytes) -> bool:
    """Whether the file at path has exactly these bytes."""
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def write_copy(path: str, data: bytes) -> None:
    """
    Write the file whole or not at all. Every add-on that bundles this package makes the copy,
    possibly at the same moment, so a reader must never find a half-written one.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Read-only, like a library installed with an app: newer Androids refuse to load
        # writable code through their own loaders, and nothing should change this file anyway.
        os.chmod(tmp, 0o555)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_library() -> tuple[ctypes.CDLL, str]:
    """
    Load the bundled library; returns it and the path it was loaded from.
    On Android the copy is named by its content hash: add-ons that each bundle this package can
    ship different builds, and each must load its own, while identical ones share one file.
    """
    import ctypes

    source = bundled_library_path()
    if not IS_ANDROID:
        try:
            return ctypes.CDLL(source), source
        except OSError as e:
            error = describe_os_error(e, source)
            raise MecabUnavailableError(
                f"MeCab's library could not be loaded on {describe_platform()}: {source}: {error}"
            ) from e

    try:
        with open(source, "rb") as f:
            data = f.read()
    except OSError as e:
        error = describe_os_error(e, source)
        raise MecabUnavailableError(
            f"MeCab's library for {describe_platform()} could not be read: {source}: {error}"
        ) from e
    name = f"libmecab-{hashlib.sha256(data).hexdigest()[:16]}.so"
    failures = []
    for directory in private_library_dirs():
        path = os.path.join(directory, name)
        try:
            if not holds(path, data):
                write_copy(path, data)
            return ctypes.CDLL(path), path
        except OSError as e:
            failures.append(f"{path}: {describe_os_error(e, path)}")
    tried = "; ".join(failures) or "no private folder found (TMPDIR and HOME are unset)"
    raise MecabUnavailableError(
        f"MeCab's library could not be loaded on {describe_platform()} from a copy of {source}: {tried}"
    )


# The executable's options, from MeCab::long_options in tagger.cpp (0.996): long name to
# short name, for those that take a value and for those that do not.
VALUE_OPTIONS = {
    "rcfile": "r",
    "dicdir": "d",
    "userdic": "u",
    "lattice-level": "l",
    "output-format-type": "O",
    "nbest": "N",
    "max-grouping-size": "M",
    "node-format": "F",
    "unk-format": "U",
    "bos-format": "B",
    "eos-format": "E",
    "eon-format": "S",
    "unk-feature": "x",
    "input-buffer-size": "b",
    "theta": "t",
    "cost-factor": "c",
    "output": "o",
}
FLAG_OPTIONS = {
    "dictionary-info": "D",
    "all-morphs": "a",
    "partial": "p",
    "marginal": "m",
    "dump-config": "P",
    "allocate-sentence": "C",
    "version": "v",
    "help": "h",
}
SHORT_NAMES = {short: name for name, short in {**VALUE_OPTIONS, **FLAG_OPTIONS}.items()}
# Options that make the executable do something other than tag stdin to stdout, which a
# library call cannot do the same way.
UNSUPPORTED_OPTIONS = ("output", "partial", "dump-config", "dictionary-info", "version", "help")

# From common.h.
MIN_INPUT_BUFFER_SIZE = 8192
MAX_INPUT_BUFFER_SIZE = 8192 * 640
NBEST_MAX = 512
# What the executable writes to stderr, which the subprocess merges into its output. The
# bundled builds are not all one version (Linux 0.98, Windows 0.98pre3, macOS and Android
# 0.996), and the executable's loop changed in between, so the library's own version decides:
# the warning about --lattice-level came with 0.99, and "splitted" became "split" during 0.991.
LATTICE_LEVEL_WARNING = b"lattice-level is DEPERCATED. use --marginal or --nbest.\n"
OVERFLOW_WARNING = b"input-buffer overflow. The line is split. use -b #SIZE option.\n"
OLD_OVERFLOW_WARNING = b"input-buffer overflow. The line is splitted. use -b #SIZE option.\n"


def version_tuple(version: bytes) -> tuple[int, ...]:
    """b"0.996" -> (0, 996); what follows the numbers ("0.99pre1") is ignored."""
    match = re.match(rb"[0-9]+(?:\.[0-9]+)*", version.strip())
    return tuple(int(part) for part in match.group(0).split(b".")) if match else (0,)


def parse_options(args: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """
    The options in a command line (without the program), read the way MeCab's Param::open reads
    them, and the arguments that are not options. A later option overrides an earlier one.
    MeCab rejects an option it does not know when the tagger is made; here one is skipped.
    """
    options: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            if not name:
                break  # a bare "--" ends the options, and the arguments after it are ignored
            if name in VALUE_OPTIONS:
                if not eq and i + 1 < len(args):
                    i += 1
                    value = args[i]
                options[name] = value
            elif name in FLAG_OPTIONS:
                options[name] = "1"
        elif arg.startswith("-") and len(arg) > 1:
            name = SHORT_NAMES.get(arg[1], "")
            if name in VALUE_OPTIONS:
                value = arg[2:]
                if not value and i + 1 < len(args):
                    i += 1
                    value = args[i]
                options[name] = value
            elif name in FLAG_OPTIONS:
                options[name] = "1"
        elif arg != "-":  # a lone "-" is neither an option nor an argument to MeCab
            rest.append(arg)
        i += 1
    return options, rest


def mecab_int(value: str) -> int:
    """An option's value as an int, as MeCab's lexical_cast reads it: 0 when it is not one."""
    if not re.fullmatch(r"\s*[+-]?[0-9]+\s*", value):
        return 0
    number = int(value)
    return number if -(2**31) <= number < 2**31 else 0


def getline(data: bytes, pos: int, size: int) -> tuple[bytes, int, bool, bool]:
    """
    std::istream::getline(buf, size) on data from pos: the line, where the next read starts,
    whether it hit the end of the input, and whether it stopped because the buffer was full.
    It stores at most size - 1 bytes; a newline right after those is still taken.
    """
    limit = min(len(data), pos + size - 1)
    newline = data.find(b"\n", pos, limit)
    if newline != -1:
        return data[pos:newline], newline + 1, False, False
    line = data[pos:limit]
    if limit == len(data):
        return line, limit, True, False
    if data[limit : limit + 1] == b"\n":
        return line, limit + 1, False, False
    return line, limit, False, True


class InProcessMecab:
    """
    A MeCab tagger in this process, made from the command line the executable would get.
    Not safe to share between threads by itself, so every parse holds the lock; callers run
    MeCab from worker threads. The library is loaded and the tagger made on the first run,
    so that a failure surfaces where MeCab is used, not when the package is imported.
    """

    def __init__(self, cmd: Sequence[str]) -> None:
        self._argv = [os.fsencode(arg) for arg in cmd]
        self._args = self._argv
        options, rest = parse_options(cmd[1:])
        unsupported = [f"--{name}" for name in UNSUPPORTED_OPTIONS if name in options]
        if rest:
            unsupported.append(f"input files {rest}")
        if unsupported:
            raise ValueError(f"MeCab cannot run in process with {', '.join(unsupported)}")
        self._nbest = mecab_int(options.get("nbest", "1"))
        if self._nbest <= 0 or self._nbest > NBEST_MAX:
            raise ValueError(f"invalid N value for MeCab: {options.get('nbest')!r}")
        self._buffer_size = min(
            MAX_INPUT_BUFFER_SIZE,
            max(mecab_int(options.get("input-buffer-size", "0")), MIN_INPUT_BUFFER_SIZE),
        )
        self._lattice_level = mecab_int(options.get("lattice-level", "0"))
        self._preamble = b""
        self._overflow_warning = OVERFLOW_WARNING
        self._lock = threading.Lock()
        self._lib: Optional[ctypes.CDLL] = None
        self._tagger: Optional[int] = None
        self._model: Optional[int] = None  # the tagger's, when the library has the model API
        self._has_model_api = False
        self.library_path: Optional[str] = None

    def _start(self) -> tuple[ctypes.CDLL, int]:
        lib = self._lib if self._lib is not None else self._load()
        if self._tagger is None:
            self._model, self._tagger = self._new_tagger(lib)
        return lib, self._tagger

    def _new_tagger(self, lib: ctypes.CDLL) -> tuple[Optional[int], int]:
        import ctypes

        # The argc/argv forms, not mecab_new2 or mecab_model_new2: those split a single string at
        # whitespace with no quoting, so a dictionary path with a space in it would come apart.
        argv = (ctypes.c_char_p * (len(self._args) + 1))(*self._args, None)
        model = None
        if self._has_model_api:
            # A model and then a tagger, as the executable makes them since 0.99. mecab_new would
            # tag the same, but when the dictionaries fail to open it loses why: its createTagger
            # overwrites the error the model recorded with the tagger's own, which is empty.
            model = lib.mecab_model_new(len(self._args), argv)
            tagger = lib.mecab_model_new_tagger(model) if model else None
        else:
            tagger = lib.mecab_new(len(self._args), argv)
        if not tagger:
            # The error of a failed open is kept per thread, and this is the same thread.
            what = (lib.mecab_strerror(None) or b"").decode("utf-8", "replace") or "MeCab gave no reason"
            if model:
                lib.mecab_model_destroy(model)
            options = " ".join(os.fsdecode(arg) for arg in self._args[1:])
            raise MecabUnavailableError(
                f"MeCab could not start from {self.library_path} with options {options}: {what}"
            )
        return model, tagger

    def _load(self) -> ctypes.CDLL:
        import ctypes

        lib, path = load_library()
        lib.mecab_new.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_char_p))
        lib.mecab_new.restype = ctypes.c_void_p
        lib.mecab_destroy.argtypes = (ctypes.c_void_p,)
        lib.mecab_destroy.restype = None
        self._has_model_api = hasattr(lib, "mecab_model_new")  # 0.99 and later
        if self._has_model_api:
            lib.mecab_model_new.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_char_p))
            lib.mecab_model_new.restype = ctypes.c_void_p
            lib.mecab_model_new_tagger.argtypes = (ctypes.c_void_p,)
            lib.mecab_model_new_tagger.restype = ctypes.c_void_p
            lib.mecab_model_destroy.argtypes = (ctypes.c_void_p,)
            lib.mecab_model_destroy.restype = None
        lib.mecab_strerror.argtypes = (ctypes.c_void_p,)
        lib.mecab_strerror.restype = ctypes.c_char_p
        lib.mecab_sparse_tostr2.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t)
        lib.mecab_sparse_tostr2.restype = ctypes.c_char_p
        lib.mecab_nbest_sparse_tostr2.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        )
        lib.mecab_nbest_sparse_tostr2.restype = ctypes.c_char_p
        lib.mecab_version.argtypes = ()
        lib.mecab_version.restype = ctypes.c_char_p

        version = version_tuple(lib.mecab_version() or b"")
        self._args = list(self._argv)
        if version < (0, 99) and self._nbest >= 2:
            self._args.append(b"--lattice-level=1")  # what the executable did before 0.99
        if version >= (0, 99) and self._lattice_level >= 1:
            self._preamble = LATTICE_LEVEL_WARNING
        if version < (0, 991):
            self._overflow_warning = OLD_OVERFLOW_WARNING
        self._lib, self.library_path = lib, path
        return lib

    def run(self, data: bytes) -> bytes:
        """What the executable writes, stdout and stderr together, when data is its stdin."""
        with self._lock:
            lib, tagger = self._start()
            out = [self._preamble]
            pos = 0
            while True:
                line, pos, eof, overflow = getline(data, pos, self._buffer_size)
                if eof and line[:1] in (b"", b"\0"):
                    break
                if overflow:
                    out.append(self._overflow_warning)
                # The executable hands MeCab the buffer as a C string, which ends at a NUL.
                sentence = line.split(b"\0", 1)[0]
                if self._nbest >= 2:
                    result = lib.mecab_nbest_sparse_tostr2(tagger, self._nbest, sentence, len(sentence))
                else:
                    result = lib.mecab_sparse_tostr2(tagger, sentence, len(sentence))
                if result is None:
                    # The executable prints what went wrong and stops. The next run gets a new
                    # tagger and model, as the next executable would: MeCab's error log never
                    # clears (it resets the stream's flags, not its text), so a model that has
                    # failed once reports its old errors in front of every new one.
                    out.append((lib.mecab_strerror(tagger) or b"") + b"\n")
                    lib.mecab_destroy(tagger)
                    if self._model:
                        lib.mecab_model_destroy(self._model)
                    self._tagger = self._model = None
                    break
                out.append(result)
            return b"".join(out)
