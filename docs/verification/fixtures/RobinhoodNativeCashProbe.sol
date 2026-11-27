// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Constructor for read-only eth_call. The return word is the measured native payout.
contract RobinhoodNativeCashProbe {
    constructor(bytes memory routerData) payable {
        require(msg.sender.code.length == 0 && msg.value > 0 && address(this).balance == msg.value);
        require(bytes4(routerData) == bytes4(keccak256("execute(bytes,bytes[],uint256)")));
        (bool ok, bytes memory output) =
            address(0x8876789976dEcBfCbBbe364623C63652db8C0904).call{value: msg.value}(routerData);
        if (!ok) {
            assembly {
                revert(add(output, 32), mload(output))
            }
        }
        uint256 payout = address(this).balance;
        (ok,) = payable(msg.sender).call{value: payout}("");
        require(ok && address(this).balance == 0);
        assembly {
            mstore(0, payout)
            return(0, 32)
        }
    }
}
