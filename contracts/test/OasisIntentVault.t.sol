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

/// @notice Lightweight ERC-20 test token with optional transfer tax and reentrancy hook.
contract MockVaultERC20 is IERC20 {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    uint256 public outboundFeeBps;
    address public taxedSender;
    address public reenterTarget;
    bytes public reenterData;
    bool public reenterArmed;
    bool public lastReenterReverted;

    constructor(string memory name_, string memory symbol_, uint8 decimals_) {
        name = name_;
        symbol = symbol_;
        decimals = decimals_;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    /// forge-lint: disable-next-line(missing-zero-check)
    function setSenderFeeBps(address sender, uint256 feeBps_) external {
        require(feeBps_ < 10_000, "FEE_TOO_HIGH");
        taxedSender = sender;
        outboundFeeBps = feeBps_;
    }

    /// forge-lint: disable-next-line(missing-zero-check)
    function armReentrancy(address target, bytes calldata data) external {
        reenterTarget = target;
        reenterData = data;
        reenterArmed = true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ALLOWANCE");
        if (allowed != type(uint256).max) {
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) private {
        require(balanceOf[from] >= amount, "BALANCE");
        unchecked {
            balanceOf[from] -= amount;
        }
        bool applyFee = outboundFeeBps > 0 && (taxedSender == address(0) || from == taxedSender);
        uint256 fee = applyFee ? (amount * outboundFeeBps) / 10_000 : 0;
        uint256 received = amount - fee;
        balanceOf[to] += received;
        if (fee > 0) {
            totalSupply -= fee;
        }
        emit Transfer(from, to, received);

        if (reenterArmed && reenterTarget != address(0)) {
            reenterArmed = false;
            (bool ok,) = reenterTarget.call(reenterData);
            lastReenterReverted = !ok;
        }
    }
}

/// @notice 7-word Uniswap V3 `slot0()` state mock.
contract MockVaultV3Pool {
    uint160 public sqrtPriceX96;

    constructor(uint160 sqrtPriceX96_) {
        sqrtPriceX96 = sqrtPriceX96_;
    }

    function setSqrtPriceX96(uint160 sqrtPriceX96_) external {
        sqrtPriceX96 = sqrtPriceX96_;
    }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (sqrtPriceX96, 0, 0, 1, 1, 0, true);
    }
}

/// @notice 6-word Aerodrome Slipstream `slot0()` state mock.
contract MockVaultSlipstreamPool {
    uint160 public sqrtPriceX96;

    constructor(uint160 sqrtPriceX96_) {
        sqrtPriceX96 = sqrtPriceX96_;
    }

    function setSqrtPriceX96(uint160 sqrtPriceX96_) external {
        sqrtPriceX96 = sqrtPriceX96_;
    }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, bool) {
        return (sqrtPriceX96, 0, 0, 1, 1, true);
    }
}

/// @notice Deterministic allowlisted swap router mock for callback swap steps.
contract MockSwapRouter {
    uint256 public rateNum;
    uint256 public rateDen;

    constructor(uint256 rateNum_, uint256 rateDen_) {
        require(rateDen_ > 0, "DENOM");
        rateNum = rateNum_;
        rateDen = rateDen_;
    }

    function setRate(uint256 rateNum_, uint256 rateDen_) external {
        require(rateDen_ > 0, "DENOM");
        rateNum = rateNum_;
        rateDen = rateDen_;
    }

    function swapExactInput(address tokenIn, address tokenOut, uint256 amountIn, address recipient)
        external
        returns (uint256 amountOut)
    {
        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "PULL_IN");
        amountOut = (amountIn * rateNum) / rateDen;
        require(IERC20(tokenOut).transfer(recipient, amountOut), "SEND_OUT");
    }

    function swapExactInput(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        address recipient
    ) external returns (uint256 amountOut) {
        require(IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn), "PULL_IN");
        amountOut = (amountIn * rateNum) / rateDen;
        require(amountOut >= minOut, "MIN_OUT");
        require(IERC20(tokenOut).transfer(recipient, amountOut), "SEND_OUT");
    }

    function unapprovedMethod(uint256 x) external pure returns (uint256) {
        return x + 1;
    }
}

