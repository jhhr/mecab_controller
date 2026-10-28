# Mecab controller

Mecab controller is a simple wrapper around
[mecab](https://github.com/taku910/mecab) ([AUR](https://aur.archlinux.org/packages/mecab-git)).
It was created primarily to be used in
[AJT Japanese](https://ankiweb.net/shared/info/1344485230),
an [Anki](https://wiki.archlinux.org/title/Anki) add-on
that generates furigana for Japanese text.
Originally based on code from
[Japanese support](https://github.com/ankitects/anki-addons/tree/main/code/japanese).

## Usage with AJT Japanese

This repository is already included with AJT Japanese.
You don't need to do anything extra.

## Standalone usage

```
>>> import mecab_controller
>>> mecab = mecab_controller.MecabController()
>>> print(mecab.reading('昨日すき焼きを食べました'))
昨日[きのう]すき 焼[や]きを 食[た]べました
```

```
python -m mecab_controller 昨日すき焼きを食べました
昨日[きのう]すき 焼[や]きを 食[た]べました
```

## How MeCab runs

By default each call starts the bundled `mecab` executable (`support/mecab.lin`, `mecab.mac`
or `mecab.exe`, unless one is installed on the system) and reads what it writes.

On Android it loads libmecab into the Python process through ctypes instead
(`mecab_library.py`), because an app there cannot start a program: the folder an add-on lives
in is usually on shared storage, which is mounted noexec. The library,
`support/libmecab-android-arm64-v8a.so`, is copied into the app's private cache folder
(`$TMPDIR`, else `~/.cache`) under a name made of its content hash, and loaded from there. The
dictionaries stay where they are. Kioku, which runs the Anki desktop app on Android, is where
this was needed.

Either way the output is the same, byte for byte: the in-process backend reads its input the
way the executable does, line by line into a buffer of `--input-buffer-size` bytes, and
`mecab_library_tests.py` compares the two wherever both can run.

To choose the backend:

- `MecabController(in_process=True)` or `in_process=False`; the default (`None`) is in process
  on Android only.
- The environment variable `MECAB_CONTROLLER_BACKEND=in_process` or `subprocess` sets that
  default without touching the code that makes the controller.

The bundled builds are not all one version: Linux (`libmecab.so.1`, `mecab.lin`) is MeCab
0.98, Windows (`libmecab.dll`, `mecab.exe`) 0.98pre3, macOS and Android 0.996. The Windows DLL
is 32-bit, so a 64-bit Python cannot load it in process. `support/build_libmecab_android.sh`
rebuilds the Android library from MeCab's source with the Android NDK; its header says how it
is built.
