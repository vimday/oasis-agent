// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { IERC1271 } from "@openzeppelin/contracts/interfaces/IERC1271.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {
    IReactor,
    IReactorCallback,
    InputToken,
    OasisIntentVault,
    OrderInfo,
    OutputToken,
    ResolvedOrder,
    SignedOrder
} from "../src/OasisIntentVault.sol";
import { MockSwapRouter, MockVaultERC20, MockVaultV3Pool } from "./OasisIntentVault.t.sol";

contract CallbackSpoofer {
    function spoof(address vault, ResolvedOrder[] calldata orders, bytes calldata callbackData)
        external
    {
        IReactorCallback(vault).reactorCallback(orders, callbackData);
    }
}

/// @notice Adversarial reactor attempting callback spoofing, tampering, skipping, and reentrancy.
contract MaliciousReactor is IReactor {
    enum Mode {
        DOUBLE_CALLBACK,
        WRONG_CALLER_CALLBACK,
        TAMPERED_CALLBACK_DATA,
        SKIP_CALLBACK,
        EMPTY_RESOLVED_ORDERS,
        REENTER_EXECUTE_FILL,
        REENTER_SWEEP_ERC20,
        REENTER_SWEEP_NATIVE
    }

    Mode public mode;
    CallbackSpoofer public immutable accomplice;
    bytes public reenterPayload;
    address public sweepToken;
    address public custodyTarget;

    constructor() {
        accomplice = new CallbackSpoofer();
    }

    function setMode(Mode mode_) external {
        mode = mode_;
    }

    function setReenterPayload(bytes calldata payload) external {
        reenterPayload = payload;
    }

    /// forge-lint: disable-next-line(missing-zero-check)
    function setSweepTargets(address token_, address custodyTarget_) external {
        sweepToken = token_;
        custodyTarget = custodyTarget_;
    }

    function executeWithCallback(SignedOrder calldata order, bytes calldata callbackData)
        external
        payable
        override
    {
        ResolvedOrder[] memory resolved = new ResolvedOrder[](1);
        resolved[0] = abi.decode(order.order, (ResolvedOrder));
        require(
            resolved[0].input.token.transfer(msg.sender, resolved[0].input.amount), "MALICIOUS_SEND"
        );

        if (mode == Mode.SKIP_CALLBACK) {
            return;
        }
        if (mode == Mode.EMPTY_RESOLVED_ORDERS) {
            ResolvedOrder[] memory empty = new ResolvedOrder[](0);
            IReactorCallback(msg.sender).reactorCallback(empty, callbackData);
            return;
        }
        if (mode == Mode.TAMPERED_CALLBACK_DATA) {
            OasisIntentVault.SwapStep[] memory emptySteps = new OasisIntentVault.SwapStep[](0);
            IReactorCallback(msg.sender).reactorCallback(resolved, abi.encode(emptySteps));
            return;
        }
        if (mode == Mode.WRONG_CALLER_CALLBACK) {
            accomplice.spoof(msg.sender, resolved, callbackData);
            return;
        }
        if (mode == Mode.REENTER_EXECUTE_FILL) {
            (bool ok, bytes memory err) = msg.sender.call(reenterPayload);
            if (!ok) {
                assembly ("memory-safe") {
                    revert(add(err, 32), mload(err))
                }
            }
            return;
        }
        if (mode == Mode.REENTER_SWEEP_ERC20) {
            OasisIntentVault(payable(msg.sender)).sweepERC20(sweepToken, custodyTarget, 1);
            return;
        }
        if (mode == Mode.REENTER_SWEEP_NATIVE) {
            OasisIntentVault(payable(msg.sender)).sweepNative(custodyTarget, 1);
            return;
        }

        IReactorCallback(msg.sender).reactorCallback(resolved, callbackData);
        if (mode == Mode.DOUBLE_CALLBACK) {
            IReactorCallback(msg.sender).reactorCallback(resolved, callbackData);
        }
    }

    function executeBatchWithCallback(SignedOrder[] calldata, bytes calldata)
        external
        payable
        override
    {
        revert("UNUSED");
    }
}

