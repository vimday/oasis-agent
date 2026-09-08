// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";

import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { IERC20Minimal } from "@uniswap/v4-core/src/interfaces/external/IERC20Minimal.sol";
import { IUnlockCallback } from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import { CustomRevert } from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { ModifyLiquidityParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";

import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IPositionDescriptor } from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import { IWETH9 } from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";
import { Actions } from "@uniswap/v4-periphery/src/libraries/Actions.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
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

/// @notice Router that drives the core PoolManager directly, bypassing the pinned
///         PositionManager. The hook must reject this sender (audit L13 direct-core calls).
contract DirectCoreRouter is IUnlockCallback {
    IPoolManager public immutable manager;

    constructor(IPoolManager manager_) {
        manager = manager_;
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(manager), "NOT_MANAGER");
        (PoolKey memory key, ModifyLiquidityParams memory params) =
            abi.decode(data, (PoolKey, ModifyLiquidityParams));
        // This direct core call must revert inside the unlock window and roll back.
        // forge-lint: disable-next-line(unused-return)
        manager.modifyLiquidity(key, params, "");
        return bytes("");
    }

    function attemptDirectModifyLiquidity(PoolKey memory key, ModifyLiquidityParams memory params)
        external
    {
        // Forge-lint directive justified: the unlock return value is not needed here.
        // forge-lint: disable-next-line(unused-return)
        manager.unlock(abi.encode(key, params));
    }
}

