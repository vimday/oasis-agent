"""The receipt native-fee record contract, shared by capture and every read-only reader.

Capture (spot_executor.receipt_fees) emits only records that satisfy these bounds, and
`read_fee_record` is the ONE reading of a journaled record that both the fee report
(spot_report.receipt_fee_attribution) and the cost replay (spot_cost_binding) use, so
no consumer can rank a record higher than another: a value capture could not have
produced is never counted, never compared and never pins a block. Pure constants and
checks: no I/O, and nothing here can submit, sign or broadcast.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

FEE_SOURCE = "eth_getTransactionReceipt_same_response"
GAS_USED_LIMIT = 1 << 64  # exclusive: gasUsed is a uint64
WEI_LIMIT = 1 << 256  # exclusive: prices, products and l1Fee are uint256
# Item states. not_captured: the reader stated nothing. missing/invalid: the receipt
# itself lacked or malformed a field. record_invalid: the journal line is not something
# capture could have written (other source or status, width, product, shape).
ITEM_STATES = ("observed", "missing", "invalid", "not_captured", "record_invalid")
_WEI = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_HASH = re.compile(r"^0x[0-9a-f]{64}$")


def execution_fee_in_bounds(gas: int, price: int, fee: int) -> bool:
    """The execution item capture can emit: in-width factors and their exact product."""
    return 0 <= gas < GAS_USED_LIMIT and 0 <= price < WEI_LIMIT and fee == gas * price < WEI_LIMIT


def l1_fee_in_bounds(fee: int) -> bool:
    return 0 <= fee < WEI_LIMIT


@dataclass(frozen=True, slots=True)
class FeeItem:
    state: str
    wei: int | None  # set only when state == "observed"


@dataclass(frozen=True, slots=True)
class FeeReading:
    execution: FeeItem
    l1: FeeItem
    # The receipt's own block hash, only from a record capture could have written and
    # only as a canonical observed hash; None for every other case, including legacy
    # height-only fills and a not_captured record that happens to carry one.
    block_hash: str | None


NOT_CAPTURED = FeeItem("not_captured", None)
RECORD_INVALID = FeeItem("record_invalid", None)


def _wei(value: object) -> int | None:
    return int(value) if isinstance(value, str) and _WEI.fullmatch(value) else None


def _item(entry: object, observed: Callable[[dict[str, object]], int | None]) -> FeeItem:
    if not isinstance(entry, dict) or entry.get("status") not in ("observed", "missing", "invalid"):
        return RECORD_INVALID
    if entry["status"] != "observed":
        return FeeItem(entry["status"], None)
    value = observed(entry)
    return RECORD_INVALID if value is None else FeeItem("observed", value)


def _execution(entry: dict[str, object]) -> int | None:
    gas, price = _wei(entry.get("gas_used")), _wei(entry.get("effective_gas_price_wei"))
    fee = _wei(entry.get("fee_wei"))
    if gas is None or price is None or fee is None or not execution_fee_in_bounds(gas, price, fee):
        return None
    return fee


def _l1(entry: dict[str, object]) -> int | None:
    fee = _wei(entry.get("fee_wei"))
    return fee if fee is not None and l1_fee_in_bounds(fee) else None


def _block(entry: object) -> str | None:
    if not isinstance(entry, dict) or entry.get("status") != "observed":
        return None
    value = entry.get("block_hash")
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def read_fee_record(fees: object) -> FeeReading:
    """The single reading of one journaled `receipt_fees` value.

    Absent or not_captured: both items not_captured, no block. Any other status or
    source, or a non-object: record_invalid, no block. A captured record: each item on
    its own terms (a bad execution item leaves a good L1 item observed), and the block
    hash only if it is a canonical observed hash.
    """
    if fees is None or (isinstance(fees, dict) and fees.get("status") == "not_captured"):
        return FeeReading(NOT_CAPTURED, NOT_CAPTURED, None)
    if (
        not isinstance(fees, dict)
        or fees.get("status") != "captured"
        or fees.get("source") != FEE_SOURCE
    ):
        return FeeReading(RECORD_INVALID, RECORD_INVALID, None)
    return FeeReading(
        _item(fees.get("execution"), _execution),
        _item(fees.get("l1_data"), _l1),
        _block(fees.get("receipt_block")),
    )
