"""Static portfolio manifest must exactly bind the reconciled-risk policy.

The manifest is supplied configuration, never chain truth: these tests pin the
fail-closed construction rules that stop a manifest from silently redefining the
monitored portfolio, its asset identities or its USD feed pins.
"""

from pathlib import Path

import pytest

from oasis_agent.farming.price_evidence import UsdFeedPolicy
from oasis_agent.models.common import ZERO_ADDRESS
from oasis_agent.risk.account_risk import GlobalRiskPolicy
from oasis_agent.risk.portfolio_manifest import (
    ChainInventory,
    ManifestAsset,
    ManifestFeed,
    ManifestPosition,
    PortfolioManifest,
)

CHAIN_ID = 4663
ACCOUNT = "0x" + "e6" * 20
OTHER_ACCOUNT = "0x" + "e7" * 20
TOKEN = "0x" + "11" * 20
OTHER_TOKEN = "0x" + "12" * 20
WETH = "0x" + "13" * 20
NATIVE_FEED = "0x" + "f1" * 20
TOKEN_FEED = "0x" + "f2" * 20
WETH_FEED = "0x" + "f3" * 20
RUNTIME = "0x" + "ab" * 32
TOKEN_RUNTIME = "0x" + "cd" * 32
WETH_RUNTIME = "0x" + "ce" * 32
POSITION_MANAGER = "0x" + "21" * 20
POSITION_MANAGER_RUNTIME = "0x" + "de" * 32
STATE_VIEW = "0x" + "31" * 20
STATE_VIEW_RUNTIME = "0x" + "df" * 32
POOL_MANAGER = "0x" + "41" * 20
POOL_MANAGER_RUNTIME = "0x" + "e1" * 32
POOL_ID = "0x" + "3c" * 32
TOKEN_ID = 7

# Shipped Base example, independently verified from two Base mainnet providers.
BASE_EXAMPLE = Path(__file__).parents[2] / "configs/account-risk.base.example.json"
BASE_ACCOUNT = "0xe653df20a8c525edf381942ffc47704b7bdfe97d"
BASE_FEED = "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"
BASE_FEED_RUNTIME = "0xbd6f524cdc4268b6bd1bb6f77a8821faeea9c52ee9e0afa0b6d948ce82c966c2"


def feed(
    address: str = NATIVE_FEED,
    description: str = "ETH / USD",
    runtime: str = RUNTIME,
    max_age: int = 300,
) -> ManifestFeed:
    return ManifestFeed(
        address=address,
        description=description,
        runtime_keccak=runtime,
        max_age_seconds=max_age,
    )


def native_asset(asset_id: str = "native", role: str = "cash") -> ManifestAsset:
    return ManifestAsset(
        asset_id=asset_id,
        address=ZERO_ADDRESS,
        decimals=18,
        runtime_keccak=None,
        role=role,
        feed=feed(),
    )


def token_asset(
    asset_id: str = "usdc",
    address: str = TOKEN,
    decimals: int = 6,
    runtime: str | None = TOKEN_RUNTIME,
    role: str = "cash",
    feed_: ManifestFeed | None = None,
) -> ManifestAsset:
    return ManifestAsset(
        asset_id=asset_id,
        address=address,
        decimals=decimals,
        runtime_keccak=runtime,
        role=role,
        feed=feed_
        if feed_ is not None
        else feed(address=TOKEN_FEED, description="USDC / USD", runtime=RUNTIME),
    )


def weth_asset() -> ManifestAsset:
    return ManifestAsset(
        asset_id="weth",
        address=WETH,
        decimals=18,
        runtime_keccak=WETH_RUNTIME,
        role="cash",
        feed=feed(address=WETH_FEED, description="WETH / USD", runtime=RUNTIME),
    )


def position(**overrides: object) -> ManifestPosition:
    options: dict[str, object] = {
        "asset_id": "lp-position",
        "position_manager": POSITION_MANAGER,
        "position_manager_runtime_keccak": POSITION_MANAGER_RUNTIME,
        "token_id": TOKEN_ID,
        "pool_id": POOL_ID,
        "state_view": STATE_VIEW,
        "state_view_runtime_keccak": STATE_VIEW_RUNTIME,
        "pool_manager": POOL_MANAGER,
        "pool_manager_runtime_keccak": POOL_MANAGER_RUNTIME,
        "currency0_id": "usdc",
        "currency1_id": "weth",
    }
    options.update(overrides)
    return ManifestPosition(**options)  # type: ignore[arg-type]


