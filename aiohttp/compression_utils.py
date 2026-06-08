import asyncio
import importlib
import importlib.util
import platform
import zlib
from concurrent.futures import Executor
from types import ModuleType
from typing import Optional, cast


def _import_system_brotli() -> Optional[ModuleType]:
    for name in ("brotlicffi", "brotli"):
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except (ImportError, ValueError):  # pragma: no cover
            continue
        try:
            return importlib.import_module(name)
        except Exception:  # pragma: no cover
            return None
    return None


def _brotli_has_max_length_cap(mod: ModuleType) -> bool:
    try:
        return tuple(int(p) for p in mod.__version__.split(".")[:2]) >= (1, 2)
    except Exception:  # pragma: no cover
        return False


def _import_vendored_brotli() -> ModuleType:
    if platform.python_implementation() == "CPython":
        from ._vendored import brotli

        mod: ModuleType = brotli
    else:
        from ._vendored import brotlicffi

        mod = brotlicffi
    return mod


# CVE-2025-69223: brotli decompression must be able to cap its output size.
# That capability only exists in Brotli / brotlicffi >= 1.2. If the system has
# brotli installed but it predates the cap, fall back to the bundled copy under
# ``aiohttp._vendored`` so the limit can be enforced without forcing users to
# bump their declared ``Brotli`` / ``brotlicffi`` requirement.
_brotli: Optional[ModuleType] = _import_system_brotli()
HAS_BROTLI = _brotli is not None

if _brotli is not None and not _brotli_has_max_length_cap(_brotli):
    _brotli_decompressor: Optional[ModuleType] = _import_vendored_brotli()
else:
    _brotli_decompressor = _brotli


MAX_SYNC_CHUNK_SIZE = 1024
DEFAULT_MAX_DECOMPRESS_SIZE = 2**25  # 32MiB


def encoding_to_mode(
    encoding: Optional[str] = None,
    suppress_deflate_header: bool = False,
) -> int:
    if encoding == "gzip":
        return 16 + zlib.MAX_WBITS

    return -zlib.MAX_WBITS if suppress_deflate_header else zlib.MAX_WBITS


class ZlibBaseHandler:
    def __init__(
        self,
        mode: int,
        executor: Optional[Executor] = None,
        max_sync_chunk_size: Optional[int] = MAX_SYNC_CHUNK_SIZE,
    ):
        self._mode = mode
        self._executor = executor
        self._max_sync_chunk_size = max_sync_chunk_size


class ZLibCompressor(ZlibBaseHandler):
    def __init__(
        self,
        encoding: Optional[str] = None,
        suppress_deflate_header: bool = False,
        level: Optional[int] = None,
        wbits: Optional[int] = None,
        strategy: int = zlib.Z_DEFAULT_STRATEGY,
        executor: Optional[Executor] = None,
        max_sync_chunk_size: Optional[int] = MAX_SYNC_CHUNK_SIZE,
    ):
        super().__init__(
            mode=(
                encoding_to_mode(encoding, suppress_deflate_header)
                if wbits is None
                else wbits
            ),
            executor=executor,
            max_sync_chunk_size=max_sync_chunk_size,
        )
        if level is None:
            self._compressor = zlib.compressobj(wbits=self._mode, strategy=strategy)
        else:
            self._compressor = zlib.compressobj(
                wbits=self._mode, strategy=strategy, level=level
            )
        self._compress_lock = asyncio.Lock()

    def compress_sync(self, data: bytes) -> bytes:
        return self._compressor.compress(data)

    async def compress(self, data: bytes) -> bytes:
        """Compress the data and returned the compressed bytes.

        Note that flush() must be called after the last call to compress()

        If the data size is large than the max_sync_chunk_size, the compression
        will be done in the executor. Otherwise, the compression will be done
        in the event loop.
        """
        async with self._compress_lock:
            # To ensure the stream is consistent in the event
            # there are multiple writers, we need to lock
            # the compressor so that only one writer can
            # compress at a time.
            if (
                self._max_sync_chunk_size is not None
                and len(data) > self._max_sync_chunk_size
            ):
                return await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._compressor.compress, data
                )
            return self.compress_sync(data)

    def flush(self, mode: int = zlib.Z_FINISH) -> bytes:
        return self._compressor.flush(mode)


class ZLibDecompressor(ZlibBaseHandler):
    def __init__(
        self,
        encoding: Optional[str] = None,
        suppress_deflate_header: bool = False,
        executor: Optional[Executor] = None,
        max_sync_chunk_size: Optional[int] = MAX_SYNC_CHUNK_SIZE,
    ):
        super().__init__(
            mode=encoding_to_mode(encoding, suppress_deflate_header),
            executor=executor,
            max_sync_chunk_size=max_sync_chunk_size,
        )
        self._decompressor = zlib.decompressobj(wbits=self._mode)

    def decompress_sync(self, data: bytes, max_length: int = 0) -> bytes:
        return self._decompressor.decompress(data, max_length)

    async def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        """Decompress the data and return the decompressed bytes.

        If the data size is large than the max_sync_chunk_size, the decompression
        will be done in the executor. Otherwise, the decompression will be done
        in the event loop.
        """
        if (
            self._max_sync_chunk_size is not None
            and len(data) > self._max_sync_chunk_size
        ):
            return await asyncio.get_running_loop().run_in_executor(
                self._executor, self._decompressor.decompress, data, max_length
            )
        return self.decompress_sync(data, max_length)

    def flush(self, length: int = 0) -> bytes:
        return (
            self._decompressor.flush(length)
            if length > 0
            else self._decompressor.flush()
        )

    @property
    def eof(self) -> bool:
        return self._decompressor.eof

    @property
    def unconsumed_tail(self) -> bytes:
        return self._decompressor.unconsumed_tail

    @property
    def unused_data(self) -> bytes:
        return self._decompressor.unused_data


class BrotliDecompressor:
    # Supports both 'brotlicffi' and 'Brotli' packages
    # since they share an import name. The top branches
    # are for 'brotlicffi' and bottom branches for 'Brotli'.
    def __init__(self) -> None:
        if not HAS_BROTLI or _brotli_decompressor is None:
            raise RuntimeError(
                "The brotli decompression is not available. "
                "Please install `Brotli` module"
            )
        self._obj = _brotli_decompressor.Decompressor()

    def decompress_sync(self, data: bytes, max_length: int = 0) -> bytes:
        # CVE-2025-69223: ``max_length`` caps the decompressed output. It is
        # honoured by Brotli / brotlicffi >= 1.2 (or the vendored fallback);
        # ``0`` means unlimited, matching the zlib convention.
        if hasattr(self._obj, "decompress"):
            return cast(bytes, self._obj.decompress(data, max_length))
        return cast(bytes, self._obj.process(data, max_length))

    def flush(self) -> bytes:
        if hasattr(self._obj, "flush"):
            return cast(bytes, self._obj.flush())
        return b""
