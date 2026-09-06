// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { PoolSwapTest } from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { BalanceDelta, toBalanceDelta } from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @notice Local fork only. Every network and runtime pin is required; no silent skips.
/// No RPC broadcast, private keys or live funding. Local test tokens isolate lifecycle mechanics.
contract OasisForkLifecycleTest is Test {
    address private constant OWNER = address(0x4000);
    uint128 private constant CAPITAL = 100 ether;
    uint256 private constant LIQUIDITY = 1 ether;
    IPoolManager private manager;
    PositionManager private positions;
    IAllowanceTransfer private permit2;
    PoolSwapTest private swapper;
    OasisDynamicFeeHook private hook;
    TestERC20 private token0;
    TestERC20 private token1;
    PoolKey private key;
    uint256 private tokenId;

    function setUp() public {
        uint256 height = vm.envUint("OASIS_FORK_BLOCK");
        uint256 forkId = vm.createSelectFork(vm.envString("OASIS_FORK_RPC"), height);
        assertEq(vm.activeFork(), forkId);
        assertEq(block.chainid, vm.envUint("OASIS_FORK_CHAIN_ID"));
        // The L2 height anchor is the retained parent hash: a header fetch that
        // non-archive nodes serve too, and it pins the exact L2 block. The EVM
        // blockhash cheatcode cannot return the current block, so the parent is
        // checked here as the independent pin.
        assertEq(blockhash(height - 1), vm.envBytes32("OASIS_FORK_PARENT_HASH"));
        // Foundry 1.8.1 reports the L1 origin block number as the fork-head
        // block number on chains whose RPC block JSON carries l1BlockNumber
        // (observed on the Arbitrum-Nitro Robinhood endpoint). Preflight records
        // that value; the fork head must equal exactly what the endpoint exposes.
        uint256 l1 = vm.envOr("OASIS_FORK_L1_BLOCK", uint256(0));
        if (l1 == 0) {
            assertEq(vm.getBlockNumber(), height);
        } else {
            assertEq(vm.getBlockNumber(), l1);
        }
        manager = IPoolManager(vm.envAddress("OASIS_FORK_POOL_MANAGER"));
        positions = PositionManager(payable(vm.envAddress("OASIS_FORK_POSITION_MANAGER")));
        permit2 = IAllowanceTransfer(vm.envAddress("OASIS_FORK_PERMIT2"));
        assertEq(address(manager).codehash, vm.envBytes32("OASIS_FORK_POOL_MANAGER_HASH"));
        assertEq(address(positions).codehash, vm.envBytes32("OASIS_FORK_POSITION_MANAGER_HASH"));
        assertEq(address(permit2).codehash, vm.envBytes32("OASIS_FORK_PERMIT2_HASH"));
        assertEq(address(positions.poolManager()), address(manager));
        assertEq(address(positions.permit2()), address(permit2));

        OasisHookFactory factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            manager,
            IOasisPositionManager(address(positions)),
            address(this),
            address(this),
            address(0x3000)
        );
        bytes32 salt = bytes32(0);
        bool found = false;
        uint160 flags = factory.REQUIRED_HOOK_FLAGS();
        for (uint256 i = 0; i < 100_000; ++i) {
            salt = bytes32(i);
            bytes32 digest =
                keccak256(abi.encodePacked(bytes1(0xff), address(factory), salt, codeHash));
            // CREATE2 specifies the low 160 bits as the address.
            // forge-lint: disable-next-line(unsafe-typecast)
            if (uint160(uint256(digest)) & Hooks.ALL_HOOK_MASK == flags) {
                found = true;
                break;
            }
        }
        require(found, "SALT_NOT_FOUND");
        hook = factory.deploy(
            salt,
            manager,
            IOasisPositionManager(address(positions)),
            address(this),
            address(this),
            address(0x3000)
        );
        TestERC20 first = new TestERC20(CAPITAL * 2);
        TestERC20 second = new TestERC20(CAPITAL * 2);
        (token0, token1) = address(first) < address(second) ? (first, second) : (second, first);
        key = PoolKey(
            Currency.wrap(address(token0)),
            Currency.wrap(address(token1)),
            LPFeeLibrary.DYNAMIC_FEE_FLAG,
            60,
            IHooks(address(hook))
        );
        hook.configurePool(
            key,
            OasisDynamicFeeHook.FeeConfig(
                500, 2000, 5000, 1 ether, 10 ether, 1 ether, 10 ether, 1, true
            )
        );
        assertEq(manager.initialize(key, uint160(1 << 96)), 0);
        hook.setWhitelist(OWNER, true);
        assertTrue(token0.transfer(OWNER, CAPITAL));
        assertTrue(token1.transfer(OWNER, CAPITAL));
        // Fixed fork timestamp used only to bound a Permit2 expiry cast.
        // forge-lint: disable-next-line(block-timestamp)
        require(block.timestamp + 1 days <= type(uint48).max, "EXPIRY_OVERFLOW");
        // Bound checked immediately above.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint48 expiry = uint48(block.timestamp + 1 days);
        vm.startPrank(OWNER);
        assertTrue(token0.approve(address(permit2), CAPITAL));
        assertTrue(token1.approve(address(permit2), CAPITAL));
        permit2.approve(address(token0), address(positions), CAPITAL, expiry);
        permit2.approve(address(token1), address(positions), CAPITAL, expiry);
        vm.stopPrank();
        swapper = new PoolSwapTest(manager);
        assertTrue(token0.approve(address(swapper), CAPITAL));
        assertTrue(token1.approve(address(swapper), CAPITAL));
        tokenId = positions.nextTokenId();
    }

    function test_mintSwapCollectRevokeUnwindRealPermit2() public {
        uint256 owner0 = token0.balanceOf(OWNER);
        uint256 owner1 = token1.balanceOf(OWNER);
        uint256 core0 = token0.balanceOf(address(manager));
        uint256 core1 = token1.balanceOf(address(manager));
        bytes[] memory params = new bytes[](3);
        params[0] =
            abi.encode(key, int24(-120), int24(120), LIQUIDITY, CAPITAL, CAPITAL, OWNER, bytes(""));
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        vm.prank(OWNER);
        positions.modifyLiquidities(abi.encode(hex"021212", params), block.timestamp + 1);
        assertEq(positions.ownerOf(tokenId), OWNER);
        assertEq(positions.getPositionLiquidity(tokenId), LIQUIDITY);
        assertGt(owner0 - token0.balanceOf(OWNER), 0);
        assertGt(owner1 - token1.balanceOf(OWNER), 0);
        assertEq(owner0 - token0.balanceOf(OWNER), token0.balanceOf(address(manager)) - core0);
        assertEq(owner1 - token1.balanceOf(OWNER), token1.balanceOf(address(manager)) - core1);

        _swap(true, -int256(10 ** 12));
        _swap(false, -int256(10 ** 12));
        _swap(true, int256(10 ** 10));
        _swap(false, int256(10 ** 10));
        owner0 = token0.balanceOf(OWNER);
        owner1 = token1.balanceOf(OWNER);
        _change(hex"01", 0);
        assertGt(token0.balanceOf(OWNER), owner0);
        assertGt(token1.balanceOf(OWNER), owner1);
        hook.setWhitelist(OWNER, false);
        vm.expectRevert();
        _change(hex"00", 1);
        owner0 = token0.balanceOf(OWNER);
        owner1 = token1.balanceOf(OWNER);
        core0 = token0.balanceOf(address(manager));
        core1 = token1.balanceOf(address(manager));
        _change(hex"01", LIQUIDITY);
        assertEq(positions.getPositionLiquidity(tokenId), 0);
        assertEq(token0.balanceOf(OWNER) - owner0, core0 - token0.balanceOf(address(manager)));
        assertEq(token1.balanceOf(OWNER) - owner1, core1 - token1.balanceOf(address(manager)));
        // Bounded real Permit2 permissions can be revoked independently of hook governance.
        vm.startPrank(OWNER);
        permit2.approve(address(token0), address(positions), 0, 0);
        permit2.approve(address(token1), address(positions), 0, 0);
        assertTrue(token0.approve(address(permit2), 0));
        assertTrue(token1.approve(address(permit2), 0));
        vm.stopPrank();
    }

    function _change(bytes memory action, uint256 liquidity) private {
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(tokenId, liquidity, CAPITAL, CAPITAL, bytes(""));
        if (keccak256(action) == keccak256(hex"01")) {
            params[0] = abi.encode(tokenId, liquidity, uint128(0), uint128(0), bytes(""));
        }
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        vm.prank(OWNER);
        positions.modifyLiquidities(
            abi.encode(bytes.concat(action, hex"1212"), params), block.timestamp + 1
        );
    }

    function _swap(bool zeroForOne, int256 amount) private {
        assertTrue(
            swapper.swap(
                key,
                SwapParams(
                    zeroForOne,
                    amount,
                    zeroForOne
                        ? uint160(4295128740)
                        : uint160(1461446703485210103287273052203988822378723970341)
                ),
                PoolSwapTest.TestSettings(false, false),
                bytes("")
            ) != toBalanceDelta(0, 0)
        );
    }
}