contract OasisPositionManagerIntegrationTest is Test {
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    address private constant OTHER = address(0x5000);
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;
    uint256 private constant LIQUIDITY = 1 ether;
    uint256 private constant ADD_LIQUIDITY = 0.5 ether;

    PoolManager private poolManager;
    MockPermit2Transfer private permit2;
    PositionManager private positionManager;
    OasisDynamicFeeHook private hook;
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
    }

    function test_realPositionManagerEnforcesFirstMintAndAllowsRevokedOwnerToRemove() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);

        assertEq(positionManager.ownerOf(1), LP_OWNER);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);

        bytes memory hookReason = abi.encodeWithSelector(
            OasisDynamicFeeHook.PositionNotWhitelisted.selector, uint256(2), OTHER
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(hook),
                IHooks.beforeAddLiquidity.selector,
                hookReason,
                abi.encodePacked(Hooks.HookCallFailed.selector)
            )
        );
        positionManager.modifyLiquidities(_mintPlan(OTHER), block.timestamp + 1);
        assertEq(positionManager.nextTokenId(), 2);

        hook.setWhitelist(LP_OWNER, false);
        vm.prank(LP_OWNER);
        positionManager.modifyLiquidities(_decreasePlan(1, LIQUIDITY), block.timestamp + 1);
        assertEq(positionManager.getPositionLiquidity(1), 0);
    }

    function test_laterIncreaseByApprovedExecutorSucceeds() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);

        // The executor (this test contract) is not the NFT owner: increase requires
        // owner approval plus the hook's whitelist gate on the recorded owner.
        vm.prank(LP_OWNER);
        positionManager.approve(address(this), 1);
        positionManager.modifyLiquidities(_increasePlan(1, ADD_LIQUIDITY), block.timestamp + 1);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY + ADD_LIQUIDITY);
    }

    function test_nftTransferMovesWhitelistGateToNewOwner() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.modifyLiquidities(_mintPlan(LP_OWNER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(1), LP_OWNER);

        // NFT transfers on the pinned PositionManager must run outside an unlock
        // window (onlyIfPoolManagerLocked), so the owner transfers directly.
        vm.prank(LP_OWNER);
        positionManager.transferFrom(LP_OWNER, OTHER, 1);
        assertEq(positionManager.ownerOf(1), OTHER);

        // The whitelist gate binds to the current NFT owner: OTHER (now whitelisted)
        // authorizes the executor, and the increase passes the hook's owner check.
        hook.setWhitelist(OTHER, true);
        vm.prank(OTHER);
        positionManager.approve(address(this), 1);
        positionManager.modifyLiquidities(_increasePlan(1, ADD_LIQUIDITY), block.timestamp + 1);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY + ADD_LIQUIDITY);

        // Revoking the new owner's whitelist immediately blocks further additions.
        hook.setWhitelist(OTHER, false);
        bytes memory hookReason = abi.encodeWithSelector(
            OasisDynamicFeeHook.PositionNotWhitelisted.selector, uint256(1), OTHER
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(hook),
                IHooks.beforeAddLiquidity.selector,
                hookReason,
                abi.encodePacked(Hooks.HookCallFailed.selector)
            )
        );
        positionManager.modifyLiquidities(_increasePlan(1, ADD_LIQUIDITY), block.timestamp + 1);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY + ADD_LIQUIDITY);
    }

    function test_alternateRecipientMintRespectsWhitelist() public {
        hook.setWhitelist(OTHER, true);
        positionManager.modifyLiquidities(_mintPlan(OTHER), block.timestamp + 1);
        assertEq(positionManager.ownerOf(1), OTHER);
        assertEq(positionManager.getPositionLiquidity(1), LIQUIDITY);
    }

    function test_directCoreModifyLiquidityRejected() public {
        DirectCoreRouter router = new DirectCoreRouter(poolManager);
        ModifyLiquidityParams memory params = ModifyLiquidityParams({
            tickLower: TICK_LOWER,
            tickUpper: TICK_UPPER,
            liquidityDelta: 0.5 ether,
            salt: bytes32(uint256(1))
        });
        bytes memory hookReason = abi.encodeWithSelector(
            OasisDynamicFeeHook.WrongPositionManager.selector, address(router)
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(hook),
                IHooks.beforeAddLiquidity.selector,
                hookReason,
                abi.encodePacked(Hooks.HookCallFailed.selector)
            )
        );
        router.attemptDirectModifyLiquidity(key, params);
    }

    function test_wrongSenderRejectedOnHookCallback() public {
        ModifyLiquidityParams memory params = ModifyLiquidityParams({
            tickLower: TICK_LOWER,
            tickUpper: TICK_UPPER,
            liquidityDelta: 0.5 ether,
            salt: bytes32(uint256(1))
        });
        vm.prank(address(poolManager));
        vm.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.WrongPositionManager.selector, OTHER)
        );
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeAddLiquidity(OTHER, key, params, bytes(""));
    }

    function test_invalidPositionSaltRejectedOnHookCallback() public {
        // Through the pinned PositionManager the salt is always a minted tokenId (> 0),
        // so the zero-salt guard is defense in depth for a hostile direct-core path.
        ModifyLiquidityParams memory params = ModifyLiquidityParams({
            tickLower: TICK_LOWER,
            tickUpper: TICK_UPPER,
            liquidityDelta: 0.5 ether,
            salt: bytes32(0)
        });
        vm.prank(address(poolManager));
        vm.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.InvalidPositionSalt.selector));
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeAddLiquidity(address(positionManager), key, params, bytes(""));
    }

    function test_whitelistGovernanceRejectsZeroAddress() public {
        vm.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.ZeroAddress.selector));
        hook.setWhitelist(address(0), true);
        vm.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.ZeroAddress.selector));
        hook.setWhitelist(address(0), false);
    }

    function test_whitelistGovernanceRejectsForbiddenAccounts() public {
        address[3] memory forbidden =
            [address(hook), address(poolManager), address(positionManager)];
        // The forbidden-account table is the assertion itself; iterating the three
        // system addresses is deliberate coverage, not an accident.
        // forge-lint: disable-start(calls-loop)
        for (uint256 i = 0; i < forbidden.length; ++i) {
            vm.expectRevert(
                abi.encodeWithSelector(
                    OasisDynamicFeeHook.ForbiddenWhitelistAccount.selector, forbidden[i]
                )
            );
            hook.setWhitelist(forbidden[i], true);
        }
        // forge-lint: disable-end(calls-loop)
    }

    function test_whitelistGovernanceRejectsNoStateChange() public {
        hook.setWhitelist(LP_OWNER, true);
        vm.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.NoStateChange.selector));
        hook.setWhitelist(LP_OWNER, true);

        hook.setWhitelist(LP_OWNER, false);
        vm.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.NoStateChange.selector));
        hook.setWhitelist(LP_OWNER, false);
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

    function _increasePlan(uint256 tokenId, uint256 liquidity) private view returns (bytes memory) {
        bytes memory actions = abi.encodePacked(
            _action(Actions.INCREASE_LIQUIDITY),
            _action(Actions.CLOSE_CURRENCY),
            _action(Actions.CLOSE_CURRENCY)
        );
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(tokenId, liquidity, type(uint128).max, type(uint128).max, bytes(""));
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        return abi.encode(actions, params);
    }

    function _decreasePlan(uint256 tokenId, uint256 liquidity) private view returns (bytes memory) {
        bytes memory actions = abi.encodePacked(
            _action(Actions.DECREASE_LIQUIDITY),
            _action(Actions.CLOSE_CURRENCY),
            _action(Actions.CLOSE_CURRENCY)
        );
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(tokenId, liquidity, uint128(0), uint128(0), bytes(""));
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
