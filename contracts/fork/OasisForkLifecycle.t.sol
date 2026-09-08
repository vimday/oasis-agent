// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { OasisLifecycleBase } from "../test/shared/OasisLifecycleBase.sol";

/// @notice Requires explicit public-chain pins; all execution remains on a local fork.
contract OasisForkLifecycleTest is OasisLifecycleBase {}