def chain(
    *,
    chain_id: int = CHAIN_ID,
    account: str = ACCOUNT,
    assets: tuple[ManifestAsset, ...] | None = None,
    positions: tuple[ManifestPosition, ...] = (),
    primary: str = "rpc_a",
    secondary: str = "rpc_b",
) -> ChainInventory:
    return ChainInventory(
        chain_id=chain_id,
        account=account,
        primary_source=primary,
        secondary_source=secondary,
        assets=assets if assets is not None else (native_asset(), token_asset()),
        positions=positions,
    )


def position_chain(
    positions: tuple[ManifestPosition, ...] = (position(),),
    *,
    assets: tuple[ManifestAsset, ...] | None = None,
) -> ChainInventory:
    """One account holding two pool currencies and a declared LP position."""
    return chain(
        assets=assets if assets is not None else (native_asset(), token_asset(), weth_asset()),
        positions=positions,
    )


def position_policy(**overrides: object) -> GlobalRiskPolicy:
    options: dict[str, object] = {
        "inventory": {f"{CHAIN_ID}:{ACCOUNT}": ("native", "usdc", "weth", "lp-position")}
    }
    options.update(overrides)
    return policy(**options)


def policy(**overrides: object) -> GlobalRiskPolicy:
    base: dict[str, object] = {
        "inventory": {f"{CHAIN_ID}:{ACCOUNT}": ("native", "usdc")},
        "total_capital_usd_micros": 500_000_000,
        "daily_loss_usd_micros": 200_000_000,
        "cumulative_loss_usd_micros": 500_000_000,
        "max_age_seconds": 600,
        "max_skew_seconds": 60,
    }
    base.update(overrides)
    return GlobalRiskPolicy(**base)  # type: ignore[arg-type]


def manifest(
    *,
    chains: tuple[ChainInventory, ...] | None = None,
    policy_: GlobalRiskPolicy | None = None,
) -> PortfolioManifest:
    return PortfolioManifest(
        policy=policy_ if policy_ is not None else policy(),
        chains=chains if chains is not None else (chain(),),
    )


def test_manifest_covers_the_policy_and_materializes_pinned_feeds() -> None:
    built = manifest()
    assert built.keys == (f"{CHAIN_ID}:{ACCOUNT}",)
    entry = built.chain(built.keys[0])
    assert entry.native.asset_id == "native" and entry.native.is_native
    assert entry.erc20_assets == (entry.asset("usdc"),)
    assert entry.asset_by_address(TOKEN) == entry.asset("usdc")
    assert built.accounts_on(CHAIN_ID) == frozenset({ACCOUNT})
    assert built.native_asset(built.keys[0]) == entry.native
    assert entry.asset("usdc").usd_feed(CHAIN_ID) == UsdFeedPolicy(
        CHAIN_ID, TOKEN_FEED, TOKEN, "USDC / USD", RUNTIME, 300
    )
    native_usd = entry.native.usd_feed(CHAIN_ID)
    assert native_usd.base_token == ZERO_ADDRESS and native_usd.description == "ETH / USD"


def test_manifest_digest_is_deterministic_and_policy_sensitive() -> None:
    assert manifest().digest == manifest().digest
    assert manifest().digest != manifest(policy_=policy(max_skew_seconds=61)).digest
    assert len(manifest().digest) == 64


def test_a_declared_position_is_part_of_the_reconciled_inventory() -> None:
    """The position identifier, not only its currencies, must be in the policy."""
    built = PortfolioManifest(policy=position_policy(), chains=(position_chain(),))
    entry = built.chain(built.keys[0])
    assert entry.asset_ids == ("native", "usdc", "weth", "lp-position")
    assert entry.position("lp-position") == position()
    with pytest.raises(KeyError):
        entry.asset("lp-position")  # a position is not a fungible asset
    with pytest.raises(KeyError):
        entry.position("usdc")
    with pytest.raises(KeyError):
        entry.position("missing")
    assert entry.erc20_assets == (entry.asset("usdc"), entry.asset("weth"))


