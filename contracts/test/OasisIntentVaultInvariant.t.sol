// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { StdInvariant } from "forge-std/StdInvariant.sol";
import { IERC1271 } from "@openzeppelin/contracts/interfaces/IERC1271.sol";

import {
    IReactor,
    InputToken,
    OasisIntentVault,
    OrderInfo,
    OutputToken,
    ResolvedOrder,
    SignedOrder
} from "../src/OasisIntentVault.sol";
import {
    MockSwapRouter,
    MockUniswapXReactor,
    MockVaultERC20,
    MockVaultV3Pool
} from "./OasisIntentVault.t.sol";

struct HandlerConfig {
    OasisIntentVault vault;
    MockVaultERC20 weth;
    MockVaultERC20 usdc;
    MockVaultV3Pool v3Pool;
    MockSwapRouter router;
    MockUniswapXReactor reactor;
    address ownerAddr;
    address guardianAddr;
    address operatorAddr;
    address treasuryAddr;
    address revenueSinkAddr;
    address swapperAddr;
    uint256 initialUsdcCapital;
    uint256 initialNativeCapital;
    uint256 initialSignerKey;
}

contract IntentVaultHandler is Test {
    bytes4 private constant _ERC1271_MAGIC = IERC1271.isValidSignature.selector;
    bytes4 private constant _ERC1271_INVALID = 0xffffffff;
    uint160 private constant _INIT_SQRT = uint160(1 << 96);

    OasisIntentVault public immutable vault;
    MockVaultERC20 public immutable weth;
    MockVaultERC20 public immutable usdc;
    MockVaultV3Pool public immutable v3Pool;
    MockSwapRouter public immutable router;
    MockUniswapXReactor public immutable reactor;

    address public immutable ownerAddr;
    address public immutable guardianAddr;
    address public immutable operatorAddr;
    address public immutable treasuryAddr;
    address public immutable revenueSinkAddr;
    address public immutable swapperAddr;

    uint256 public immutable initialUsdcCapital;
    uint256 public immutable initialNativeCapital;
    uint256 public cumulativeRecordedProfit;
    uint256 public expectedEpoch;
    uint256 private currentSignerKey;
    uint256 private orderCounter;

    constructor(HandlerConfig memory cfg) {
        vault = cfg.vault;
        weth = cfg.weth;
        usdc = cfg.usdc;
        v3Pool = cfg.v3Pool;
        router = cfg.router;
        reactor = cfg.reactor;
        ownerAddr = cfg.ownerAddr;
        guardianAddr = cfg.guardianAddr;
        operatorAddr = cfg.operatorAddr;
        treasuryAddr = cfg.treasuryAddr;
        revenueSinkAddr = cfg.revenueSinkAddr;
        swapperAddr = cfg.swapperAddr;
        initialUsdcCapital = cfg.initialUsdcCapital;
        initialNativeCapital = cfg.initialNativeCapital;
        currentSignerKey = cfg.initialSignerKey;
        expectedEpoch = cfg.vault.signerEpoch();
    }

    function executeProfitableFill(uint256 ethSeed) external {
        if (vault.paused()) return;
        uint256 reactorWeth = weth.balanceOf(address(reactor));
        if (reactorWeth < 0.1 ether) return;
        uint256 maxIn = reactorWeth < 2 ether ? reactorWeth : 2 ether;
        uint256 amountIn = bound(ethSeed, 0.1 ether, maxIn);

        // Router returns 2,600 USDC per 1 WETH; swapper asks 2,500 USDC per 1 WETH -> +100 USDC/WETH profit.
        uint256 routerOut = (amountIn * 2_600e6) / 1 ether;
        if (usdc.balanceOf(address(router)) < routerOut) return;
        uint256 orderOut = (amountIn * 2_500e6) / 1 ether;
        uint256 minProfit = routerOut - orderOut;

        orderCounter += 1;
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: orderOut, recipient: swapperAddr });
        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: swapperAddr,
                nonce: orderCounter,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: amountIn, maxAmount: amountIn }),
            outputs: outputs,
            sig: "",
            hash: keccak256(abi.encode("inv-order", orderCounter))
        });

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](1);
        steps[0] = OasisIntentVault.SwapStep({
            router: address(router),
            pool: address(v3Pool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: amountIn,
            minOut: routerOut,
            data: abi.encodeCall(
                MockSwapRouter.swapExactInput,
                (address(weth), address(usdc), amountIn, address(vault))
            )
        });

        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: minProfit,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(v3Pool),
                minSqrtPriceX96: _INIT_SQRT - 10,
                maxSqrtPriceX96: _INIT_SQRT + 10
            }),
            steps: steps
        });

        bytes memory reactorCalldata = abi.encodeCall(
            IReactor.executeWithCallback,
            (SignedOrder({ order: abi.encode(order), sig: "" }), abi.encode(steps))
        );

        vm.prank(operatorAddr);
        (bool executed, uint256 netProfit) = vault.executeReactorFill(plan, reactorCalldata);
        require(executed, "EXPECTED_FILL");
        cumulativeRecordedProfit += netProfit;
    }

    function executeEarlyAbort() external {
        if (vault.paused()) return;
        v3Pool.setSqrtPriceX96(_INIT_SQRT + 500);

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](0);
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 1e6, recipient: swapperAddr });
        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: swapperAddr,
                nonce: 999,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 0.1 ether, maxAmount: 0.1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("inv-abort")
        });

        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: 0,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(v3Pool),
                minSqrtPriceX96: _INIT_SQRT - 10,
                maxSqrtPriceX96: _INIT_SQRT + 10
            }),
            steps: steps
        });
        bytes memory reactorCalldata = abi.encodeCall(
            IReactor.executeWithCallback,
            (SignedOrder({ order: abi.encode(order), sig: "" }), abi.encode(steps))
        );

        vm.prank(operatorAddr);
        (bool executed,) = vault.executeReactorFill(plan, reactorCalldata);
        require(!executed, "EXPECTED_ABORT");
        v3Pool.setSqrtPriceX96(_INIT_SQRT);
    }

    function sweepERC20ToCustody(uint256 amountSeed, bool toTreasury) external {
        uint256 bal = usdc.balanceOf(address(vault));
        if (bal == 0) return;
        uint256 amount = bound(amountSeed, 1, bal);
        address recipient = toTreasury ? treasuryAddr : revenueSinkAddr;

        if (vault.paused()) {
            vm.prank(ownerAddr);
            vault.sweepERC20(address(usdc), recipient, amount);
        } else {
            vm.prank(operatorAddr);
            vault.sweepERC20(address(usdc), recipient, amount);
        }
    }

    function sweepNativeToCustody(uint256 amountSeed, bool toTreasury) external {
        uint256 bal = address(vault).balance;
        if (bal == 0) return;
        uint256 amount = bound(amountSeed, 1, bal);
        address recipient = toTreasury ? treasuryAddr : revenueSinkAddr;

        if (vault.paused()) {
            vm.prank(ownerAddr);
            vault.sweepNative(recipient, amount);
        } else {
            vm.prank(operatorAddr);
            vault.sweepNative(recipient, amount);
        }
    }

    function togglePause(bool shouldPause) external {
        bool currentlyPaused = vault.paused();
        if (shouldPause && !currentlyPaused) {
            vm.prank(guardianAddr);
            vault.pause();
        } else if (!shouldPause && currentlyPaused) {
            vm.prank(ownerAddr);
            vault.unpause();
        }
    }

    function rotateSignerAndCheckEpoch(uint256 keySeed, uint256 digestSeed) external {
        uint256 nextKey = bound(keySeed, 10, 10_000);
        address nextSigner = vm.addr(nextKey);
        if (nextSigner == vault.quoteSigner()) {
            nextKey += 1;
            nextSigner = vm.addr(nextKey);
        }

        bytes32 sampleHash = keccak256(abi.encode("epoch-check", digestSeed));
        uint256 deadline = block.timestamp + 120;
        bytes32 oldDigest = vault.computeQuoteDigest(sampleHash, deadline);
        (uint8 oldV, bytes32 oldR, bytes32 oldS) = vm.sign(currentSignerKey, oldDigest);
        bytes memory oldSig = abi.encode(deadline, abi.encodePacked(oldR, oldS, oldV));

        vm.prank(ownerAddr);
        vault.rotateQuoteSigner(nextSigner);
        expectedEpoch += 1;
        currentSignerKey = nextKey;

        // Prior epoch signature must always fail after rotation.
        require(vault.isValidSignature(sampleHash, oldSig) == _ERC1271_INVALID, "OLD_EPOCH_VALID");

        // Fresh signature succeeds iff unpaused and not revoked.
        bytes32 newDigest = vault.computeQuoteDigest(sampleHash, deadline);
        (uint8 newV, bytes32 newR, bytes32 newS) = vm.sign(currentSignerKey, newDigest);
        bytes memory newSig = abi.encode(deadline, abi.encodePacked(newR, newS, newV));
        bytes4 expectedMagic = (vault.paused() || vault.revokedDigests(sampleHash))
            ? _ERC1271_INVALID
            : _ERC1271_MAGIC;
        require(vault.isValidSignature(sampleHash, newSig) == expectedMagic, "NEW_EPOCH_MISMATCH");
    }

    function revokeSampleDigest(uint256 digestSeed) external {
        bytes32 sampleHash = keccak256(abi.encode("revokable", digestSeed));
        if (vault.revokedDigests(sampleHash)) return;
        uint256 deadline = block.timestamp + 120;
        bytes32 digest = vault.computeQuoteDigest(sampleHash, deadline);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(currentSignerKey, digest);
        bytes memory sig = abi.encode(deadline, abi.encodePacked(r, s, v));

        vm.prank(guardianAddr);
        vault.revokeDigest(sampleHash);
        require(vault.isValidSignature(sampleHash, sig) == _ERC1271_INVALID, "REVOKED_STILL_VALID");
    }
}

