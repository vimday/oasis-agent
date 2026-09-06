// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";

import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IERC20Minimal } from "@uniswap/v4-core/src/interfaces/external/IERC20Minimal.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";

import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { DeltaResolver } from "@uniswap/v4-periphery/src/base/DeltaResolver.sol";
import { IPositionDescriptor } from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import { IWETH9 } from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";
import { Actions } from "@uniswap/v4-periphery/src/libraries/Actions.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { OasisPositionExecutor } from "../src/OasisPositionExecutor.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @dev Minimal transfer surface used to isolate PositionManager lifecycle behavior in this test.
///      Permit2 signature/allowance behavior is outside the hook's trust boundary and needs its own
///      pinned integration campaign before production.
contract MockPermit2Transfer {
    function transferFrom(address from, address to, uint160 amount, address token) external {
        // The PositionManager derives `from` from msgSender(); this mock deliberately preserves
        // Permit2's transferFrom surface while signature and allowance policy remain out of scope.
        // forge-lint: disable-next-line(arbitrary-send-erc20)
        require(IERC20Minimal(token).transferFrom(from, to, amount), "TRANSFER_FAILED");
    }
}

contract OasisPositionExecutorTest is Test {
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    address private constant OTHER = address(0x5000);
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;
    uint256 private constant LIQUIDITY = 1 ether;
    uint256 private constant RECENTER_LIQUIDITY = 0.1 ether;
    uint64 private constant NONCE = 1;
    uint64 private constant DEADLINE_OFFSET = 60;

    PoolManager private poolManager;
    MockPermit2Transfer private permit2;
    PositionManager private positionManager;
    OasisDynamicFeeHook private hook;
    OasisPositionExecutor private executor;
    PoolKey private key;
    TestERC20 private token0;
    TestERC20 private token1;

    function setUp() public {
        poolManager = new PoolManager(address(this));
        permit2 = new MockPermit2Transfer();
        positionManager = new PositionManager(
            poolManager,
            IAllowanceTransfer(address(permit2)),
            100_000,
            IPositionDescriptor(address(0)),
            IWETH9(address(0))
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

        TestERC20 first = new TestERC20(type(uint128).max);
        TestERC20 second = new TestERC20(type(uint128).max);
        (token0, token1) = address(first) < address(second) ? (first, second) : (second, first);
        key = PoolKey({
            currency0: Currency.wrap(address(token0)),
            currency1: Currency.wrap(address(token1)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });

        hook.configurePool(key, _config());
        int24 initializedTick = poolManager.initialize(key, uint160(1 << 96));
        assertEq(initializedTick, 0);
        assertTrue(token0.approve(address(permit2), type(uint256).max));
        assertTrue(token1.approve(address(permit2), type(uint256).max));

        // Initial position: the test contract pays, the LP owner receives the NFT.
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(1), LP_OWNER);

        // The executor acts on the NFT on the owner's behalf.
        vm.prank(LP_OWNER);
        positionManager.setApprovalForAll(address(executor), true);
    }

    function test_recenterBurnsOldMintsNewAndTakesSurplus() public {
        uint256 balanceBefore0 = token0.balanceOf(LP_OWNER);
        uint256 balanceBefore1 = token1.balanceOf(LP_OWNER);
        assertEq(balanceBefore0, 0);
        assertEq(balanceBefore1, 0);

        int24 newLower = -60;
        int24 newUpper = 180;
        vm.prank(LP_OWNER);
        executor.execute(
            _intent(
                OasisPositionExecutor.Mode.RECENTER,
                1,
                NONCE,
                newLower,
                newUpper,
                RECENTER_LIQUIDITY
            )
        );

        // The old NFT is burned (its pool liquidity is zero) and a new one exists for the owner.
        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(positionManager.ownerOf(2), LP_OWNER);
        // 0.1 ether = 1e17 << 2^128, so the narrowing cast is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(positionManager.getPositionLiquidity(2), uint128(RECENTER_LIQUIDITY));
        assertEq(positionManager.nextTokenId(), 3);

        // The surplus from the smaller new range is taken to the owner within the same bundle.
        assertGt(token0.balanceOf(LP_OWNER), balanceBefore0);
        assertGt(token1.balanceOf(LP_OWNER), balanceBefore1);

        // Audit trail: nonce consumed and hash recorded exactly once.
        assertEq(executor.lastNonce(), NONCE);
        assertTrue(executor.executedIntents(NONCE) != bytes32(0));
        assertEq(executor.history(0), executor.executedIntents(NONCE));
    }

    function test_removeBurnsAndTakesAllProceeds() public {
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(executor.lastNonce(), NONCE);
        // Proceeds are taken to the owner; the executor holds nothing between transactions.
        assertGt(token0.balanceOf(LP_OWNER), 0);
        assertGt(token1.balanceOf(LP_OWNER), 0);
        assertEq(token0.balanceOf(address(executor)), 0);
        assertEq(token1.balanceOf(address(executor)), 0);
    }

    function test_recenter_failsClosedWhenNewRangeExceedsProceeds() public {
        // Minting 1_000 ether of liquidity costs far more than the burned position yields; the
        // final TAKE(OPEN_DELTA) sees a negative credit and reverts the whole bundle. The executor
        // effects written before the pinned call roll back with it, leaving no audit trail.
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(DeltaResolver.DeltaNotPositive.selector, key.currency0)
        );
        executor.execute(
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -120, 120, 1_000 ether)
        );

        assertEq(executor.lastNonce(), 0);
        assertEq(executor.executedIntents(NONCE), bytes32(0));
        // 1 ether = 1e18 << 2^128, so the narrowing cast is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(positionManager.getPositionLiquidity(1), uint128(LIQUIDITY));
        assertEq(positionManager.ownerOf(1), LP_OWNER);
    }

    function test_revert_staleNonce() public {
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.StaleNonce.selector, NONCE, NONCE)
        );
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));
    }

    function test_revert_deadlinePassed() public {
        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0);
        // A unix timestamp fits comfortably in uint64 for centuries to come.
        // forge-lint: disable-next-line(unsafe-typecast)
        intent.deadline = uint64(block.timestamp - 1);
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.DeadlinePassed.selector, intent.deadline)
        );
        executor.execute(intent);
    }

    function test_revert_nonOwnerCannotExecute() public {
        vm.prank(OTHER);
        vm.expectRevert(abi.encodeWithSignature("OwnableUnauthorizedAccount(address)", OTHER));
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));
    }

    function test_revert_poolBindingIsImmutable() public {
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        // A different tick spacing changes the PoolId; the binding check fires before any
        // PositionManager interaction, so the pool does not even need to exist.
        PoolKey memory otherKey = key;
        otherKey.tickSpacing = 120;
        bytes32 boundPoolId = executor.boundPoolId();
        bytes32 otherPoolId = PoolId.unwrap(PoolIdLibrary.toId(otherKey));
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisPositionExecutor.PoolBindingImmutable.selector, boundPoolId, otherPoolId
            )
        );
        executor.execute(
            _intentWithKey(otherKey, OasisPositionExecutor.Mode.REMOVE, 1, NONCE + 1, 0, 0, 0)
        );
    }

    function test_revert_swapLegUnsupported() public {
        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0);
        intent.maxSwapInput = 1;
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.SwapLegUnsupported.selector, uint256(1))
        );
        executor.execute(intent);
    }

    function test_revert_notOwnerPosition() public {
        // OTHER mints a position and does not approve the executor; the executor's owner differs.
        hook.setWhitelist(OTHER, true);
        positionManager.modifyLiquidities(_mintPlan(OTHER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(2), OTHER);

        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.NotOwnerPosition.selector, uint256(2))
        );
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 2, NONCE, 0, 0, 0));
    }

    function test_revert_invalidRange() public {
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.InvalidRange.selector, int24(0), int24(0))
        );
        executor.execute(
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, 0, 0, RECENTER_LIQUIDITY)
        );
    }

    function test_revert_removeRejectsNonzeroLiquidity() public {
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisPositionExecutor.InvalidLiquidity.selector, RECENTER_LIQUIDITY
            )
        );
        executor.execute(
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, RECENTER_LIQUIDITY)
        );
    }

    function test_revert_recenterRejectsZeroLiquidity() public {
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.InvalidLiquidity.selector, uint256(0))
        );
        executor.execute(_intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -120, 120, 0));
    }

    function test_guardianCanPauseButOnlyOwnerResumes() public {
        vm.prank(GUARDIAN);
        executor.emergencyPause();
        assertTrue(executor.paused());

        vm.prank(LP_OWNER);
        vm.expectRevert(abi.encodeWithSelector(OasisPositionExecutor.ExecutorPaused.selector));
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        // The guardian cannot resume; only the owner may unwind the pause.
        vm.prank(GUARDIAN);
        vm.expectRevert(abi.encodeWithSignature("OwnableUnauthorizedAccount(address)", GUARDIAN));
        executor.setPaused(false);

        vm.prank(LP_OWNER);
        executor.setPaused(false);
        assertFalse(executor.paused());
    }

    function test_guardianCannotAddAuthority() public {
        vm.prank(GUARDIAN);
        vm.expectRevert(abi.encodeWithSignature("OwnableUnauthorizedAccount(address)", GUARDIAN));
        executor.setGuardian(OTHER);
        assertEq(executor.guardian(), GUARDIAN);
    }

    function _intent(
        OasisPositionExecutor.Mode mode,
        uint256 oldTokenId,
        uint64 nonce,
        int24 tickLower,
        int24 tickUpper,
        uint256 liquidity
    ) private view returns (OasisPositionExecutor.Intent memory) {
        return _intentWithKey(key, mode, oldTokenId, nonce, tickLower, tickUpper, liquidity);
    }

    function _intentWithKey(
        PoolKey memory poolKey,
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
            poolKey: poolKey,
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