/// @notice Adversarial router attempting callback and vault reentrancy during a swap step.
contract MaliciousRouter {
    enum Mode {
        REENTER_CALLBACK,
        REENTER_EXECUTE_FILL,
        REENTER_SWEEP_ERC20
    }

    Mode public mode;
    bytes public reenterPayload;
    address public sweepToken;
    address public custodyTarget;

    function setMode(Mode mode_) external {
        mode = mode_;
    }

    function setReenterPayload(bytes calldata payload) external {
        reenterPayload = payload;
    }

    /// forge-lint: disable-next-line(missing-zero-check)
    function setSweepTargets(address token_, address custodyTarget_) external {
        sweepToken = token_;
        custodyTarget = custodyTarget_;
    }

    function swapExactInput(address, address, uint256, address) external returns (uint256) {
        if (mode == Mode.REENTER_CALLBACK) {
            ResolvedOrder[] memory empty = new ResolvedOrder[](0);
            IReactorCallback(msg.sender).reactorCallback(empty, "");
        } else if (mode == Mode.REENTER_EXECUTE_FILL) {
            (bool ok, bytes memory err) = msg.sender.call(reenterPayload);
            if (!ok) {
                assembly ("memory-safe") {
                    revert(add(err, 32), mload(err))
                }
            }
        } else if (mode == Mode.REENTER_SWEEP_ERC20) {
            OasisIntentVault(payable(msg.sender)).sweepERC20(sweepToken, custodyTarget, 1);
        }
        return 0;
    }
}

