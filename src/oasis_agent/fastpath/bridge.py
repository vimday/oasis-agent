import ctypes
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from oasis_agent.errors import OasisError

LIB_ENV_VAR: Final[str] = "OASIS_CORE_LIB_PATH"
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


class FastpathUnavailableError(OasisError):
    """Raised when the native oasis-core shared library cannot be found or loaded."""


class FastpathCallError(OasisError):
    """Raised when the native oasis-core FFI boundary returns an error or invalid payload."""


def _default_lib_filename() -> str:
    if sys.platform == "darwin":
        return "liboasis_core.dylib"
    if sys.platform == "win32":
        return "oasis_core.dll"
    return "liboasis_core.so"


def resolve_lib_path(explicit_path: Path | None = None) -> Path:
    """Resolve the oasis-core shared library path from argument, env, or cargo target dirs."""
    if explicit_path is not None:
        if not explicit_path.is_file():
            raise FastpathUnavailableError(f"native library not found at {explicit_path}")
        return explicit_path

    env_override = os.environ.get(LIB_ENV_VAR)
    if env_override:
        candidate = Path(env_override).expanduser()
        if not candidate.is_file():
            raise FastpathUnavailableError(f"{LIB_ENV_VAR} points to missing library: {candidate}")
        return candidate

    lib_name = _default_lib_filename()
    release_path = _REPO_ROOT / "crates" / "target" / "release" / lib_name
    if release_path.is_file():
        return release_path
    debug_path = _REPO_ROOT / "crates" / "target" / "debug" / lib_name
    if debug_path.is_file():
        return debug_path

    raise FastpathUnavailableError(
        f"native oasis-core library not built; expected {release_path} or {debug_path}"
    )


