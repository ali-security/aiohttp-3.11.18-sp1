import subprocess
import sys
import textwrap

import pytest

from aiohttp.compression_utils import (
    DEFAULT_MAX_DECOMPRESS_SIZE,
    HAS_BROTLI,
    BrotliDecompressor,
)

try:
    import brotli as _system_brotli
except ImportError:  # pragma: no cover
    _system_brotli = None  # type: ignore[assignment]


def _run_py(code: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )


def _ver(mod: object) -> "tuple[int, int]":
    parts = mod.__version__.split(".")[:2]  # type: ignore[attr-defined]
    return (int(parts[0]), int(parts[1]))


@pytest.mark.skipif(not HAS_BROTLI, reason="brotli is not installed")
def test_system_brotli_is_module_of_record() -> None:
    from aiohttp.compression_utils import _brotli

    assert _brotli is not None
    assert not _brotli.__name__.startswith("aiohttp._vendored")


@pytest.mark.skipif(not HAS_BROTLI, reason="brotli is not installed")
def test_brotli_decompressor_is_at_least_1_2() -> None:
    from aiohttp.compression_utils import _brotli_decompressor

    assert _brotli_decompressor is not None
    assert _ver(_brotli_decompressor) >= (1, 2), _brotli_decompressor.__version__


@pytest.mark.skipif(not HAS_BROTLI, reason="brotli is not installed")
def test_brotli_bomb_is_capped() -> None:
    from aiohttp.compression_utils import _brotli

    assert _brotli is not None
    original = b"A" * (64 * 2**20)
    compressed = _brotli.compress(original)
    assert len(compressed) < 2**16

    decompressor = BrotliDecompressor()
    out = decompressor.decompress_sync(
        compressed, max_length=DEFAULT_MAX_DECOMPRESS_SIZE + 1
    )
    assert len(out) > DEFAULT_MAX_DECOMPRESS_SIZE
    assert len(out) < len(original)


@pytest.mark.skipif(
    _system_brotli is None or _ver(_system_brotli) >= (1, 2),
    reason="requires an OLD (<1.2) system brotli to prove the vendored fallback",
)
def test_old_system_brotli_uses_vendored_decompressor() -> None:
    from aiohttp.compression_utils import _brotli, _brotli_decompressor

    assert _brotli is not None
    assert _brotli_decompressor is not None
    assert _system_brotli is not None

    assert _brotli is _system_brotli
    assert _brotli.__name__ == "brotli"
    assert _ver(_brotli) < (1, 2)

    assert _brotli_decompressor is not _system_brotli
    assert _brotli_decompressor.__name__.startswith("aiohttp._vendored")
    assert _ver(_brotli_decompressor) >= (1, 2)

    original = b"A" * (64 * 2**20)
    compressed = _brotli.compress(original)
    out = BrotliDecompressor().decompress_sync(
        compressed, max_length=DEFAULT_MAX_DECOMPRESS_SIZE + 1
    )
    assert len(out) > DEFAULT_MAX_DECOMPRESS_SIZE
    with pytest.raises(TypeError):
        _system_brotli.Decompressor().process(compressed, DEFAULT_MAX_DECOMPRESS_SIZE)


def test_coexistence_subprocess_no_segfault() -> None:
    result = _run_py("""
        import importlib.util, sys
        if importlib.util.find_spec("brotli") is None:
            print("SKIP: no system brotli"); sys.exit(0)
        import brotli
        sysver = brotli.__version__
        from aiohttp.compression_utils import (
            _brotli, _brotli_decompressor, HAS_BROTLI,
        )
        assert HAS_BROTLI is True
        assert _brotli is brotli
        assert "brotli" in sys.modules

        sysmm = tuple(int(p) for p in sysver.split(".")[:2])
        assert _brotli_decompressor is not None
        decmm = tuple(int(p) for p in _brotli_decompressor.__version__.split(".")[:2])
        assert decmm >= (1, 2)
        if sysmm >= (1, 2):
            assert _brotli_decompressor is brotli
        else:
            assert _brotli_decompressor is not brotli
            assert _brotli_decompressor.__name__.startswith("aiohttp._vendored")
            assert "aiohttp._vendored.brotli" in sys.modules

        data = brotli.compress(b"x" * 1000)
        assert brotli.decompress(data) == b"x" * 1000
        from aiohttp.compression_utils import BrotliDecompressor
        BrotliDecompressor()
        print("OK system=%s decompressor=%s" % (sysver, _brotli_decompressor.__version__))
        """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout or "SKIP" in result.stdout, result.stdout


def test_no_system_brotli_disables_br_subprocess() -> None:
    result = _run_py("""
        import sys, os
        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name in ("brotli", "brotlicffi"):
                    raise ImportError("blocked for test")
                return None
        sys.meta_path.insert(0, _Blocker())
        for m in [k for k in sys.modules if k.split(".")[0] in ("brotli", "brotlicffi")]:
            del sys.modules[m]

        from aiohttp.compression_utils import (
            HAS_BROTLI, _brotli, _brotli_decompressor,
        )
        assert HAS_BROTLI is False, "HAS_BROTLI should be False without system brotli"
        assert _brotli is None
        assert _brotli_decompressor is None

        import aiohttp
        vp = os.path.join(os.path.dirname(aiohttp.__file__), "_vendored", "brotli.py")
        assert os.path.exists(vp), "vendored brotli.py should still ship"

        from aiohttp.http_parser import DeflateBuffer
        from aiohttp.http_exceptions import ContentEncodingError
        import asyncio
        from aiohttp.streams import StreamReader
        from aiohttp.base_protocol import BaseProtocol
        async def _check():
            loop = asyncio.get_running_loop()
            sr = StreamReader(BaseProtocol(loop), 2**16, loop=loop)
            try:
                DeflateBuffer(sr, "br")
            except ContentEncodingError:
                return True
            return False
        assert asyncio.run(_check()) is True

        from aiohttp.client_reqrep import _gen_default_accept_encoding
        assert _gen_default_accept_encoding() == "gzip, deflate"
        print("OK br disabled, vendored still ships")
        """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout, result.stdout
