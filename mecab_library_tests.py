# Copyright: Ajatt-Tools and contributors; https://github.com/Ajatt-Tools
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""
The in-process backend has to give exactly what the executable gives, or a field written on a
device that runs MeCab in process differs from the same field written where it runs as a
program. The pure parts (reading a command line the way MeCab does, std::istream::getline) are
checked against what MeCab's C++ does; the rest against the bundled executable itself, wherever
this platform can run both. Where it cannot (the bundled Windows library is 32-bit, which a
64-bit Python cannot load) those cases are skipped.
"""

import hashlib
import os
import stat
import subprocess
import sys
import threading

import pytest

from . import mecab_library
from .basic_mecab_controller import BACKEND_ENV, BasicMecabController, in_process_by_default, mend_line_endings
from .mecab_controller import MecabController
from .mecab_exe_finder import IS_ANDROID, SUPPORT_DIR, get_bundled_executable
from .mecab_library import (
    InProcessMecab,
    MecabUnavailableError,
    getline,
    mecab_int,
    parse_options,
    version_tuple,
)


@pytest.mark.parametrize(
    "data, pos, size, expected",
    [
        pytest.param(b"ab\ncd", 0, 8, (b"ab", 3, False, False), id="a line"),
        pytest.param(b"ab\ncd", 3, 8, (b"cd", 5, True, False), id="the last line, no newline"),
        pytest.param(b"", 0, 8, (b"", 0, True, False), id="nothing left"),
        pytest.param(b"abcdefg\n", 0, 8, (b"abcdefg", 8, False, False), id="a full buffer, then the newline"),
        pytest.param(b"abcdefgh\n", 0, 8, (b"abcdefg", 7, False, True), id="a line longer than the buffer"),
        pytest.param(b"abcdefg", 0, 8, (b"abcdefg", 7, True, False), id="a full buffer at the end"),
    ],
)
def test_getline(data, pos, size, expected):
    assert getline(data, pos, size) == expected


@pytest.mark.parametrize(
    "args, options, rest",
    [
        pytest.param(
            ["--dicdir=/a b/dic", "-b", "100", "-N2", "--node-format", "%m", "-a"],
            {"dicdir": "/a b/dic", "input-buffer-size": "100", "nbest": "2", "node-format": "%m", "all-morphs": "1"},
            [],
            id="long and short options, values attached and apart",
        ),
        pytest.param(["-b1", "--input-buffer-size=2"], {"input-buffer-size": "2"}, [], id="the later one wins"),
        pytest.param(["-", "input.txt"], {}, ["input.txt"], id="a lone dash is ignored, a file is not"),
        pytest.param(["-b5", "--", "--partial", "x"], {"input-buffer-size": "5"}, [], id="a bare -- ends it"),
        pytest.param(["--eos-format="], {"eos-format": ""}, [], id="an empty value"),
    ],
)
def test_parse_options(args, options, rest):
    assert parse_options(args) == (options, rest)


@pytest.mark.parametrize(
    "value, expected",
    [("8192", 8192), (" 12 ", 12), ("+5", 5), ("-3", -3), ("0x10", 0), ("1_000", 0), ("", 0), ("9" * 12, 0)],
)
def test_mecab_int(value, expected):
    assert mecab_int(value) == expected


def test_version_tuple():
    assert version_tuple(b"0.996") == (0, 996)
    assert version_tuple(b"0.98") < (0, 99) <= version_tuple(b"0.99pre1") < (0, 991)
    assert version_tuple(b"") == (0,)


@pytest.mark.parametrize("args", [["--partial"], ["-o", "out.txt"], ["input.txt"], ["--nbest=0"], ["-h"]])
def test_refused_options(args):
    with pytest.raises(ValueError):
        InProcessMecab(["mecab"] + args)


@pytest.mark.parametrize(
    "backend, expected",
    [("in_process", True), ("subprocess", False), ("", IS_ANDROID)],
)
def test_backend_from_environment(monkeypatch, backend, expected):
    monkeypatch.setenv(BACKEND_ENV, backend)
    assert in_process_by_default() is expected


def test_backend_from_environment_rejects_a_typo(monkeypatch):
    monkeypatch.setenv(BACKEND_ENV, "in-process")
    with pytest.raises(ValueError, match=BACKEND_ENV):
        in_process_by_default()


CRLF_DICRC = b"; a config file checked out on Windows\r\n\r\ncost-factor = 800\r\n"


def test_mend_line_endings(tmp_path):
    dicrc = tmp_path / "dicrc"
    dicrc.write_bytes(CRLF_DICRC)
    mend_line_endings(str(dicrc))
    assert dicrc.read_bytes() == CRLF_DICRC.replace(b"\r\n", b"\n")
    assert os.listdir(tmp_path) == ["dicrc"]

    mended = os.stat(dicrc)
    mend_line_endings(str(dicrc))
    assert os.stat(dicrc).st_ino == mended.st_ino, "a file with LF line endings is left alone"
    mend_line_endings(str(tmp_path / "missing"))  # nothing to mend, and no error


def test_an_executable_that_cannot_start_is_named(tmp_path):
    missing = str(tmp_path / "no-such-mecab")
    mecab = BasicMecabController(mecab_cmd=[missing], in_process=False)
    with pytest.raises(MecabUnavailableError) as error:
        mecab.run("x")
    message = str(error.value)
    assert sys.platform in message and missing in message and "FileNotFoundError" in message


def test_a_library_that_cannot_load_is_named(monkeypatch, tmp_path):
    missing = str(tmp_path / "libmecab-missing.so")
    monkeypatch.setattr(mecab_library, "bundled_library_path", lambda: missing)
    with pytest.raises(MecabUnavailableError) as error:
        InProcessMecab(["mecab"]).run(b"x\n")
    message = str(error.value)
    assert sys.platform in message and missing in message
    assert isinstance(error.value.__cause__, OSError)


# The executable and its options, without the --input-buffer-size the controller adds, so that
# the cases can set their own. The bundled executable, not one installed on the system, so that
# both sides are the same MeCab.
BASE_CMD = [get_bundled_executable("mecab")] + BasicMecabController._mecab_cmd[1:4]


def run_executable(cmd, data):
    env = dict(os.environ, LD_LIBRARY_PATH=SUPPORT_DIR, DYLD_LIBRARY_PATH=SUPPORT_DIR)
    try:
        return subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env).stdout
    except OSError as e:
        pytest.skip(f"the bundled executable cannot run here: {e}")


@pytest.fixture(scope="module")
def library_path():
    try:
        mecab = InProcessMecab(BASE_CMD)
        mecab.run(b"x\n")
    except MecabUnavailableError as e:
        pytest.skip(f"MeCab cannot run in process here: {e}")
    return mecab.library_path


SENTENCE = "昨日、林檎を2個買った。".encode()
LONG_LINE = "あ".encode() * 3000  # 9000 bytes, so a split falls inside a character


@pytest.mark.parametrize(
    "options, data",
    [
        pytest.param([], SENTENCE + b"\n", id="a sentence"),
        pytest.param([], b"", id="no input"),
        pytest.param([], b"\n", id="an empty line"),
        pytest.param([], b"a\n\nb\n", id="several lines"),
        pytest.param([], b"x\0y\nz", id="a NUL and no newline at the end"),
        pytest.param([], b"\0abc", id="a NUL first, at the end of the input"),
        pytest.param([], "日本語\r\n".encode(), id="a carriage return"),
        pytest.param(["-b8192"], b"x" * 8191 + b"\n", id="a line that fills the buffer"),
        pytest.param(["-b8192"], b"x" * 8192 + b"\n", id="a line one byte over"),
        pytest.param(["-b8192"], LONG_LINE + b"\n", id="a line split inside a character"),
        pytest.param(["-b8192"], b"x" * 20000, id="a line split twice, no newline"),
        pytest.param(["--input-buffer-size=10"], LONG_LINE + b"\n", id="a buffer below the minimum"),
        pytest.param(["--nbest=2"], SENTENCE + b"\n", id="n-best"),
        pytest.param(["--lattice-level=1"], SENTENCE + b"\n", id="the lattice-level warning"),
        pytest.param(["-Owakati"], SENTENCE + b"\n", id="an output format"),
        pytest.param(MecabController._mecab_args, SENTENCE + b"\n", id="the controller's formats"),
    ],
)
def test_in_process_is_the_executable(library_path, options, data):
    cmd = BASE_CMD + options
    assert InProcessMecab(cmd).run(data) == run_executable(cmd, data)


def test_an_error_is_reported_as_by_a_fresh_executable(library_path):
    """A field the format names but an unknown word lacks fails the parse; the second failure
    must not carry the first's message, as it would in a tagger that is reused."""
    cmd = BASE_CMD + ["--node-format=%m|%f[7]\\n"]
    mecab = InProcessMecab(cmd)
    expected = run_executable(cmd, b"xyz\n")
    assert b"out of range" in expected
    assert mecab.run(b"xyz\n") == expected
    assert mecab.run(b"xyz\n") == expected
    assert mecab.run(SENTENCE + b"\n") == run_executable(cmd, SENTENCE + b"\n")