contract OasisIntentVaultAdversarialTest is Test {
    bytes4 private constant _ERC1271_MAGIC = IERC1271.isValidSignature.selector;
    bytes4 private constant _ERC1271_INVALID = 0xffffffff;
    uint256 private constant _SECP256K1_N =
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
    uint160 private constant _INIT_SQRT = uint160(1 << 96);

    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    address private constant SWAPPER = address(0x6666);
    address private constant ATTACKER = address(0x9999);

    uint256 private signerKey;
    address private signer;

    OasisIntentVault private vault;
    MockVaultERC20 private weth;
    MockVaultERC20 private usdc;
    MockVaultV3Pool private v3Pool;
    MockSwapRouter private goodRouter;
    MaliciousReactor private malReactor;
    MaliciousRouter private malRouter;

    function setUp() public {
        (signer, signerKey) = makeAddrAndKey("adv-quote-signer");

        vm.prank(OWNER);
        vault = new OasisIntentVault(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        weth = new MockVaultERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockVaultERC20("USD Coin", "USDC", 6);
        v3Pool = new MockVaultV3Pool(_INIT_SQRT);
        goodRouter = new MockSwapRouter(2_600e6, 1 ether);
        malReactor = new MaliciousReactor();
        malRouter = new MaliciousRouter();

        weth.mint(address(malReactor), 100 ether);
        usdc.mint(address(goodRouter), 1_000_000e6);
        usdc.mint(address(vault), 10_000e6);
        vm.deal(address(vault), 2 ether);

        vm.startPrank(OWNER);
        vault.setFillerOperator(OPERATOR);
        vault.rotateQuoteSigner(signer);
        vault.setReactorAllowed(address(malReactor), true);
        vault.setRouterAllowed(address(goodRouter), true);
        vault.setRouterAllowed(address(malRouter), true);
        vault.setRouterSelectorAllowed(MockSwapRouter.swapExactInput.selector, true);
        vault.setPoolAllowed(address(v3Pool), true);
        vm.stopPrank();
    }

    function test_erc1271RejectsMalleableHighSInvalidVAndMalformedSignatures() public view {
        bytes32 orderHash = keccak256("adv-order");
        uint256 deadline = block.timestamp + 120;
        bytes32 digest = vault.computeQuoteDigest(orderHash, deadline);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);

        // Baseline valid signature passes.
        bytes memory validSig = abi.encode(deadline, abi.encodePacked(r, s, v));
        assertEq(vault.isValidSignature(orderHash, validSig), _ERC1271_MAGIC);

        // 1. High-s ECDSA malleability flip: s' = N - s, v' = (v == 27 ? 28 : 27).
        // forge-lint: disable-next-line(unsafe-typecast)
        bytes32 malleableS = bytes32(_SECP256K1_N - uint256(s));
        uint8 flippedV = v == 27 ? 28 : 27;
        bytes memory highSSig = abi.encode(deadline, abi.encodePacked(r, malleableS, flippedV));
        assertEq(vault.isValidSignature(orderHash, highSSig), _ERC1271_INVALID);

        // 2. Invalid v values (0, 1, 26, 29).
        assertEq(
            vault.isValidSignature(
                orderHash, abi.encode(deadline, abi.encodePacked(r, s, uint8(0)))
            ),
            _ERC1271_INVALID
        );
        assertEq(
            vault.isValidSignature(
                orderHash, abi.encode(deadline, abi.encodePacked(r, s, uint8(29)))
            ),
            _ERC1271_INVALID
        );

        // 3. Zero r/s values (ecrecover == address(0)).
        assertEq(
            vault.isValidSignature(
                orderHash, abi.encode(deadline, abi.encodePacked(bytes32(0), s, v))
            ),
            _ERC1271_INVALID
        );

        // 4. Short, oversized, wrong raw length, and dirty trailing padding return 0xffffffff without reverting.
        assertEq(vault.isValidSignature(orderHash, ""), _ERC1271_INVALID);
        assertEq(vault.isValidSignature(orderHash, hex"deadbeef"), _ERC1271_INVALID);
        assertEq(
            vault.isValidSignature(orderHash, abi.encode(deadline, abi.encodePacked(r, s))),
            _ERC1271_INVALID
        );
        assertEq(
            vault.isValidSignature(orderHash, bytes.concat(validSig, hex"00")), _ERC1271_INVALID
        );

        bytes memory dirtyPadding = abi.encode(deadline, abi.encodePacked(r, s, v));
        dirtyPadding[191] = 0x01;
        assertEq(vault.isValidSignature(orderHash, dirtyPadding), _ERC1271_INVALID);

        // Non-canonical offset (offset != 64).
        bytes memory badOffset = abi.encode(deadline, abi.encodePacked(r, s, v));
        badOffset[63] = 0x60;
        assertEq(vault.isValidSignature(orderHash, badOffset), _ERC1271_INVALID);
    }

    function test_spoofedDoubleTamperedSkippedAndEmptyReactorCallbacksRevert() public {
        ResolvedOrder[] memory orders = new ResolvedOrder[](1);
        orders[0] = _sampleResolvedOrder();
        OasisIntentVault.SwapStep[] memory steps = _singleSwapStep(address(goodRouter), 2_550e6);
        bytes memory callbackData = abi.encode(steps);

        // 1. Direct external spoof outside active fill.
        vm.prank(ATTACKER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.UnauthorizedReactorCallback.selector, ATTACKER)
        );
        vault.reactorCallback(orders, callbackData);

        vm.prank(address(malReactor));
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnauthorizedReactorCallback.selector, address(malReactor)
            )
        );
        vault.reactorCallback(orders, callbackData);

        // 2. Wrong caller callback mid-fill (accomplice spoofs callback).
        OasisIntentVault.ReactorFillPlan memory plan = _planWithSteps(address(malReactor), steps);
        bytes memory reactorCall = _reactorCall(orders[0], steps);
        address accomplice = address(malReactor.accomplice());

        malReactor.setMode(MaliciousReactor.Mode.WRONG_CALLER_CALLBACK);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnauthorizedReactorCallback.selector, accomplice
            )
        );
        vm.prank(OPERATOR);
        vault.executeReactorFill(plan, reactorCall);

        // 3. Double callback from active reactor (second call fails because CEI cleared _activeReactor).
        malReactor.setMode(MaliciousReactor.Mode.DOUBLE_CALLBACK);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnauthorizedReactorCallback.selector, address(malReactor)
            )
        );
        vm.prank(OPERATOR);
        vault.executeReactorFill(plan, reactorCall);

        // 4. Tampered callbackData (reactor substitutes different steps).
        malReactor.setMode(MaliciousReactor.Mode.TAMPERED_CALLBACK_DATA);
        vm.expectRevert(OasisIntentVault.InvalidCallbackData.selector);
        vm.prank(OPERATOR);
        vault.executeReactorFill(plan, reactorCall);

        // 5. Skipped callback (reactor returns without invoking reactorCallback).
        malReactor.setMode(MaliciousReactor.Mode.SKIP_CALLBACK);
        vm.expectRevert(OasisIntentVault.ReactorCallbackNotInvoked.selector);
        vm.prank(OPERATOR);
        vault.executeReactorFill(plan, reactorCall);

        // 6. Empty resolvedOrders inside callback.
        malReactor.setMode(MaliciousReactor.Mode.EMPTY_RESOLVED_ORDERS);
        vm.expectRevert(OasisIntentVault.EmptyResolvedOrders.selector);
        vm.prank(OPERATOR);
        vault.executeReactorFill(plan, reactorCall);
    }

    function test_reentrancyFromReactorRouterAndTokenHookReverts() public {
        ResolvedOrder memory order = _sampleResolvedOrder();
        OasisIntentVault.SwapStep[] memory goodSteps = _singleSwapStep(address(goodRouter), 2_550e6);
        OasisIntentVault.ReactorFillPlan memory malPlan =
            _planWithSteps(address(malReactor), goodSteps);
        bytes memory malCall = _reactorCall(order, goodSteps);
        bytes memory reenterFillCall =
            abi.encodeCall(OasisIntentVault.executeReactorFill, (malPlan, malCall));

        // 1. Temporarily set malReactor as fillerOperator so reentrancy checks hit ReentrantCall.
        vm.prank(OWNER);
        vault.setFillerOperator(address(malReactor));

        malReactor.setMode(MaliciousReactor.Mode.REENTER_EXECUTE_FILL);
        malReactor.setReenterPayload(reenterFillCall);
        vm.expectRevert(OasisIntentVault.ReentrantCall.selector);
        vm.prank(address(malReactor));
        vault.executeReactorFill(malPlan, malCall);

        malReactor.setMode(MaliciousReactor.Mode.REENTER_SWEEP_ERC20);
        malReactor.setSweepTargets(address(usdc), REVENUE_SINK);
        vm.expectRevert(OasisIntentVault.ReentrantCall.selector);
        vm.prank(address(malReactor));
        vault.executeReactorFill(malPlan, malCall);

        malReactor.setMode(MaliciousReactor.Mode.REENTER_SWEEP_NATIVE);
        malReactor.setSweepTargets(address(usdc), TREASURY);
        vm.expectRevert(OasisIntentVault.ReentrantCall.selector);
        vm.prank(address(malReactor));
        vault.executeReactorFill(malPlan, malCall);

        // 2. Malicious router inside normal reactor callback cannot reenter callback or fill.
        OasisIntentVault.SwapStep[] memory badRouterSteps = _singleSwapStep(address(malRouter), 0);
        bytes memory malReactorFill = _reactorCall(order, badRouterSteps);

        address benignReactor = address(new BenignSingleCallbackReactor());
        weth.mint(benignReactor, 10 ether);
        vm.startPrank(OWNER);
        vault.setReactorAllowed(benignReactor, true);
        vault.setFillerOperator(address(malRouter));
        vm.stopPrank();

        OasisIntentVault.ReactorFillPlan memory badRouterPlan =
            _planWithSteps(benignReactor, badRouterSteps);

        malRouter.setMode(MaliciousRouter.Mode.REENTER_CALLBACK);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnauthorizedReactorCallback.selector, address(malRouter)
            )
        );
        vm.prank(address(malRouter));
        vault.executeReactorFill(badRouterPlan, malReactorFill);

        malRouter.setMode(MaliciousRouter.Mode.REENTER_EXECUTE_FILL);
        malRouter.setReenterPayload(reenterFillCall);
        vm.expectRevert(OasisIntentVault.ReentrantCall.selector);
        vm.prank(address(malRouter));
        vault.executeReactorFill(badRouterPlan, malReactorFill);

        // 3. Token transfer hook tries to reenter sweepERC20 during swap output transfer.
        vm.prank(OWNER);
        vault.setFillerOperator(OPERATOR);
        usdc.armReentrancy(
            address(vault),
            abi.encodeCall(OasisIntentVault.sweepERC20, (address(usdc), REVENUE_SINK, 1))
        );
        OasisIntentVault.ReactorFillPlan memory goodBenignPlan =
            _planWithSteps(benignReactor, goodSteps);
        bytes memory goodBenignCall = _reactorCall(order, goodSteps);
        vm.prank(OPERATOR);
        vault.executeReactorFill(goodBenignPlan, goodBenignCall);
        assertTrue(usdc.lastReenterReverted());
    }

    function test_feeOnTransferUnderDeliveryAndValidationGatesRevert() public {
        address benignReactor = address(new BenignSingleCallbackReactor());
        weth.mint(benignReactor, 10 ether);
        vm.prank(OWNER);
        vault.setReactorAllowed(benignReactor, true);

        // 10% fee on USDC sent by goodRouter: 2600 USDC -> 2340 USDC received -> minOut 2550e6 reverts.
        usdc.setSenderFeeBps(address(goodRouter), 1_000);
        ResolvedOrder memory order = _sampleResolvedOrder();
        OasisIntentVault.SwapStep[] memory steps = _singleSwapStep(address(goodRouter), 2_550e6);

        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.InsufficientStepOutput.selector,
                address(v3Pool),
                address(usdc),
                10_000e6,
                12_340e6,
                2_550e6
            )
        );
        vault.executeReactorFill(_planWithSteps(benignReactor, steps), _reactorCall(order, steps));
        usdc.setSenderFeeBps(address(goodRouter), 0);

        // Unapproved reactor, unapproved router, unapproved selector, and ERC20 selector allowlist block.
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.UnapprovedReactor.selector, address(0xDEAD))
        );
        vault.executeReactorFill(_planWithSteps(address(0xDEAD), steps), _reactorCall(order, steps));

        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.InvalidReactorSelector.selector, bytes4(0x12345678)
            )
        );
        vault.executeReactorFill(_planWithSteps(benignReactor, steps), hex"12345678");

        OasisIntentVault.SwapStep[] memory badSelectorSteps =
            _singleSwapStep(address(goodRouter), 0);
        badSelectorSteps[0].data = abi.encodeCall(MockSwapRouter.unapprovedMethod, (7));
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnapprovedRouterSelector.selector,
                MockSwapRouter.unapprovedMethod.selector
            )
        );
        vault.executeReactorFill(
            _planWithSteps(benignReactor, badSelectorSteps), _reactorCall(order, badSelectorSteps)
        );

        vm.prank(OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisIntentVault.UnapprovedRouterSelector.selector, IERC20.approve.selector
            )
        );
        vault.setRouterSelectorAllowed(IERC20.approve.selector, true);

        // > 3 swap steps rejected.
        OasisIntentVault.SwapStep[] memory fourSteps = new OasisIntentVault.SwapStep[](4);
        for (uint256 i = 0; i < 4; ++i) {
            fourSteps[i] = steps[0];
        }
        vm.prank(OPERATOR);
        vm.expectRevert(abi.encodeWithSelector(OasisIntentVault.InvalidStepCount.selector, 4));
        vault.executeReactorFill(
            _planWithSteps(benignReactor, fourSteps), _reactorCall(order, fourSteps)
        );
    }

    function test_compromisedRolesAndCustodyRecipientBoundaries() public {
        // Sweeps to ANY non-TREASURY/REVENUE_SINK address revert even for OWNER or OPERATOR.
        vm.startPrank(OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.InvalidCustodyRecipient.selector, OWNER)
        );
        vault.sweepERC20(address(usdc), OWNER, 1e6);

        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.InvalidCustodyRecipient.selector, ATTACKER)
        );
        vault.sweepNative(ATTACKER, 1 ether);
        vm.stopPrank();

        // Compromised fillerOperator cannot touch governance or signer rotation.
        vm.startPrank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        vault.rotateQuoteSigner(ATTACKER);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        vault.setReactorAllowed(ATTACKER, true);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        vault.unpause();

        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.UnauthorizedPauser.selector, OPERATOR)
        );
        vault.pause();
        vm.stopPrank();

        // Guardian can pause and revoke digests, never unpause, execute fills, or sweep.
        vm.startPrank(GUARDIAN);
        vault.pause();
        vm.expectRevert(OasisIntentVault.NoStateChange.selector);
        vault.pause();

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, GUARDIAN)
        );
        vault.unpause();

        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.UnauthorizedOperator.selector, GUARDIAN)
        );
        vault.sweepERC20(address(usdc), TREASURY, 1e6);
        vm.stopPrank();

        // Unprivileged attacker cannot revoke digests.
        vm.prank(ATTACKER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisIntentVault.UnauthorizedRevoker.selector, ATTACKER)
        );
        vault.revokeDigest(keccak256("digest"));
    }

    function _sampleResolvedOrder() private view returns (ResolvedOrder memory) {
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 2_500e6, recipient: SWAPPER });
        return ResolvedOrder({
            info: OrderInfo({
                reactor: address(malReactor),
                swapper: SWAPPER,
                nonce: 1,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 1 ether, maxAmount: 1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("adv-resolved-order")
        });
    }

    function _singleSwapStep(address routerAddr, uint256 minOut)
        private
        view
        returns (OasisIntentVault.SwapStep[] memory steps)
    {
        steps = new OasisIntentVault.SwapStep[](1);
        steps[0] = OasisIntentVault.SwapStep({
            router: routerAddr,
            pool: address(v3Pool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: 1 ether,
            minOut: minOut,
            data: abi.encodeCall(
                MockSwapRouter.swapExactInput,
                (address(weth), address(usdc), 1 ether, address(vault))
            )
        });
    }

    function _planWithSteps(address reactorAddr, OasisIntentVault.SwapStep[] memory steps)
        private
        view
        returns (OasisIntentVault.ReactorFillPlan memory)
    {
        return OasisIntentVault.ReactorFillPlan({
            reactor: reactorAddr,
            profitToken: address(usdc),
            minNetProfit: 50e6,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(0), minSqrtPriceX96: 0, maxSqrtPriceX96: 0
            }),
            steps: steps
        });
    }

    function _reactorCall(ResolvedOrder memory order, OasisIntentVault.SwapStep[] memory steps)
        private
        pure
        returns (bytes memory)
    {
        return abi.encodeCall(
            IReactor.executeWithCallback,
            (SignedOrder({ order: abi.encode(order), sig: "" }), abi.encode(steps))
        );
    }
}

contract BenignSingleCallbackReactor is IReactor {
    function executeWithCallback(SignedOrder calldata order, bytes calldata callbackData)
        external
        payable
        override
    {
        ResolvedOrder[] memory resolved = new ResolvedOrder[](1);
        resolved[0] = abi.decode(order.order, (ResolvedOrder));
        require(resolved[0].input.token.transfer(msg.sender, resolved[0].input.amount), "SEND_IN");
        IReactorCallback(msg.sender).reactorCallback(resolved, callbackData);
        OutputToken[] memory outputs = resolved[0].outputs;
        for (uint256 j = 0; j < outputs.length; ++j) {
            require(
                IERC20(outputs[j].token)
                    .transferFrom(msg.sender, outputs[j].recipient, outputs[j].amount),
                "PULL_OUT"
            );
        }
    }

    function executeBatchWithCallback(SignedOrder[] calldata, bytes calldata)
        external
        payable
        override
    {
        revert("UNUSED");
    }
}