def test_the_policy_must_cover_a_position_identifier() -> None:
    with pytest.raises(ValueError, match="assets must exactly cover the policy inventory"):
        PortfolioManifest(policy=policy(), chains=(position_chain(),))
    with pytest.raises(ValueError, match="assets must exactly cover the policy inventory"):
        PortfolioManifest(
            policy=position_policy(inventory={f"{CHAIN_ID}:{ACCOUNT}": ("native",)}),
            chains=(position_chain(),),
        )


def test_a_position_currency_must_be_a_declared_fungible_asset() -> None:
    with pytest.raises(ValueError, match="currency must be a declared fungible asset"):
        position_chain((position(currency0_id="dai"),))
    entry = position_chain((position(currency0_id="native", currency1_id="usdc"),))
    assert entry.asset(entry.positions[0].currency0_id).is_native
    with pytest.raises(ValueError, match="currency must be a declared fungible asset"):
        position_chain((position(),), assets=(native_asset(), token_asset()))


def test_a_position_must_not_reuse_a_fungible_identifier() -> None:
    with pytest.raises(ValueError, match="cannot reuse a fungible asset identifier"):
        position_chain((position(asset_id="usdc"),))


def test_position_identities_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique position asset identifiers required"):
        position_chain((position(), position(asset_id="lp-position")))
    with pytest.raises(ValueError, match="one manifest position per position manager"):
        position_chain((position(), position(asset_id="other-position")))
    second = position(asset_id="other-position", position_manager=OTHER_TOKEN, token_id=TOKEN_ID)
    entry = position_chain((position(), second))
    assert entry.asset_ids[-2:] == ("lp-position", "other-position")


def test_a_position_requires_deployed_contracts_and_canonical_hashes() -> None:
    with pytest.raises(ValueError, match="requires deployed contracts"):
        position(position_manager=ZERO_ADDRESS)
    with pytest.raises(ValueError, match="requires deployed contracts"):
        position(state_view=ZERO_ADDRESS)
    with pytest.raises(ValueError, match="requires deployed contracts"):
        position(pool_manager=ZERO_ADDRESS)
    with pytest.raises(ValueError, match="requires two distinct pool currencies"):
        position(currency1_id="usdc")
    with pytest.raises(ValueError):
        position(pool_id="0x3c")
    with pytest.raises(ValueError):
        position(state_view_runtime_keccak="0xdf")
    with pytest.raises(ValueError):
        position(token_id=0)
    with pytest.raises(ValueError):
        position(position_manager_runtime_keccak="0xde")
    with pytest.raises(ValueError):
        position(pool_manager_runtime_keccak="0xe1")


def test_manifest_rejects_a_policy_account_it_does_not_cover() -> None:
    extra = dict(policy().inventory)
    extra[f"{CHAIN_ID}:{OTHER_ACCOUNT}"] = ("native",)
    with pytest.raises(ValueError, match="exactly cover the policy inventory"):
        manifest(policy_=policy(inventory=extra))


def test_manifest_rejects_an_asset_set_that_differs_from_the_policy() -> None:
    with pytest.raises(ValueError, match="assets must exactly cover the policy inventory"):
        manifest(policy_=policy(inventory={f"{CHAIN_ID}:{ACCOUNT}": ("native",)}))


def test_duplicate_asset_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="unique asset identifiers required"):
        chain(assets=(native_asset(), token_asset(asset_id="native")))
    with pytest.raises(ValueError, match="one manifest asset per on-chain address"):
        chain(assets=(native_asset(), native_asset(asset_id="gas")))


def test_native_asset_requires_decimals_18_and_no_runtime_hash() -> None:
    with pytest.raises(ValueError, match="native asset requires decimals 18"):
        ManifestAsset(
            asset_id="native",
            address=ZERO_ADDRESS,
            decimals=6,
            runtime_keccak=None,
            role="cash",
            feed=feed(),
        )
    with pytest.raises(ValueError, match="native asset requires decimals 18"):
        ManifestAsset(
            asset_id="native",
            address=ZERO_ADDRESS,
            decimals=18,
            runtime_keccak=RUNTIME,
            role="cash",
            feed=feed(),
        )