def require_int(value: int, name: str, *, non_negative: bool = False) -> int:
    """Validate that a value is a true Python int (never bool or float)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if non_negative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def parse_int_str(value: object, name: str) -> int:
    """Parse an integer encoded strictly as a base-10 string in FFI responses."""
    if not isinstance(value, str):
        raise FastpathCallError(f"expected integer string for {name}, got {type(value).__name__}")
    try:
        return int(value, 10)
    except ValueError as exc:
        raise FastpathCallError(f"invalid integer string for {name}: {value!r}") from exc


class FastpathBridge:
    """Thread-safe ctypes JSON FFI bridge to the Rust `oasis_core` shared library."""

    def __init__(self, lib_path: Path | None = None) -> None:
        self._lib_path = resolve_lib_path(lib_path)
        try:
            cdll = ctypes.CDLL(str(self._lib_path))
        except OSError as exc:
            raise FastpathUnavailableError(
                f"failed to load native library {self._lib_path}: {exc}"
            ) from exc

        try:
            eval_fn = cdll.oasis_core_eval_json
            free_fn = cdll.oasis_core_free_str
        except AttributeError as exc:
            raise FastpathUnavailableError(
                f"native library {self._lib_path} is missing expected FFI exports: {exc}"
            ) from exc

        eval_fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        eval_fn.restype = ctypes.c_void_p
        free_fn.argtypes = [ctypes.c_void_p]
        free_fn.restype = None

        self._cdll = cdll
        self._eval_fn = eval_fn
        self._free_fn = free_fn
        self._v4_bitmap_steps_confirmed = False

    @property
    def lib_path(self) -> Path:
        return self._lib_path

    def require_v4_bitmap_steps(self) -> None:
        if not self._v4_bitmap_steps_confirmed:
            caps = self.eval_json({"cmd": "graph_session_capabilities"}).get("capabilities")
            if not isinstance(caps, list) or "v4_bitmap_steps" not in caps:
                raise FastpathCallError("native graph lacks V4 bitmap-step support")
            self._v4_bitmap_steps_confirmed = True

    def eval_json(self, command: Mapping[str, object]) -> dict[str, Any]:
        """Send one JSON command to `oasis_core_eval_json` and unwrap the response."""
        try:
            payload = json.dumps(command, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise FastpathCallError(f"invalid FFI request payload: {exc}") from exc

        ptr: int | None = self._eval_fn(payload, len(payload))
        if not ptr:
            raise FastpathCallError("oasis_core_eval_json returned null pointer")

        try:
            raw_bytes = ctypes.cast(ptr, ctypes.c_char_p).value
        finally:
            self._free_fn(ptr)

        if raw_bytes is None:
            raise FastpathCallError("oasis_core_eval_json returned empty C string")

        try:
            decoded: object = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FastpathCallError(f"invalid JSON from oasis_core_eval_json: {exc}") from exc

        if not isinstance(decoded, dict):
            raise FastpathCallError("FFI response must be a JSON object")
        if decoded.get("ok") is not True:
            raise FastpathCallError(str(decoded.get("error", "unknown fastpath error")))

        result = decoded.get("result")
        if not isinstance(result, dict):
            raise FastpathCallError("FFI response result must be a JSON object")
        return result

    def ping(self) -> dict[str, str]:
        result = self.eval_json({"cmd": "ping"})
        return {str(k): str(v) for k, v in result.items()}

    def sqrt_price_at_tick(self, tick: int) -> int:
        checked_tick = require_int(tick, "tick")
        result = self.eval_json({"cmd": "sqrt_price_at_tick", "tick": checked_tick})
        return parse_int_str(result.get("sqrt_price_x96"), "sqrt_price_x96")

    def amount_deltas(
        self,
        sqrt_price_a_x96: int,
        sqrt_price_b_x96: int,
        liquidity: int,
        *,
        round_up: bool,
    ) -> tuple[int, int]:
        a = require_int(sqrt_price_a_x96, "sqrt_price_a_x96", non_negative=True)
        b = require_int(sqrt_price_b_x96, "sqrt_price_b_x96", non_negative=True)
        liq = require_int(liquidity, "liquidity", non_negative=True)
        if not isinstance(round_up, bool):
            raise TypeError("round_up must be a boolean")
        result = self.eval_json(
            {
                "cmd": "amount_deltas",
                "sqrt_price_a_x96": str(a),
                "sqrt_price_b_x96": str(b),
                "liquidity": str(liq),
                "round_up": round_up,
            }
        )
        return (
            parse_int_str(result.get("amount0"), "amount0"),
            parse_int_str(result.get("amount1"), "amount1"),
        )

    def amount0_delta(
        self,
        sqrt_price_a_x96: int,
        sqrt_price_b_x96: int,
        liquidity: int,
        *,
        round_up: bool,
    ) -> int:
        amount0, _ = self.amount_deltas(
            sqrt_price_a_x96,
            sqrt_price_b_x96,
            liquidity,
            round_up=round_up,
        )
        return amount0

    def amount1_delta(
        self,
        sqrt_price_a_x96: int,
        sqrt_price_b_x96: int,
        liquidity: int,
        *,
        round_up: bool,
    ) -> int:
        _, amount1 = self.amount_deltas(
            sqrt_price_a_x96,
            sqrt_price_b_x96,
            liquidity,
            round_up=round_up,
        )
        return amount1

    def amounts_for_liquidity(
        self,
        sqrt_price_x96: int,
        sqrt_price_lower_x96: int,
        sqrt_price_upper_x96: int,
        liquidity: int,
        *,
        round_up: bool = False,
    ) -> tuple[int, int]:
        p = require_int(sqrt_price_x96, "sqrt_price_x96", non_negative=True)
        lo = require_int(sqrt_price_lower_x96, "sqrt_price_lower_x96", non_negative=True)
        hi = require_int(sqrt_price_upper_x96, "sqrt_price_upper_x96", non_negative=True)
        liq = require_int(liquidity, "liquidity", non_negative=True)
        if not isinstance(round_up, bool):
            raise TypeError("round_up must be a boolean")
        result = self.eval_json(
            {
                "cmd": "amounts_for_liquidity",
                "sqrt_price_x96": str(p),
                "sqrt_price_lower_x96": str(lo),
                "sqrt_price_upper_x96": str(hi),
                "liquidity": str(liq),
                "round_up": round_up,
            }
        )
        return (
            parse_int_str(result.get("amount0"), "amount0"),
            parse_int_str(result.get("amount1"), "amount1"),
        )

    def read_aleph_bbo(
        self,
        path: Path,
        num_symbols: int,
        symbol_id: int,
        exchange_id: int,
    ) -> dict[str, Any]:
        return self.eval_json(
            {
                "cmd": "read_aleph_bbo",
                "path": str(path),
                "num_symbols": require_int(num_symbols, "num_symbols", non_negative=True),
                "symbol_id": require_int(symbol_id, "symbol_id", non_negative=True),
                "exchange_id": require_int(exchange_id, "exchange_id", non_negative=True),
            }
        )

    def write_synthetic_aleph_shm(
        self,
        path: Path,
        num_symbols: int,
        updates: Sequence[Mapping[str, object]],
    ) -> int:
        result = self.eval_json(
            {
                "cmd": "write_synthetic_aleph_shm",
                "path": str(path),
                "num_symbols": require_int(num_symbols, "num_symbols", non_negative=True),
                "updates": list(updates),
            }
        )
        written = result.get("written")
        if isinstance(written, bool) or not isinstance(written, int):
            raise FastpathCallError("invalid written count from write_synthetic_aleph_shm")
        return written

    def audit_ring_init(self, path: Path) -> dict[str, Any]:
        return self.eval_json({"cmd": "audit_ring_init", "path": str(path)})