contract OasisIntentVaultInvariantTest is StdInvariant, Test {
    uint160 private constant _INIT_SQRT = uint160(1 << 96);
    uint256 private constant _INITIAL_USDC = 10_000e6;
    uint256 private constant _INITIAL_ETH = 10 ether;

    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    address private constant SWAPPER = address(0x6666);

    OasisIntentVault private vault;
    MockVaultERC20 private weth;
    MockVaultERC20 private usdc;
    MockSwapRouter private router;
    MockUniswapXReactor private reactor;
    IntentVaultHandler private handler;

    function setUp() public {
        (address initialSigner, uint256 initialSignerKey) = makeAddrAndKey("inv-init-signer");

        vm.prank(OWNER);
        vault = new OasisIntentVault(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        weth = new MockVaultERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockVaultERC20("USD Coin", "USDC", 6);
        MockVaultV3Pool v3Pool = new MockVaultV3Pool(_INIT_SQRT);
        router = new MockSwapRouter(2_600e6, 1 ether);
        reactor = new MockUniswapXReactor();

        weth.mint(address(reactor), 1_000 ether);
        usdc.mint(address(router), 10_000_000e6);
        usdc.mint(address(vault), _INITIAL_USDC);
        vm.deal(address(vault), _INITIAL_ETH);

        vm.startPrank(OWNER);
        vault.setFillerOperator(OPERATOR);
        vault.rotateQuoteSigner(initialSigner);
        vault.setReactorAllowed(address(reactor), true);
        vault.setRouterAllowed(address(router), true);
        vault.setRouterSelectorAllowed(MockSwapRouter.swapExactInput.selector, true);
        vault.setPoolAllowed(address(v3Pool), true);
        vm.stopPrank();

        handler = new IntentVaultHandler(
            HandlerConfig({
                vault: vault,
                weth: weth,
                usdc: usdc,
                v3Pool: v3Pool,
                router: router,
                reactor: reactor,
                ownerAddr: OWNER,
                guardianAddr: GUARDIAN,
                operatorAddr: OPERATOR,
                treasuryAddr: TREASURY,
                revenueSinkAddr: REVENUE_SINK,
                swapperAddr: SWAPPER,
                initialUsdcCapital: _INITIAL_USDC,
                initialNativeCapital: _INITIAL_ETH,
                initialSignerKey: initialSignerKey
            })
        );

        bytes4[] memory selectors = new bytes4[](7);
        selectors[0] = IntentVaultHandler.executeProfitableFill.selector;
        selectors[1] = IntentVaultHandler.executeEarlyAbort.selector;
        selectors[2] = IntentVaultHandler.sweepERC20ToCustody.selector;
        selectors[3] = IntentVaultHandler.sweepNativeToCustody.selector;
        selectors[4] = IntentVaultHandler.togglePause.selector;
        selectors[5] = IntentVaultHandler.rotateSignerAndCheckEpoch.selector;
        selectors[6] = IntentVaultHandler.revokeSampleDigest.selector;
        targetContract(address(handler));
        targetSelector(FuzzSelector({ addr: address(handler), selectors: selectors }));
    }

    function invariant_erc20AndNativeCustodyConserved() public view {
        uint256 totalUsdc = usdc.balanceOf(address(vault)) + usdc.balanceOf(TREASURY)
            + usdc.balanceOf(REVENUE_SINK);
        assertGe(totalUsdc, handler.initialUsdcCapital());
        assertEq(totalUsdc, handler.initialUsdcCapital() + handler.cumulativeRecordedProfit());
        assertEq(usdc.balanceOf(OPERATOR), 0);
        assertEq(usdc.balanceOf(GUARDIAN), 0);
        assertEq(usdc.balanceOf(OWNER), 0);

        uint256 totalEth = address(vault).balance + TREASURY.balance + REVENUE_SINK.balance;
        assertEq(totalEth, handler.initialNativeCapital());
    }

    function invariant_zeroLingeringAllowances() public view {
        assertEq(weth.allowance(address(vault), address(router)), 0);
        assertEq(usdc.allowance(address(vault), address(reactor)), 0);
    }

    function invariant_controlPlaneAndSignerEpochLocked() public view {
        assertEq(vault.TREASURY(), TREASURY);
        assertEq(vault.REVENUE_SINK(), REVENUE_SINK);
        assertEq(vault.owner(), OWNER);
        assertEq(vault.guardian(), GUARDIAN);
        assertEq(vault.fillerOperator(), OPERATOR);
        assertEq(vault.signerEpoch(), handler.expectedEpoch());
        assertGe(vault.signerEpoch(), 1);
    }
}