def test_erc20_asset_requires_a_runtime_hash() -> None:
    with pytest.raises(ValueError, match="ERC-20 asset requires a runtime hash"):
        token_asset(runtime=None)


def test_inventory_requires_two_distinct_providers() -> None:
    with pytest.raises(ValueError, match="two distinct providers"):
        chain(primary="rpc_a", secondary="rpc_a")


def test_inventory_rejects_a_zero_account_and_unknown_provider_alias() -> None:
    with pytest.raises(ValueError, match="account must be nonzero"):
        chain(account=ZERO_ADDRESS)
    with pytest.raises(ValueError):
        chain(primary="rpc a")


def test_inventory_requires_exactly_one_native_asset() -> None:
    with pytest.raises(ValueError, match="exactly one native gas asset"):
        chain(assets=(token_asset(), token_asset(asset_id="dai", address=OTHER_TOKEN)))


def test_feed_must_be_a_deployed_contract_with_a_usd_description() -> None:
    with pytest.raises(ValueError, match="deployed contract"):
        feed(address=ZERO_ADDRESS)
    unusable = token_asset(feed_=feed(address=TOKEN_FEED, description="USDCX"))
    with pytest.raises(ValueError, match="explicit USD feed description"):
        chain(assets=(native_asset(), unusable))


def test_manifest_rejects_unknown_fields_noncanonical_hashes_and_roles() -> None:
    with pytest.raises(ValueError):
        ManifestAsset.model_validate(
            {
                "asset_id": "usdc",
                "address": TOKEN,
                "decimals": 6,
                "runtime_keccak": RUNTIME,
                "role": "cash",
                "feed": {
                    "address": NATIVE_FEED,
                    "description": "ETH / USD",
                    "runtime_keccak": RUNTIME,
                    "max_age_seconds": 300,
                },
                "unexpected": 1,
            }
        )
    with pytest.raises(ValueError):
        token_asset(runtime="0xab")
    with pytest.raises(ValueError):
        token_asset(role="equity")


def test_unknown_lookups_raise_instead_of_returning_a_default() -> None:
    entry = chain()
    with pytest.raises(KeyError):
        entry.asset("dai")
    with pytest.raises(KeyError):
        entry.asset_by_address(OTHER_TOKEN)
    with pytest.raises(KeyError):
        manifest().chain(f"1:{ACCOUNT}")


def test_shipped_base_example_binds_the_owner_limits_and_the_verified_feed_pin() -> None:
    """Pin the shipped Base example so a stale pin fails here, not at capture time.

    The example is configuration, so a wrong account, provider alias, feed address,
    feed runtime hash or loss limit must break this test rather than silently
    redefine what an operator's reconciliation run would trust. The example is
    Base-only and native-only on purpose: it declares no ERC-20 because no
    retained Base USDC/USD feed evidence exists, and that gap belongs in the
    inventory, not in a fabricated pin.
    """
    built = PortfolioManifest.model_validate_json(BASE_EXAMPLE.read_bytes())
    assert built.keys == (f"8453:{BASE_ACCOUNT}",)
    entry = built.chain(built.keys[0])
    assert (entry.primary_source, entry.secondary_source) == (
        "BASE_RPC_PRIMARY",
        "BASE_RPC_SECONDARY",
    )
    assert entry.erc20_assets == ()
    assert entry.native.asset_id == "eth" and entry.native.role == "cash"
    assert entry.native.usd_feed(entry.chain_id) == UsdFeedPolicy(
        8453, BASE_FEED, ZERO_ADDRESS, "ETH / USD", BASE_FEED_RUNTIME, 3600
    )
    assert (
        built.policy.total_capital_usd_micros,
        built.policy.daily_loss_usd_micros,
        built.policy.cumulative_loss_usd_micros,
    ) == (500_000_000, 200_000_000, 500_000_000)
    assert built.policy.inventory == {built.keys[0]: ("eth",)}
    assert built.policy.max_skew_seconds == 0  # a single chain cannot skew against itself