def test_the_controller_is_the_same_either_way(library_path):
    cmd = BASE_CMD + BasicMecabController._mecab_cmd[4:]
    subprocess_mecab = BasicMecabController(mecab_cmd=cmd, mecab_args=MecabController._mecab_args, in_process=False)
    in_process_mecab = BasicMecabController(mecab_cmd=cmd, mecab_args=MecabController._mecab_args, in_process=True)
    for expr in ("カリン、自分でまいた種は自分で刈り取れ", "彼２０００万も使った。", "", "放っておいて\n二人の美人"):
        assert in_process_mecab.run(expr) == subprocess_mecab.run(expr)


def test_threads_share_one_tagger(library_path):
    mecab = InProcessMecab(BASE_CMD)
    lines = [f"{i}番目の文を解析します。".encode() + b"\n" for i in range(40)]
    expected = [mecab.run(line) for line in lines]
    results = {}

    def work(n):
        results[n] = [mecab.run(line) for line in lines]

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result == expected for result in results.values())


def test_android_loads_a_private_copy_named_by_its_content(library_path, monkeypatch, tmp_path):
    """The library ships where nothing may be mapped executable, so Android loads a copy."""
    shipped = tmp_path / "shared" / "libmecab-android-test.so"
    shipped.parent.mkdir()
    with open(library_path, "rb") as f:
        data = f.read()
    shipped.write_bytes(data)
    monkeypatch.setattr(mecab_library, "IS_ANDROID", True)
    monkeypatch.setattr(mecab_library, "bundled_library_path", lambda: str(shipped))
    # A TMPDIR that cannot hold a folder: the copy falls back to the one in HOME.
    (tmp_path / "not-a-folder").write_text("")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "not-a-folder"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    _, path = mecab_library.load_library()
    name = f"libmecab-{hashlib.sha256(data).hexdigest()[:16]}.so"
    assert path == str(tmp_path / "home" / ".cache" / "mecab_controller" / name)
    with open(path, "rb") as f:
        assert f.read() == data
    assert not stat.S_IMODE(os.stat(path).st_mode) & 0o222
    assert os.listdir(os.path.dirname(path)) == [name]

    copied = os.stat(path)
    assert mecab_library.load_library()[1] == path
    assert os.stat(path).st_ino == copied.st_ino, "an intact copy is reused"

    # What an interrupted copy of an older version could have left. A new file, not a write
    # into this one: the process has it mapped.
    os.remove(path)
    with open(path, "wb") as f:
        f.write(data[: len(data) // 2])
    mecab_library.load_library()
    with open(path, "rb") as f:
        assert f.read() == data, "a damaged copy is replaced"