/// @notice Deterministic UniswapX `IReactor` mock executing single and batch callback orders.
contract MockUniswapXReactor is IReactor {
    function executeWithCallback(SignedOrder calldata order, bytes calldata callbackData)
        external
        payable
        override
    {
        ResolvedOrder[] memory resolved = new ResolvedOrder[](1);
        resolved[0] = abi.decode(order.order, (ResolvedOrder));
        _settle(resolved, callbackData);
    }

    function executeBatchWithCallback(SignedOrder[] calldata orders, bytes calldata callbackData)
        external
        payable
        override
    {
        ResolvedOrder[] memory resolved = new ResolvedOrder[](orders.length);
        for (uint256 i = 0; i < orders.length; ++i) {
            resolved[i] = abi.decode(orders[i].order, (ResolvedOrder));
        }
        _settle(resolved, callbackData);
    }

    function _settle(ResolvedOrder[] memory resolved, bytes calldata callbackData) private {
        for (uint256 i = 0; i < resolved.length; ++i) {
            require(
                resolved[i].input.token.transfer(msg.sender, resolved[i].input.amount),
                "REACTOR_SEND_IN"
            );
        }

        IReactorCallback(msg.sender).reactorCallback(resolved, callbackData);

        for (uint256 i = 0; i < resolved.length; ++i) {
            OutputToken[] memory outputs = resolved[i].outputs;
            for (uint256 j = 0; j < outputs.length; ++j) {
                require(
                    IERC20(outputs[j].token)
                        .transferFrom(msg.sender, outputs[j].recipient, outputs[j].amount),
                    "REACTOR_PULL_OUT"
                );
            }
        }
    }
}

