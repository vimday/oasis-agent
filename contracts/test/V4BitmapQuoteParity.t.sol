// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { SwapMath } from "@uniswap/v4-core/src/libraries/SwapMath.sol";
import { TickBitmap } from "@uniswap/v4-core/src/libraries/TickBitmap.sol";
import { TickMath } from "@uniswap/v4-core/src/libraries/TickMath.sol";

contract V4BitmapQuoteParityTest is Test {
    using TickBitmap for mapping(int16 => uint256);

    struct Step {
        uint160 price;
        uint256 consumed;
        uint256 output;
        uint256 fee;
    }

    mapping(int16 => uint256) private bitmap;

    function testOneForZeroRoundsAtUninitializedWordEnd() public {
        _check(254, 255, 257, false, 5);
    }

    function testZeroForOneRoundsAtNegativeWordStart() public {
        _check(-254, -256, -258, true, 6);
    }

    function _check(
        int24 startTick,
        int24 boundary,
        int24 initializedTick,
        bool zeroForOne,
        uint256 totalInput
    ) private {
        bitmap.flipTick(initializedTick, 1);
        (int24 firstTick, bool firstInitialized) =
            bitmap.nextInitializedTickWithinOneWord(startTick, 1, zeroForOne);
        assertEq(firstTick, boundary);
        assertFalse(firstInitialized);
        (int24 secondTick, bool secondInitialized) = bitmap.nextInitializedTickWithinOneWord(
            zeroForOne ? firstTick - 1 : firstTick, 1, zeroForOne
        );
        assertEq(secondTick, initializedTick);
        assertTrue(secondInitialized);

        Step memory first = _step(TickMath.getSqrtPriceAtTick(startTick), firstTick, totalInput);
        Step memory second = _step(first.price, secondTick, totalInput - first.consumed);
        assertEq(second.price, TickMath.getSqrtPriceAtTick(secondTick));
        assertEq(first.consumed + second.consumed, totalInput);
        assertEq(first.fee + second.fee, 2);
        assertEq(first.output + second.output, 0);
    }

    function _step(uint160 start, int24 tick, uint256 input)
        private
        pure
        returns (Step memory step)
    {
        uint256 inputWithoutFee;
        (step.price, inputWithoutFee, step.output, step.fee) = SwapMath.computeSwapStep(
            start, TickMath.getSqrtPriceAtTick(tick), 10_000, -int256(input), 3_000
        );
        step.consumed = inputWithoutFee + step.fee;
    }
}
