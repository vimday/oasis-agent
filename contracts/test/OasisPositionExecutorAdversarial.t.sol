// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Vm } from "forge-std/Vm.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";
import { DeployPermit2 } from "permit2/test/utils/DeployPermit2.sol";
import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IERC20Minimal } from "@uniswap/v4-core/src/interfaces/external/IERC20Minimal.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { CustomRevert } from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { TickMath } from "@uniswap/v4-core/src/libraries/TickMath.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { Currency, CurrencyLibrary } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IPositionDescriptor } from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import { IWETH9 } from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";
import { Actions } from "@uniswap/v4-periphery/src/libraries/Actions.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { OasisPositionExecutor } from "../src/OasisPositionExecutor.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @notice Minimal ERC-20 whose transfer behavior can be adversarially configured:
/// outbound fee-on-transfer, hard transfer revert (paused/blacklisted style), and a
/// reentrancy callback that fires on every token movement. The public state
/// variables below implement the full ERC-20 metadata surface, but the contract
/// intentionally inherits only IERC20Minimal: inheriting the full IERC20 interface
/// as well would redeclare its Transfer/Approval events, which Solidity rejects.
/// forge-lint: disable-next-line(missing-inheritance)
contract MaliciousToken is IERC20Minimal {
    string public name = "Malicious";
    string public symbol = "MAL";
    uint8 public immutable decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    bool public outboundFeeEnabled;
    uint256 public feeBps;
    bool public transfersRevert;
    address public reentrancyTarget;
    bytes public reentrancyCalldata;
    bool public reentrancyArmed;
    bool public lastReentrancyReverted;

    constructor(uint256 supply, uint8 decimals_) {
        decimals = decimals_;
        totalSupply = supply;
        balanceOf[msg.sender] = supply;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    /// @notice Outbound transfer (used by PoolManager.take) applies the configured fee.
    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount, true);
        return true;
    }

    /// @notice Inbound settle (used by Permit2) is fee-free so pool deposits book exactly.
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ALLOWANCE");
        unchecked {
            allowance[from][msg.sender] = allowed - amount;
        }
        _move(from, to, amount, false);
        return true;
    }

    function _move(address from, address to, uint256 amount, bool outbound) internal {
        uint256 fee = outbound && outboundFeeEnabled ? (amount * feeBps) / 10_000 : 0;
        require(balanceOf[from] >= amount, "BALANCE");
        unchecked {
            balanceOf[from] -= amount;
        }
        uint256 received = amount - fee;
        unchecked {
            balanceOf[to] += received;
            if (fee > 0) totalSupply -= fee;
        }
        if (transfersRevert) revert("TRANSFERS_REVERTED");
        if (reentrancyArmed && reentrancyTarget != address(0)) {
            (bool ok,) = reentrancyTarget.call(reentrancyCalldata);
            lastReentrancyReverted = !ok;
        }
    }

    function setOutboundFee(bool enabled, uint256 bps) external {
        outboundFeeEnabled = enabled;
        feeBps = bps;
    }

    function setTransfersRevert(bool enabled) external {
        transfersRevert = enabled;
    }

    /// @notice Arm a reentrancy callback for the next token movement; the test
    /// helper deliberately accepts any target so the unarmed/zero path is covered.
    /// forge-lint: disable-next-line(missing-zero-check)
    function armReentrancy(address target, bytes calldata data) external {
        reentrancyTarget = target;
        reentrancyCalldata = data;
        reentrancyArmed = true;
    }

    function disarmReentrancy() external {
        reentrancyArmed = false;
    }
}

