# Settlement in eight published multihop transactions

An independent offline pass followed each receipt's ERC-20 transfers to the sender, executor and other recipients. The two largest transactions repeatedly traded a fixed amount through the same venues. In both, the last round still produced a positive token difference, but neither the receipts nor the paper report the incremental gas that the last round required. The eight targeted cases come from the first published shard on each of four networks. They do not estimate how often such transactions are profitable. [Retained receipt identities, log-index matches and fee sources](PUBLISHED_MULTIHOP_RECEIPTS_20261127.json)

## Cash recipients and fees

The transaction prefix and chain identify a unique full hash in the linked record. Token deltas below are in the asset's integer base units. “Combined” adds the sender's observed network fee to the executor or sender's observed cash delta, valuing WETH and ETH at par. Ownership of those addresses, other native transfers, financing arrangements and payments outside the transaction remain unverified. USDC is shown in its own currency, with six decimals.

| Chain and transaction | Swap events | Destination and receipt-observed asset delta | Network fee in ETH wei | Combined balance change in ETH wei before other costs |
| --- | ---: | --- | ---: | ---: |
| Optimism `0x88ebc448` | 142 | Sender +698,641 USDC raw | 64,884,518,946,208 | Unpriced |
| Arbitrum `0x83ca4b80` | 72 | Executor +2,041,225,902,218,061 WETH wei | 816,820,300,000,000 | +1,224,405,602,218,061 |
| zkSync Era `0xaa50b106` | 12 | Executor +71,326,107 USDC raw | 2,211,157,000,000,000 | Unpriced |
| zkSync Era `0x11546c17` | 5 | Executor +799,372,509,227,515 WETH wei | 1,097,563,500,000,000 | −298,190,990,772,485 |
| Arbitrum `0xf8848156` | 7 | Sender +2,293,501 USDC raw | 608,966,600,000,000 | Unpriced |
| Optimism `0xea7db973` | 7 | Executor +13,376,111,183,776,015 WETH wei | 437,907,132,152,276 | +12,938,204,051,623,739 |
| Ethereum `0x03e34cf4` | 6 | Executor +7,411,241,106,439,455 ETH wei from wrapping and unwrapping | 6,339,401,488,810,000 | +1,071,839,617,629,455 |
| Ethereum `0x4ab736ec` | 12 | Executor +59,322,227,284,729,913 WETH wei | 52,385,671,202,650,884 | +6,936,556,082,079,029 |

**Optimism, 71 two-swap rounds.** In every round the sender transferred 32,000,000 USDC raw to the executor and received exactly that amount back. The executor held the differences until log 994, when it sent the sender a further 698,641 raw. The 71 FRAX links have exact transfers through the intermediate holder. The 70 transitions between rounds each reset principal. Round gains fell from 14,987 to 5,050 USDC raw. A pool independently transferred 6,400 USDC raw per round to `0xf1c268942a4462fdc45c28a0a1661c493bb3d108`. That pool-side outflow is already reflected in the measured sender receipts. The two Optimism transactions have two agreeing receipts each. Their historical network fee uses the original legacy transaction gas price and the separately reported L1 fee. For the 142-event case the computation is `11,260,240 × 1,100,000 + 52,498,254,946,208` wei.

**Arbitrum, 24 three-swap rounds.** All 48 within-round transfers match through a holder. Every round sent 0.29 WETH and returned more than 0.29 WETH. None of the 23 successive outputs equals the following input. The 6.96 WETH input sum measures turnover. The gross gain per round declined monotonically from 161,978,689,092,396 to 8,142,108,721,424 wei. The aggregate difference remained at executor `0x004ed4cf52d2e15d2dd376a6fc2b1508b9942699`. Network cost was `8,168,203 × 100,000,000` wei, which includes the L1 component. In the separate seven-swap creation transaction, the original sender paid 114,062,500 USDC raw to the newly created contract at log 1 and received 116,356,001 raw at log 37. Looking only at the created contract gives zero settlement balance.

## Shared custody on zkSync Era

**Twelve swaps form three four-swap rounds.** Executor `0xaf748f4876f8d150890210af62bb82786e37db7b` sent 145,000,000 USDC raw at logs 10, 30 and 50. Custody address `0x621425a1ef6abe91058e9712575dcc4258f8d091` returned 189,479,062, 167,578,558 and 149,268,487 raw at logs 28, 48 and 68. For each round, OT, WETH and USD+ each moved in the exact adjacent swap quantity from custody to executor and back. The first round's receipt log pairs are 12→14, 18→20 and 24→26. Adding 20 or 40 gives the remaining six pairs. Each pair has zero intervening executor transfers in that asset. This recovers all nine links under shared custody. Attribution to individual pools' internal balances would require the vault's accounting. The executor gained the 71,326,107 USDC raw lost by the vault. The sender's system-token prepayment less two refunds independently gives the 2,211,157,000,000,000 wei fee at logs 8, 9 and 70.

**Five swaps show a loss after the observed network fee.** In a second transaction the executor sent 25,690,000,000,000,000 WETH wei at log 2 and received 26,489,372,509,227,515 at log 44. The missing LUSD links are visible in custody transfers. One full amount goes pool→router→executor→router→next pool at logs 11, 18, 21 and 22. Another goes pool→router→vault at logs 30 and 36, before the matching swap event at 37. Native settlement, WETH minting and onward payment appear at logs 38–44. The separate sender's system-token prepayment less refunds at logs 0, 1 and 47 equals the receipt gas charge. Deducting both the transfer-ledger debit and receipt fee would charge it twice.

## Evidence boundaries and reproduction

On Ethereum the six-swap executor deposited 230,000,000,000,000,000 wei into WETH at log 195 and withdrew 237,411,241,106,439,455 at log 215. Adding the WETH transfer surplus again would count the same cash twice. The 12-event case has a WETH difference between swaps 33 and 41 equal to the executor's WETH gain. A second gap of 11,289,183,953,784 COMP raw remains between swaps 41 and 44. The receipt at log 43 transferred the whole first COMP amount directly into the next venue. The venue's event reports the smaller amount. Evidence for the venue's internal accounting and transaction-wide native transfers remains open.

The local capture is under `reports/research/multichain-inventory-20261127/`. Rebuild the published amount and fee fields with the command below. To reproduce the recipient balances, group each successful receipt's three-topic, one-word ERC-20 `Transfer` by emitting token address. Debit topic 1, credit topic 2 and retain the original `logIndex`. Compare both sender and executor, plus mint/burn, WETH deposit/withdrawal and zkSync native-ledger entries as described above. Match each paper entry by transaction hash and `source_row`. Arbitrum and zkSync each have one non-null receipt per case and a block header containing the hash at the recorded position. Ethereum has one receipt per case and lacks the independently checked in-block position in this capture. This pass had no call traces, pre/post account snapshots or full receipt trie reconstruction. [Offline receipt verifier](fixtures/replay_published_multihop_receipts.py) · [Pinned paper shard identities](fixtures/replay_published_multihop_shards.py)

```sh
.venv/bin/python docs/verification/fixtures/replay_published_multihop_receipts.py reports/research/multichain-inventory-20261127
```
