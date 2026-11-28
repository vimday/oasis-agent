// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface BaseQuoteUni {
    struct Params {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint24 fee;
        uint160 limit;
    }
    function quoteExactInputSingle(Params calldata params) external returns (uint256, uint160, uint32, uint256);
}

interface BaseQuoteSlip {
    struct Params {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        int24 spacing;
        uint160 limit;
    }
    function quoteExactInputSingle(Params calldata params) external returns (uint256, uint160, uint32, uint256);
}

interface BaseQuoteFeeOracle {
    function getL1FeeUpperBound(uint256 unsignedLength) external view returns (uint256);
    function getOperatorFee(uint256 gasLimit) external view returns (uint256);
}

interface BaseQuoteClock {
    function timestampMs() external view returns (uint256);
}

interface BaseQuoteFactory {
    function getPool(address token0, address token1, uint24 fee) external view returns (address);
}

interface BaseQuoteSlipFactory {
    function getPool(address token0, address token1, int24 spacing) external view returns (address);
}

/// @dev Read-only research fixture for a hash-pinned eth_call code override. The venue quoters
/// revert their inner pool swaps. This fixture holds no funds, approvals or write authority.
contract BaseBatchedQuoteProbe {
    // Six initial and up to 30 dependent reads leave headroom under the outer call limit.
    uint256 private constant LEG_GAS = 400_000;
    address private constant PROBE = address(uint160(0xa258));
    address private constant WETH = address(bytes20(hex"4200000000000000000000000000000000000006"));
    address private constant USDC = address(bytes20(hex"833589fcd6edb6e08f4c7c32d4f71b54bda02913"));
    address private constant UNI = address(bytes20(hex"3d4e44Eb1374240CE5F1B871ab261CD16335B76a"));
    address private constant AERO = address(bytes20(hex"254cf9e1e6e233aa1ac962cb9b05b2cfeaae15b0"));
    address private constant AERO_NEW = address(bytes20(hex"514c8b5f54112481e28028f1166bd78501089259"));
    address private constant UNI_FACTORY = address(bytes20(hex"33128a8fc17869897dce68ed026d694621f6fdfd"));
    address private constant AERO_FACTORY = address(bytes20(hex"5e7bb104d84c7cb9b682aac2f3d509f5f406809a"));
    address private constant AERO_NEW_FACTORY = address(bytes20(hex"f8f2eb4940cfe7d13603dddd87f123820fc061ef"));
    BaseQuoteFeeOracle private constant FEES =
        BaseQuoteFeeOracle(address(uint160(0x420000000000000000000000000000000000000F)));
    BaseQuoteClock private constant BASE_TIME =
        BaseQuoteClock(address(uint160(0x4200000000000000000000000000000000000030)));

    struct Venue {
        address quoter;
        address factory;
        uint24 parameter;
        bool slip;
    }

    struct Quote {
        uint256 intermediate;
        uint256 received;
    }

    struct Snapshot {
        uint256 blockNumber;
        uint256 timestampMs;
        uint256 operator;
        uint256[] l1;
        address[6] pools;
        bytes32[6] poolCode;
        Quote[] quotes;
    }

    function venues() private pure returns (Venue[6] memory) {
        return [
            Venue(UNI, UNI_FACTORY, 100, false),
            Venue(AERO, AERO_FACTORY, 100, true),
            Venue(UNI, UNI_FACTORY, 500, false),
            Venue(AERO, AERO_FACTORY, 1, true),
            Venue(AERO_NEW, AERO_NEW_FACTORY, 1, true),
            Venue(UNI, UNI_FACTORY, 3000, false)
        ];
    }

    function quoteBatch(uint256[] calldata amounts, uint256[] calldata unsignedLengths, bool baseline)
        external
        returns (Snapshot memory snapshot)
    {
        require(block.chainid == 8453 && address(this) == PROBE);
        require(amounts.length == 1 && amounts.length == unsignedLengths.length);
        Venue[6] memory configured = venues();
        for (uint256 i; i < configured.length; ++i) {
            Venue memory v = configured[i];
            address pool = v.slip
                ? BaseQuoteSlipFactory(v.factory).getPool(WETH, USDC, int24(v.parameter))
                : BaseQuoteFactory(v.factory).getPool(WETH, USDC, v.parameter);
            require(pool.code.length != 0);
            snapshot.pools[i] = pool;
            snapshot.poolCode[i] = pool.codehash;
        }

        snapshot.blockNumber = block.number;
        snapshot.timestampMs = BASE_TIME.timestampMs();
        snapshot.operator = FEES.getOperatorFee(550_000);
        snapshot.l1 = new uint256[](amounts.length);
        snapshot.quotes = new Quote[](amounts.length * (baseline ? 1 : 30));
        uint256 next;
        for (uint256 lot; lot < amounts.length; ++lot) {
            require(amounts[lot] > 0 && amounts[lot] <= 1 ether && unsignedLengths[lot] <= 512);
            snapshot.l1[lot] = FEES.getL1FeeUpperBound(unsignedLengths[lot]);
            uint256 firstCount = baseline ? 1 : configured.length;
            for (uint256 i; i < firstCount; ++i) {
                uint256 middle = leg(configured[i], WETH, USDC, amounts[lot]);
                for (uint256 j = baseline ? 1 : 0; j < (baseline ? 2 : configured.length); ++j) {
                    if (i == j) continue;
                    uint256 received = middle == 0 ? 0 : leg(configured[j], USDC, WETH, middle);
                    snapshot.quotes[next++] = Quote(middle, received);
                }
            }
        }
    }

    function leg(Venue memory v, address tokenIn, address tokenOut, uint256 amount) private returns (uint256) {
        if (v.slip) {
            try BaseQuoteSlip(v.quoter).quoteExactInputSingle{gas: LEG_GAS}(
                BaseQuoteSlip.Params(tokenIn, tokenOut, amount, int24(v.parameter), 0)
            ) returns (
                uint256 out, uint160, uint32, uint256
            ) {
                return out;
            } catch {
                return 0;
            }
        }
        try BaseQuoteUni(v.quoter).quoteExactInputSingle{gas: LEG_GAS}(
            BaseQuoteUni.Params(tokenIn, tokenOut, amount, v.parameter, 0)
        ) returns (
            uint256 out, uint160, uint32, uint256
        ) {
            return out;
        } catch {
            return 0;
        }
    }
}