/// @notice Executor × real Permit2 runtime adversarial coverage (audit M4/M5):
/// malicious token categories (fee-on-transfer / transfer-revert / reentrant),
/// boundary tick validation, non-empty hookData, and atomic fail-closed rollback.
/// @dev Fully offline: fresh PoolManager + real Permit2 runtime via DeployPermit2.
contract OasisPositionExecutorAdversarialTest is Test {
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;
    uint256 private constant LIQUIDITY = 1 ether;
    uint64 private constant NONCE = 1;
    uint64 private constant DEADLINE_OFFSET = 60;
    uint24 private constant FEE_BPS = 1_000; // 10% outbound fee

    PoolManager private poolManager;
    IAllowanceTransfer private permit2;
    PositionManager private positionManager;
    OasisDynamicFeeHook private hook;
    OasisPositionExecutor private executor;
    PoolKey private key;
    MaliciousToken private mal;
    TestERC20 private reg;
    bool private malIsCurrency0;

    function setUp() public {
        poolManager = new PoolManager(address(this));
        permit2 = IAllowanceTransfer(new DeployPermit2().deployPermit2());
        positionManager = new PositionManager(
            poolManager, permit2, 100_000, IPositionDescriptor(address(0)), IWETH9(address(0))
        );

        OasisHookFactory factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            poolManager,
            IOasisPositionManager(address(positionManager)),
            address(this),
            address(this),
            GUARDIAN
        );
        hook = factory.deploy(
            _mineSalt(factory, codeHash),
            poolManager,
            IOasisPositionManager(address(positionManager)),
            address(this),
            address(this),
            GUARDIAN
        );
        executor = new OasisPositionExecutor(
            poolManager, positionManager, LP_OWNER, GUARDIAN, type(uint256).max
        );

        mal = new MaliciousToken(type(uint128).max, 18);
        TestERC20 regular = new TestERC20(type(uint128).max);
        malIsCurrency0 = address(mal) < address(regular);
        reg = regular;
        key = PoolKey({
            currency0: malIsCurrency0 ? Currency.wrap(address(mal)) : Currency.wrap(address(reg)),
            currency1: malIsCurrency0 ? Currency.wrap(address(reg)) : Currency.wrap(address(mal)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        hook.configurePool(key, _config());
        int24 initializedTick = poolManager.initialize(key, uint160(1 << 96));
        assertEq(initializedTick, 0);

        _approvePayer(address(mal));
        _approvePayer(address(reg));
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(1), LP_OWNER);
        vm.prank(LP_OWNER);
        positionManager.setApprovalForAll(address(executor), true);
    }

    function test_recenterSucceedsWithNonEmptyHookData() public {
        bytes memory hookData = hex"deadbeef00112233445566778899aabbccddeeff";
        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -60, 180, 0.1 ether);
        intent.hookData = hookData;
        vm.prank(LP_OWNER);
        executor.execute(intent);

        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(positionManager.ownerOf(2), LP_OWNER);
        assertEq(executor.lastNonce(), NONCE);
        assertTrue(executor.executedIntents(NONCE) != bytes32(0));
        assertEq(executor.history(0), executor.executedIntents(NONCE));
    }

    function test_recenterRejectsBoundaryTickViolations() public {
        // Misordered range.
        _expectInvalidRange(120, -120);
        // Unaligned for tickSpacing 60.
        _expectInvalidRange(-130, 120);
        // MIN_TICK is not aligned to 60.
        _expectInvalidRange(TickMath.MIN_TICK, TickMath.MAX_TICK);
        // Aligned but strictly above MAX_TICK.
        _expectInvalidRange(-887_220, 887_280);
        // Bounded executor enforces the liquidity ceiling.
        OasisPositionExecutor bounded =
            new OasisPositionExecutor(poolManager, positionManager, LP_OWNER, GUARDIAN, 1_000);
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisPositionExecutor.LiquidityAboveMax.selector, uint256(1_001), uint256(1_000)
            )
        );
        bounded.execute(_intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -60, 60, 1_001));
    }

    function test_removeWithOutboundFeeTokenCompletesWithExactDeltas() public {
        mal.setOutboundFee(true, FEE_BPS);
        uint256 ownerBefore0 = mal.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = reg.balanceOf(LP_OWNER);
        uint256 coreBefore0 = mal.balanceOf(address(poolManager));
        uint256 coreBefore1 = reg.balanceOf(address(poolManager));

        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(executor.lastNonce(), NONCE);
        // The executor holds nothing between transactions.
        assertEq(mal.balanceOf(address(executor)), 0);
        assertEq(reg.balanceOf(address(executor)), 0);
        // The full recorded delta leaves the pool manager; the outbound fee is
        // borne by the LP recipient, never minted into the pool.
        uint256 deltaMal = coreBefore0 - mal.balanceOf(address(poolManager));
        uint256 deltaReg = coreBefore1 - reg.balanceOf(address(poolManager));
        assertEq(reg.balanceOf(LP_OWNER) - ownerBefore1, deltaReg);
        assertEq(mal.balanceOf(LP_OWNER) - ownerBefore0, deltaMal - (deltaMal * FEE_BPS) / 10_000);
    }

    function test_removeWithTransferRevertingTokenFailsClosedAtomically() public {
        uint256 ownerBefore0 = mal.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = reg.balanceOf(LP_OWNER);
        uint256 coreBefore0 = mal.balanceOf(address(poolManager));
        uint256 coreBefore1 = reg.balanceOf(address(poolManager));
        mal.setTransfersRevert(true);

        // The pinned v4-core runtime wraps token transfer failures in an ERC-7751
        // WrappedError carrying the raw Error(string) from the token. The details
        // field is a dynamic bytes holding the additional-context selector.
        bytes memory innerRevert = abi.encodeWithSignature("Error(string)", "TRANSFERS_REVERTED");
        bytes memory details = abi.encodePacked(CurrencyLibrary.ERC20TransferFailed.selector);
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(mal),
                IERC20Minimal.transfer.selector,
                innerRevert,
                details
            )
        );
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        // Atomic rollback: position intact, no audit trail, no balance moved.
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);
        assertEq(positionManager.ownerOf(1), LP_OWNER);
        assertEq(executor.lastNonce(), 0);
        assertEq(executor.executedIntents(NONCE), bytes32(0));
        assertEq(mal.balanceOf(LP_OWNER), ownerBefore0);
        assertEq(reg.balanceOf(LP_OWNER), ownerBefore1);
        assertEq(mal.balanceOf(address(poolManager)), coreBefore0);
        assertEq(reg.balanceOf(address(poolManager)), coreBefore1);
    }

    function test_reentrantTokenCannotTriggerSecondExecution() public {
        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0);
        mal.armReentrancy(
            address(executor), abi.encodeWithSelector(executor.execute.selector, intent)
        );
        vm.recordLogs();
        vm.prank(LP_OWNER);
        executor.execute(intent);

        // The token's reentrant call was rejected (onlyOwner) and recorded.
        assertTrue(mal.lastReentrancyReverted());
        // Exactly one economic execution remains in the audit trail: the reentrant
        // attempt reverted before emitting any second IntentExecuted event.
        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(executor.lastNonce(), NONCE);
        assertEq(executor.history(0), executor.executedIntents(NONCE));
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256(
            "IntentExecuted(uint64,bytes32,uint8,uint256,uint256,int24,int24,uint256,address)"
        );
        uint256 executed = 0;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics.length >= 1 && logs[i].topics[0] == signature) {
                ++executed;
            }
        }
        assertEq(executed, 1);
    }

    function _expectInvalidRange(int24 lower, int24 upper) private {
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.InvalidRange.selector, lower, upper)
        );
        executor.execute(
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, lower, upper, 0.1 ether)
        );
    }

    function _approvePayer(address token) private {
        // Payer is this test contract; approve Permit2 and the PositionManager.
        assertTrue(IERC20Approval(token).approve(address(permit2), type(uint256).max));
        // A unix timestamp plus a day fits comfortably in uint48 for centuries to come.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint48 deadline = uint48(block.timestamp + 1 days);
        permit2.approve(token, address(positionManager), type(uint160).max, deadline);
    }

    function _intent(
        OasisPositionExecutor.Mode mode,
        uint256 oldTokenId,
        uint64 nonce,
        int24 tickLower,
        int24 tickUpper,
        uint256 liquidity
    ) private view returns (OasisPositionExecutor.Intent memory) {
        return OasisPositionExecutor.Intent({
            nonce: nonce,
            // A unix timestamp plus a minute fits comfortably in uint64 for centuries to come.
            // forge-lint: disable-next-line(unsafe-typecast)
            deadline: uint64(block.timestamp + DEADLINE_OFFSET),
            mode: mode,
            poolKey: key,
            oldTokenId: oldTokenId,
            tickLower: tickLower,
            tickUpper: tickUpper,
            liquidity: liquidity,
            amount0Min: 0,
            amount1Min: 0,
            amount0Max: type(uint128).max,
            amount1Max: type(uint128).max,
            maxSwapInput: 0,
            hookData: bytes("")
        });
    }

    function _mintPlan(address owner) private view returns (bytes memory) {
        bytes memory actions = abi.encodePacked(
            _action(Actions.MINT_POSITION),
            _action(Actions.CLOSE_CURRENCY),
            _action(Actions.CLOSE_CURRENCY)
        );
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(
            key,
            TICK_LOWER,
            TICK_UPPER,
            LIQUIDITY,
            type(uint128).max,
            type(uint128).max,
            owner,
            bytes("")
        );
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        return abi.encode(actions, params);
    }

    function _mineSalt(OasisHookFactory factory, bytes32 codeHash)
        private
        view
        returns (bytes32 salt)
    {
        uint160 requiredFlags = factory.REQUIRED_HOOK_FLAGS();
        for (uint256 candidate = 0; candidate < 100_000; ++candidate) {
            salt = bytes32(candidate);
            address predicted = _create2Address(address(factory), salt, codeHash);
            if (uint160(predicted) & Hooks.ALL_HOOK_MASK == requiredFlags) return salt;
        }
        revert("SALT_NOT_FOUND");
    }

    function _action(uint256 action) private pure returns (bytes1) {
        require(action <= type(uint8).max, "ACTION_OVERFLOW");
        // The explicit bound above proves this narrowing conversion is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        return bytes1(uint8(action));
    }

    function _create2Address(address deployer, bytes32 salt, bytes32 codeHash)
        private
        pure
        returns (address)
    {
        bytes32 digest = keccak256(abi.encodePacked(bytes1(0xff), deployer, salt, codeHash));
        // CREATE2 specifies the low 160 bits of this digest as the deployed address.
        // forge-lint: disable-next-line(unsafe-typecast)
        return address(uint160(uint256(digest)));
    }

    function _config() private pure returns (OasisDynamicFeeHook.FeeConfig memory) {
        return OasisDynamicFeeHook.FeeConfig({
            baseFee: 500,
            mediumFee: 2_000,
            highFee: 5_000,
            mediumAmount0Threshold: 1 ether,
            highAmount0Threshold: 10 ether,
            mediumAmount1Threshold: 1 ether,
            highAmount1Threshold: 10 ether,
            ruleVersion: 1,
            configured: true
        });
    }
}

interface IERC20Approval {
    function approve(address spender, uint256 amount) external returns (bool);
}
