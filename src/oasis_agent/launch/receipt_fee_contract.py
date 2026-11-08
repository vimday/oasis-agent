"""The receipt native-fee record contract, shared by capture and the read-only report.

Capture (spot_executor.receipt_fees) emits only records that satisfy these bounds; the
report (spot_report.receipt_fee_attribution) accepts only records that satisfy them, so
it can never count a value capture could not have produced. Pure constants and checks:
no I/O, and nothing here can submit, sign or broadcast.
"""

FEE_SOURCE = "eth_getTransactionReceipt_same_response"
GAS_USED_LIMIT = 1 << 64  # exclusive: gasUsed is a uint64
WEI_LIMIT = 1 << 256  # exclusive: prices, products and l1Fee are uint256


def execution_fee_in_bounds(gas: int, price: int, fee: int) -> bool:
    """The execution item capture can emit: in-width factors and their exact product."""
    return 0 <= gas < GAS_USED_LIMIT and 0 <= price < WEI_LIMIT and fee == gas * price < WEI_LIMIT


def l1_fee_in_bounds(fee: int) -> bool:
    return 0 <= fee < WEI_LIMIT