contract OasisIntentVaultTest is Test {
    bytes4 private constant _ERC1271_MAGIC = IERC1271.isValidSignature.selector;
    bytes4 private constant _ERC1271_INVALID = 0xffffffff;
    uint160 private constant _INIT_SQRT = uint160(1 << 96);

    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    address private constant SWAPPER = address(0x6666);
    address private constant NEW_OWNER = address(0x7777);

    uint256 private signer1Key;
    address private signer1;
    uint256 private signer2Key;
    address private signer2;

    OasisIntentVault private vault;
    MockVaultERC20 private weth;
    MockVaultERC20 private usdc;
    MockVaultERC20 private tokenC;
    MockVaultV3Pool private v3Pool;
    MockVaultSlipstreamPool private slipPool;
    MockSwapRouter private router;
    MockUniswapXReactor private reactor;

    function setUp() public {
        (signer1, signer1Key) = makeAddrAndKey("quote-signer-1");
        (signer2, signer2Key) = makeAddrAndKey("quote-signer-2");

        vm.prank(OWNER);
        vault = new OasisIntentVault(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        weth = new MockVaultERC20("Wrapped Ether", "WETH", 18);
        usdc = new MockVaultERC20("USD Coin", "USDC", 6);
        tokenC = new MockVaultERC20("Token C", "TKC", 18);
        v3Pool = new MockVaultV3Pool(_INIT_SQRT);
        slipPool = new MockVaultSlipstreamPool(_INIT_SQRT);
        // 1 WETH (1e18) -> 2,600 USDC (2600e6): rateNum = 2600e6, rateDen = 1e18.
        router = new MockSwapRouter(2_600e6, 1 ether);
        reactor = new MockUniswapXReactor();

        weth.mint(address(reactor), 100 ether);
        usdc.mint(address(router), 1_000_000e6);
        tokenC.mint(address(router), 10_000 ether);

        vm.startPrank(OWNER);
        vault.setFillerOperator(OPERATOR);
        vault.rotateQuoteSigner(signer1);
        vault.setReactorAllowed(address(reactor), true);
        vault.setRouterAllowed(address(router), true);
        vault.setPoolAllowed(address(v3Pool), true);
        vault.setPoolAllowed(address(slipPool), true);
        vm.stopPrank();
    }

    function test_constructorGovernanceAndOwnable2Step() public {
        assertEq(vault.TREASURY(), TREASURY);
        assertEq(vault.REVENUE_SINK(), REVENUE_SINK);
        assertEq(vault.guardian(), GUARDIAN);
        assertEq(vault.owner(), OWNER);
        assertEq(vault.fillerOperator(), OPERATOR);
        assertEq(vault.quoteSigner(), signer1);
        assertEq(vault.signerEpoch(), 1);
        assertFalse(vault.paused());

        vm.startPrank(OWNER);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableInvalidOwner.selector, address(0)));
        new OasisIntentVault(address(0), GUARDIAN, TREASURY, REVENUE_SINK);

        vm.expectRevert(OasisIntentVault.ZeroAddress.selector);
        new OasisIntentVault(OWNER, address(0), TREASURY, REVENUE_SINK);

        vm.expectRevert(OasisIntentVault.NoStateChange.selector);
        vault.setFillerOperator(OPERATOR);

        vault.setGuardian(NEW_OWNER);
        assertEq(vault.guardian(), NEW_OWNER);

        vault.transferOwnership(NEW_OWNER);
        vm.stopPrank();
        assertEq(vault.owner(), OWNER);

        vm.prank(NEW_OWNER);
        vault.acceptOwnership();
        assertEq(vault.owner(), NEW_OWNER);
    }

    function test_erc1271DomainSeparatedSignatureLifecycle() public {
        OasisIntentVault fresh;
        vm.prank(OWNER);
        fresh = new OasisIntentVault(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);
        bytes32 orderHash = keccak256("order-1");
        // forge-lint: disable-next-line(environment-read-across-mutation)
        uint256 deadline = block.timestamp + 60;
        bytes memory sigEpoch1 = _signQuote(vault, signer1Key, orderHash, deadline);

        // Unconfigured quoteSigner returns 0xffffffff.
        assertEq(fresh.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);

        // Active epoch 1 signature succeeds on exact vault + chainId.
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_MAGIC);

        // Different chainId invalidates signature cleanly.
        // forge-lint: disable-next-line(environment-read-across-mutation)
        uint256 originalChain = block.chainid;
        vm.chainId(1);
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);
        vm.chainId(originalChain);

        // Expiry boundary: equal to block.timestamp succeeds; +1s fails.
        vm.warp(deadline);
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_MAGIC);
        vm.warp(deadline + 1);
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);
        vm.warp(deadline - 10);

        // A far-future deadline is rejected even though it has not expired yet.
        // forge-lint: disable-next-line(environment-read-across-mutation)
        uint256 longDeadline = block.timestamp + 301;
        bytes memory longSig = _signQuote(vault, signer1Key, orderHash, longDeadline);
        assertEq(vault.isValidSignature(orderHash, longSig), _ERC1271_INVALID);

        // Guardian pause invalidates signature; owner unpause restores it.
        vm.prank(GUARDIAN);
        vault.pause();
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);

        vm.prank(OWNER);
        vault.unpause();
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_MAGIC);

        // Rotating quoteSigner increments signerEpoch and invalidates prior epoch quotes.
        vm.prank(OWNER);
        vault.rotateQuoteSigner(signer2);
        assertEq(vault.signerEpoch(), 2);
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);

        bytes memory sigEpoch2 = _signQuote(vault, signer2Key, orderHash, deadline);
        assertEq(vault.isValidSignature(orderHash, sigEpoch2), _ERC1271_MAGIC);

        // Disabling signer and rotating back to signer1 increments epoch again; epoch 1 stays dead.
        vm.startPrank(OWNER);
        vault.disableQuoteSigner();
        assertEq(vault.quoteSigner(), address(0));
        assertEq(vault.signerEpoch(), 3);
        assertEq(vault.isValidSignature(orderHash, sigEpoch2), _ERC1271_INVALID);
        vault.rotateQuoteSigner(signer1);
        assertEq(vault.signerEpoch(), 4);
        vm.stopPrank();
        assertEq(vault.isValidSignature(orderHash, sigEpoch1), _ERC1271_INVALID);

        // Digest revocation permanently invalidates hash even under a newly signed epoch-4 quote.
        bytes memory sigEpoch4 = _signQuote(vault, signer1Key, orderHash, deadline);
        assertEq(vault.isValidSignature(orderHash, sigEpoch4), _ERC1271_MAGIC);
        vm.prank(OPERATOR);
        vault.revokeDigest(orderHash);
        assertEq(vault.isValidSignature(orderHash, sigEpoch4), _ERC1271_INVALID);
    }

    function test_executeReactorFillSingleOrderWithSwapStep() public {
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 2_500e6, recipient: SWAPPER });

        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: SWAPPER,
                nonce: 1,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 1 ether, maxAmount: 1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("single-order")
        });

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](1);
        steps[0] = OasisIntentVault.SwapStep({
            router: address(router),
            pool: address(v3Pool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: 1 ether,
            minOut: 2_550e6,
            data: ""
        });

        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: 90e6,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(v3Pool),
                minSqrtPriceX96: _INIT_SQRT - 10,
                maxSqrtPriceX96: _INIT_SQRT + 10
            }),
            steps: steps
        });

        SignedOrder memory signedOrder = SignedOrder({ order: abi.encode(order), sig: "" });
        bytes memory reactorCalldata =
            abi.encodeCall(IReactor.executeWithCallback, (signedOrder, abi.encode(steps)));

        vm.expectEmit(true, true, true, true, address(vault));
        emit OasisIntentVault.ReactorFillExecuted(address(reactor), address(usdc), 100e6, OPERATOR);

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = vault.executeReactorFill(plan, reactorCalldata);

        assertTrue(executed);
        assertEq(netProfit, 100e6);
        assertEq(usdc.balanceOf(address(vault)), 100e6);
        assertEq(usdc.balanceOf(SWAPPER), 2_500e6);
        assertEq(weth.allowance(address(vault), address(router)), 0);
        assertEq(usdc.allowance(address(vault), address(reactor)), 0);
    }

    function test_executeReactorFillIgnoresArbitraryRouterCalldata() public {
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 2_500e6, recipient: SWAPPER });

        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: SWAPPER,
                nonce: 1,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 1 ether, maxAmount: 1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("malicious-data-order")
        });

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](1);
        steps[0] = OasisIntentVault.SwapStep({
            router: address(router),
            pool: address(v3Pool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: 1 ether,
            minOut: 2_550e6,
            // Ignored by the typed adapter call; must not be able to redirect vault funds.
            data: abi.encodeCall(IERC20.transfer, (address(0xBAD), 1_000e6))
        });

        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: 90e6,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(v3Pool),
                minSqrtPriceX96: _INIT_SQRT - 10,
                maxSqrtPriceX96: _INIT_SQRT + 10
            }),
            steps: steps
        });

        SignedOrder memory signedOrder = SignedOrder({ order: abi.encode(order), sig: "" });
        bytes memory reactorCalldata =
            abi.encodeCall(IReactor.executeWithCallback, (signedOrder, abi.encode(steps)));

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = vault.executeReactorFill(plan, reactorCalldata);

        assertTrue(executed);
        assertEq(netProfit, 100e6);
        assertEq(usdc.balanceOf(address(vault)), 100e6);
        assertEq(usdc.balanceOf(address(0xBAD)), 0);
        assertEq(weth.allowance(address(vault), address(router)), 0);
    }

    function test_executeReactorFillBatchOrdersAggregatesSharedOutputTokens() public {
        OutputToken[] memory out1 = new OutputToken[](1);
        out1[0] = OutputToken({ token: address(usdc), amount: 1_200e6, recipient: SWAPPER });

        OutputToken[] memory out2 = new OutputToken[](2);
        out2[0] = OutputToken({ token: address(usdc), amount: 1_300e6, recipient: SWAPPER });
        out2[1] = OutputToken({ token: address(tokenC), amount: 5 ether, recipient: SWAPPER });

        tokenC.mint(address(vault), 5 ether);

        SignedOrder[] memory orders = new SignedOrder[](2);
        orders[0] = SignedOrder({
            order: abi.encode(
                ResolvedOrder({
                    info: OrderInfo({
                        reactor: address(reactor),
                        swapper: SWAPPER,
                        nonce: 10,
                        deadline: block.timestamp + 60,
                        additionalValidationContract: address(0),
                        additionalValidationData: ""
                    }),
                    input: InputToken({ token: weth, amount: 0.5 ether, maxAmount: 0.5 ether }),
                    outputs: out1,
                    sig: "",
                    hash: keccak256("batch-1")
                })
            ),
            sig: ""
        });
        orders[1] = SignedOrder({
            order: abi.encode(
                ResolvedOrder({
                    info: OrderInfo({
                        reactor: address(reactor),
                        swapper: SWAPPER,
                        nonce: 11,
                        deadline: block.timestamp + 60,
                        additionalValidationContract: address(0),
                        additionalValidationData: ""
                    }),
                    input: InputToken({ token: weth, amount: 0.5 ether, maxAmount: 0.5 ether }),
                    outputs: out2,
                    sig: "",
                    hash: keccak256("batch-2")
                })
            ),
            sig: ""
        });

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](1);
        steps[0] = OasisIntentVault.SwapStep({
            router: address(router),
            pool: address(slipPool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: 1 ether,
            minOut: 2_600e6,
            data: ""
        });

        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: 100e6,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(slipPool), minSqrtPriceX96: _INIT_SQRT, maxSqrtPriceX96: _INIT_SQRT
            }),
            steps: steps
        });

        bytes memory reactorCalldata =
            abi.encodeCall(IReactor.executeBatchWithCallback, (orders, abi.encode(steps)));

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = vault.executeReactorFill(plan, reactorCalldata);

        assertTrue(executed);
        assertEq(netProfit, 100e6);
        assertEq(usdc.balanceOf(SWAPPER), 2_500e6);
        assertEq(tokenC.balanceOf(SWAPPER), 5 ether);
        assertEq(usdc.allowance(address(vault), address(reactor)), 0);
        assertEq(tokenC.allowance(address(vault), address(reactor)), 0);
    }

    function test_executeReactorFillInventoryZeroSteps() public {
        usdc.mint(address(vault), 2_500e6);

        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 2_500e6, recipient: SWAPPER });
        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: SWAPPER,
                nonce: 2,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 1 ether, maxAmount: 1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("inventory-fill")
        });

        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](0);
        OasisIntentVault.ReactorFillPlan memory plan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(weth),
            minNetProfit: 1 ether,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(0), minSqrtPriceX96: 0, maxSqrtPriceX96: 0
            }),
            steps: steps
        });

        bytes memory reactorCalldata = abi.encodeCall(
            IReactor.executeWithCallback,
            (SignedOrder({ order: abi.encode(order), sig: "" }), abi.encode(steps))
        );

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = vault.executeReactorFill(plan, reactorCalldata);
        assertTrue(executed);
        assertEq(netProfit, 1 ether);
        assertEq(weth.balanceOf(address(vault)), 1 ether);
    }

    function test_earlyAbortPrecheckUnder65kGasV3AndSlipstream() public {
        OasisIntentVault.SwapStep[] memory steps = new OasisIntentVault.SwapStep[](0);
        OutputToken[] memory outputs = new OutputToken[](1);
        outputs[0] = OutputToken({ token: address(usdc), amount: 2_500e6, recipient: SWAPPER });
        ResolvedOrder memory order = ResolvedOrder({
            info: OrderInfo({
                reactor: address(reactor),
                swapper: SWAPPER,
                nonce: 3,
                deadline: block.timestamp + 60,
                additionalValidationContract: address(0),
                additionalValidationData: ""
            }),
            input: InputToken({ token: weth, amount: 1 ether, maxAmount: 1 ether }),
            outputs: outputs,
            sig: "",
            hash: keccak256("abort-order")
        });
        bytes memory reactorCalldata = abi.encodeCall(
            IReactor.executeWithCallback,
            (SignedOrder({ order: abi.encode(order), sig: "" }), abi.encode(steps))
        );

        // 1. 7-word V3 pool out-of-band abort (<65k gas).
        v3Pool.setSqrtPriceX96(_INIT_SQRT - 500);
        OasisIntentVault.ReactorFillPlan memory v3Plan = OasisIntentVault.ReactorFillPlan({
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

        vm.expectEmit(true, true, false, true, address(vault));
        emit OasisIntentVault.EarlyAborted(address(reactor), address(v3Pool), _INIT_SQRT - 500);

        vm.prank(OPERATOR);
        uint256 gasBeforeV3 = gasleft();
        (bool executedV3, uint256 profitV3) = vault.executeReactorFill(v3Plan, reactorCalldata);
        uint256 gasUsedV3 = gasBeforeV3 - gasleft();
        assertFalse(executedV3);
        assertEq(profitV3, 0);
        assertLt(gasUsedV3, 65_000);

        // 2. 6-word Slipstream pool out-of-band abort (<65k gas).
        slipPool.setSqrtPriceX96(_INIT_SQRT + 500);
        OasisIntentVault.ReactorFillPlan memory slipPlan = OasisIntentVault.ReactorFillPlan({
            reactor: address(reactor),
            profitToken: address(usdc),
            minNetProfit: 0,
            precheck: OasisIntentVault.PrecheckSqrtPrice({
                pool: address(slipPool),
                minSqrtPriceX96: _INIT_SQRT - 10,
                maxSqrtPriceX96: _INIT_SQRT + 10
            }),
            steps: steps
        });

        vm.prank(OPERATOR);
        uint256 gasBeforeSlip = gasleft();
        (bool executedSlip, uint256 profitSlip) =
            vault.executeReactorFill(slipPlan, reactorCalldata);
        uint256 gasUsedSlip = gasBeforeSlip - gasleft();
        assertFalse(executedSlip);
        assertEq(profitSlip, 0);
        assertLt(gasUsedSlip, 65_000);
    }

    function test_dualPlaneSweepsERC20AndNativeAcrossPauseState() public {
        usdc.mint(address(vault), 500e6);
        vm.deal(address(vault), 3 ether);

        // Operator sweeps to REVENUE_SINK and TREASURY while active.
        vm.startPrank(OPERATOR);
        vault.sweepERC20(address(usdc), REVENUE_SINK, 200e6);
        vault.sweepNative(TREASURY, 1 ether);
        vm.stopPrank();
        assertEq(usdc.balanceOf(REVENUE_SINK), 200e6);
        assertEq(TREASURY.balance, 1 ether);

        // Guardian pauses vault -> operator sweeps fail closed, owner can still rescue to TREASURY/REVENUE_SINK.
        vm.prank(GUARDIAN);
        vault.pause();

        vm.startPrank(OPERATOR);
        vm.expectRevert(OasisIntentVault.VaultPaused.selector);
        vault.sweepERC20(address(usdc), REVENUE_SINK, 100e6);

        vm.expectRevert(OasisIntentVault.VaultPaused.selector);
        vault.sweepNative(REVENUE_SINK, 1 ether);
        vm.stopPrank();

        vm.startPrank(OWNER);
        vault.sweepERC20(address(usdc), TREASURY, 300e6);
        vault.sweepNative(REVENUE_SINK, 2 ether);
        vm.stopPrank();
        assertEq(usdc.balanceOf(TREASURY), 300e6);
        assertEq(REVENUE_SINK.balance, 2 ether);
        assertEq(address(vault).balance, 0);
    }

    function _signQuote(
        OasisIntentVault targetVault,
        uint256 privKey,
        bytes32 orderHash,
        uint256 deadline
    ) private view returns (bytes memory) {
        bytes32 digest = targetVault.computeQuoteDigest(orderHash, deadline);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privKey, digest);
        return abi.encode(deadline, abi.encodePacked(r, s, v));
    }
}
