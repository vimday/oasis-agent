// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Vm } from "forge-std/Vm.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";
import { DeployPermit2 } from "permit2/test/utils/DeployPermit2.sol";

import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
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

/// @notice Executor × real Permit2 end-to-end integration campaign (audit M5).
/// @dev The full stack runs against the pinned upstream Permit2 runtime
/// (DeployPermit2), not a transfer mock: mint pays through Permit2, and the
/// executor's RECENTER/REMOVE bundles settle against the same runtime. This
/// closes the previous gap where the executor was only exercised with a
/// MockPermit2 transfer surface.
contract OasisExecutorRealPermit2IntegrationTest is Test {
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;
    uint256 private constant LIQUIDITY = 1 ether;
    uint64 private constant NONCE = 1;
    uint64 private constant DEADLINE_OFFSET = 60;

    PoolManager private poolManager;
    IAllowanceTransfer private permit2;
    PositionManager private positionManager;
    OasisDynamicFeeHook private hook;
    OasisPositionExecutor private executor;
    PoolKey private key;
    TestERC20 private token0;
    TestERC20 private token1;

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

        // The payer for the initial mint is this test contract; the NFT goes to
        // the LP owner. The payer must carry Permit2 allowances on the runtime.
        _approvePayer(address(token0));
        _approvePayer(address(token1));
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(1), LP_OWNER);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);

        vm.prank(LP_OWNER);
        positionManager.setApprovalForAll(address(executor), true);
    }

    function test_recenterDownSettlesThroughRealPermit2WithoutExternalPull() public {
        uint256 ownerBefore0 = token0.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = token1.balanceOf(LP_OWNER);
        uint256 coreBefore0 = token0.balanceOf(address(poolManager));
        uint256 coreBefore1 = token1.balanceOf(address(poolManager));
        uint256 executorBefore0 = token0.balanceOf(address(executor));
        uint256 executorBefore1 = token1.balanceOf(address(executor));

        vm.recordLogs();
        vm.prank(LP_OWNER);
        executor.execute(
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -60, 180, 0.1 ether)
        );

        // Old position burned, new position minted to the same owner.
        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(positionManager.ownerOf(2), LP_OWNER);
        assertEq(positionManager.getPositionLiquidity(2), 0.1 ether);
        // Single audit entry, hash-linked to the executed nonce.
        assertEq(executor.lastNonce(), NONCE);
        assertTrue(executor.executedIntents(NONCE) != bytes32(0));
        assertEq(executor.history(0), executor.executedIntents(NONCE));
        assertEq(_countIntentExecutedEvents(), 1);

        // The executor never holds a token balance at any point: the burn
        // proceeds minus mint cost leave the pool and are taken straight to the
        // owner. No external capital entered the executor (it has no funding
        // path on the real Permit2 runtime).
        assertEq(token0.balanceOf(address(executor)), executorBefore0);
        assertEq(token1.balanceOf(address(executor)), executorBefore1);
        assertEq(token0.balanceOf(address(executor)), 0);
        assertEq(token1.balanceOf(address(executor)), 0);
        // Conservation: the entire pool outflow reached the owner, zero dust.
        assertEq(
            ownerBefore0 + (coreBefore0 - token0.balanceOf(address(poolManager))),
            token0.balanceOf(LP_OWNER)
        );
        assertEq(
            ownerBefore1 + (coreBefore1 - token1.balanceOf(address(poolManager))),
            token1.balanceOf(LP_OWNER)
        );
        assertGt(token0.balanceOf(LP_OWNER), ownerBefore0);
        assertGt(token1.balanceOf(LP_OWNER), ownerBefore1);
    }

    function test_recenterThenRemoveAcrossGenerationsThroughRealPermit2() public {
        vm.recordLogs();
        vm.prank(LP_OWNER);
        executor.execute(
            _intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -60, 180, 0.1 ether)
        );
        assertEq(positionManager.ownerOf(2), LP_OWNER);

        uint256 ownerBefore0 = token0.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = token1.balanceOf(LP_OWNER);
        uint256 coreBefore0 = token0.balanceOf(address(poolManager));
        uint256 coreBefore1 = token1.balanceOf(address(poolManager));

        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 2, NONCE + 1, 0, 0, 0));

        assertEq(positionManager.getPositionLiquidity(2), 0);
        assertEq(executor.lastNonce(), NONCE + 1);
        assertEq(executor.history(1), executor.executedIntents(NONCE + 1));
        assertEq(_countIntentExecutedEvents(), 2);
        assertEq(token0.balanceOf(address(executor)), 0);
        assertEq(token1.balanceOf(address(executor)), 0);
        // Full unwind of the second generation: pool outflow == owner inflow.
        assertEq(
            ownerBefore0 + (coreBefore0 - token0.balanceOf(address(poolManager))),
            token0.balanceOf(LP_OWNER)
        );
        assertEq(
            ownerBefore1 + (coreBefore1 - token1.balanceOf(address(poolManager))),
            token1.balanceOf(LP_OWNER)
        );
        assertGt(token0.balanceOf(LP_OWNER), ownerBefore0);
        assertGt(token1.balanceOf(LP_OWNER), ownerBefore1);
    }

    function test_recenterUpFailsClosedAtomicallyThroughRealPermit2() public {
        uint256 ownerBefore0 = token0.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = token1.balanceOf(LP_OWNER);
        uint256 coreBefore0 = token0.balanceOf(address(poolManager));
        uint256 coreBefore1 = token1.balanceOf(address(poolManager));
        uint256 executorBefore0 = token0.balanceOf(address(executor));
        uint256 executorBefore1 = token1.balanceOf(address(executor));

        // 2 ether after burning 1 ether leaves a negative net delta; the
        // OPEN_DELTA take in the pinned PositionManager fails closed before any
        // settle can pull funds on the real Permit2 runtime.
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                DeltaResolver.DeltaNotPositive.selector, Currency.unwrap(key.currency0)
            )
        );
        executor.execute(_intent(OasisPositionExecutor.Mode.RECENTER, 1, NONCE, -60, 60, 2 ether));

        // Atomic rollback: no audit entry, no nonce advance, no token moved.
        assertEq(executor.lastNonce(), 0);
        assertEq(executor.executedIntents(NONCE), bytes32(0));
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);
        assertEq(positionManager.ownerOf(1), LP_OWNER);
        assertEq(token0.balanceOf(address(executor)), executorBefore0);
        assertEq(token1.balanceOf(address(executor)), executorBefore1);
        assertEq(token0.balanceOf(LP_OWNER), ownerBefore0);
        assertEq(token1.balanceOf(LP_OWNER), ownerBefore1);
        assertEq(token0.balanceOf(address(poolManager)), coreBefore0);
        assertEq(token1.balanceOf(address(poolManager)), coreBefore1);
    }

    function test_staleNonceReplayFailsClosedThroughRealPermit2() public {
        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0);
        vm.recordLogs();
        vm.prank(LP_OWNER);
        executor.execute(intent);
        assertEq(executor.lastNonce(), NONCE);

        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.StaleNonce.selector, NONCE, NONCE)
        );
        executor.execute(intent);
        // Exactly one audit entry survives the replay attempt.
        assertEq(_countIntentExecutedEvents(), 1);
        assertEq(executor.lastNonce(), NONCE);
    }

    function test_deadlineExpiredFailsClosedThroughRealPermit2() public {
        uint256 ownerBefore0 = token0.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = token1.balanceOf(LP_OWNER);
        uint256 coreBefore0 = token0.balanceOf(address(poolManager));
        uint256 coreBefore1 = token1.balanceOf(address(poolManager));

        OasisPositionExecutor.Intent memory intent =
            _intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0);
        vm.warp(block.timestamp + DEADLINE_OFFSET + 1);
        vm.prank(LP_OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisPositionExecutor.DeadlinePassed.selector, intent.deadline)
        );
        executor.execute(intent);

        assertEq(executor.lastNonce(), 0);
        assertEq(executor.executedIntents(NONCE), bytes32(0));
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);
        assertEq(token0.balanceOf(LP_OWNER), ownerBefore0);
        assertEq(token1.balanceOf(LP_OWNER), ownerBefore1);
        assertEq(token0.balanceOf(address(poolManager)), coreBefore0);
        assertEq(token1.balanceOf(address(poolManager)), coreBefore1);
    }

    function test_pausedHookAllowsExecutorFullUnwindThroughRealPermit2() public {
        // HALT posture: additions paused, whitelist revoked, and the owner's
        // Permit2 allowances zeroed. The executor must still fully exit.
        hook.setRiskPaused(true);
        hook.setWhitelist(LP_OWNER, false);
        vm.startPrank(LP_OWNER);
        permit2.approve(address(token0), address(positionManager), 0, 0);
        permit2.approve(address(token1), address(positionManager), 0, 0);
        vm.stopPrank();

        uint256 ownerBefore0 = token0.balanceOf(LP_OWNER);
        uint256 ownerBefore1 = token1.balanceOf(LP_OWNER);
        uint256 coreBefore0 = token0.balanceOf(address(poolManager));
        uint256 coreBefore1 = token1.balanceOf(address(poolManager));

        vm.recordLogs();
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 1, NONCE, 0, 0, 0));

        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(executor.lastNonce(), NONCE);
        assertEq(_countIntentExecutedEvents(), 1);
        assertEq(token0.balanceOf(address(executor)), 0);
        assertEq(token1.balanceOf(address(executor)), 0);
        // Full exit while paused: pool outflow == owner inflow, zero dust.
        assertEq(
            ownerBefore0 + (coreBefore0 - token0.balanceOf(address(poolManager))),
            token0.balanceOf(LP_OWNER)
        );
        assertEq(
            ownerBefore1 + (coreBefore1 - token1.balanceOf(address(poolManager))),
            token1.balanceOf(LP_OWNER)
        );
        assertGt(token0.balanceOf(LP_OWNER), ownerBefore0);
        assertGt(token1.balanceOf(LP_OWNER), ownerBefore1);
    }

    function _countIntentExecutedEvents() private returns (uint256 count) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256(
            "IntentExecuted(uint64,bytes32,uint8,uint256,uint256,int24,int24,uint256,address)"
        );
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics.length >= 1 && logs[i].topics[0] == signature) {
                ++count;
            }
        }
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
