# The Feed — NFT Launch Readme

This document is the canonical reference for the NFT launch of **The Feed**.
It captures the contract spec, the public mint surface in Django, the media
pipeline that backs each token, the deploy / post-deploy checklist, and the
known weaknesses to monitor in production.

The dev-focused Foundry workspace lives at [`contracts/`](contracts/README.md).

---

## 1. Collection at a glance

| Field | Value |
| --- | --- |
| Name / symbol | `The Feed` / `FEED` |
| Standard | ERC-721 (Enumerable, EIP-2981) |
| Total supply | **1000** (hard cap, immutable) |
| Allocation | Lazy Fisher-Yates random draw at mint time |
| Per-tx cap | 10 |
| Per-wallet cap | 50 |
| Royalty | 7.00% (configurable) via EIP-2981 |
| Owner | Gnosis Safe multisig (post-deploy transfer) |
| Network | Ethereum mainnet (sepolia testnet for soak) |

### Pricing tiers

| Phase | Price | Window |
| --- | --- | --- |
| Finiliar holder | **0.05 ETH** | tokens 1-800, while `FINILIAR.balanceOf(buyer) > 0` |
| Public | **0.07 ETH** | tokens 1-800, all other buyers |
| Last 20% | **0.10 ETH** | tokens 801-1000, everyone (no holder discount) |

The 80% boundary is enforced **per-token inside a batch**: a `mint(10)` call
that crosses token #800 charges the pre-boundary tier for the early tokens
and the last-phase tier for the rest. The contract exposes `quote(buyer, qty)`
for the exact total and `priceFor(buyer)` for the next-token price.

### Revenue ceiling (gross, before gas + royalty)

- Floor (all early tokens minted by Finiliar holders): 800·0.05 + 200·0.10 = **60 ETH**
- Ceiling (no holder discount taken): 800·0.07 + 200·0.10 = **76 ETH**
- Realistic mid-case: **60-70 ETH**

---

## 2. Smart contract — `contracts/src/TheFeed.sol`

### Inheritance

`ERC721Enumerable`, `ERC2981`, `Ownable`, `ReentrancyGuard`, `Pausable`
(OpenZeppelin v4.9.6).

### Public state and views

```solidity
uint256 public constant MAX_SUPPLY         = 1000;
uint256 public constant MAX_PER_TX         = 10;
uint256 public constant MAX_PER_WALLET     = 50;
uint256 public constant LAST_PHASE_TRIGGER = 800;
uint256 public constant HOLDER_PRICE       = 0.05 ether;
uint256 public constant PUBLIC_PRICE       = 0.07 ether;
uint256 public constant LAST_PHASE_PRICE   = 0.10 ether;

IERC721 public immutable FINILIAR;
bool    public saleOpen;
string  public provenance;            // set-once keccak256 of metadata JSON
mapping(address => uint256) public mintedBy;

function totalMinted()                 external view returns (uint256);
function remaining()                   external view returns (uint256);
function priceFor(address)             external view returns (uint256);
function quote(address, uint256 qty)   external view returns (uint256);
```

### Random allocation

`_drawRandomTokenId()` uses lazy Fisher-Yates over `[1, MAX_SUPPLY]`:

1. `index = entropy % _remaining`, where entropy mixes `block.prevrandao`,
   `block.timestamp`, `msg.sender`, the current `_remaining` counter, and the
   previous block hash.
2. The picked id is materialised from a sparse `_swap[]` mapping, the tail is
   moved into the picked slot, `_remaining` decreases by 1.
3. After 1000 draws, `_remaining == 0` and any further `mint()` reverts with
   `"sold out"`.

### Mint flow

```text
mint(qty) →
  require saleOpen, !paused, qty in [1, MAX_PER_TX], remaining ≥ qty
  require mintedBy[msg.sender] + qty ≤ MAX_PER_WALLET
  owed = quote(msg.sender, qty)        // per-token, boundary-aware
  require msg.value ≥ owed
  for i in 0..qty:
      perToken = tier(totalMinted(), isHolder)
      tokenId  = _drawRandomTokenId()
      _safeMint(msg.sender, tokenId)
      emit Minted(msg.sender, tokenId, perToken)
  mintedBy[msg.sender] += qty
  refund (msg.value - owed)            // failure reverts the entire mint
```

### Admin (owner / multisig)

| Function | Purpose |
| --- | --- |
| `setSaleOpen(bool)` | Open / close the sale |
| `pause()` / `unpause()` | Emergency stop |
| `setBaseURI(string)` | Swap to final IPFS CID |
| `setProvenance(string)` | One-shot, set before opening the sale |
| `setRoyalty(addr, bps)` | Update EIP-2981 receiver / bps |
| `withdraw(payable to)` | Sweep contract balance to multisig |

---

## 3. Foundry workspace — `contracts/`

```
contracts/
├─ foundry.toml          solc 0.8.24, optimizer, gas reports
├─ remappings.txt        @openzeppelin + forge-std
├─ Makefile              install / build / test / fuzz / gas / deploy-*
├─ .env.example          FINILIAR_ADDRESS, ROYALTY_*, BASE_URI, RPC keys
├─ src/TheFeed.sol
├─ test/
│  ├─ TheFeed.t.sol      42 tests (unit + boundary + fuzz + reentrancy)
│  └─ mocks/             MockFiniliar, ReentrantReceiver, RejectingReceiver
└─ script/Deploy.s.sol   env-driven deploy + Etherscan verify
```

### Test coverage

```
Ran 42 tests for test/TheFeed.t.sol:TheFeedTest
Suite result: ok. 42 passed; 0 failed; 0 skipped
```

| Group | Coverage |
| --- | --- |
| Constructor | initial state + zero-address guards on FINILIAR / royalty receiver |
| Pricing view | public default, holder discount, last-phase override after 80% |
| Quote helper | linear public/holder, **batch crossing the 800 boundary**, holder span |
| Mint validation | sale closed, paused, qty 0 / >10, wallet cap, insufficient ETH |
| Sold-out | reverts within batch and after 1000 are minted |
| Refund | overpay returns excess; **refund failure reverts the entire mint** |
| Boundary pricing | exact transition at #800; underpaid spanning batch reverts |
| Fisher-Yates | mints all 1000, asserts no duplicates and ids ∈ [1, 1000] |
| Royalty | EIP-2981 default, owner update, non-owner revert |
| Withdraw / metadata | base-URI swap, set-once provenance, owner-only guards |
| Reentrancy | inner `mint()` from `onERC721Received` rejected by `nonReentrant` |
| EIP support | 721, 721-Enumerable, 2981, 721-Metadata interface ids |
| Fuzz | qty round-trip, quote ≤ qty·0.10, quote-vs-loop equivalence |

Run from `contracts/`:

```bash
make install   # OZ v4.9.6 + forge-std v1.9.4
make build
make test      # 42 tests, ~80 ms
make test-ci   # CI profile: fuzz runs = 1000
make gas       # gas report for TheFeed
make snapshot  # writes .gas-snapshot
```

---

## 4. Public mint surface (Django)

All routes live under `djangoscrap/templates/mint/` and are wired in
`djangoscrap/urls.py`.

| URL | Template | Purpose |
| --- | --- | --- |
| `/mint/` | `mint/site.html` | "The Feed" grid: square tiles, hover-reveal title, search, header `Mint` link |
| `/mint/<slug>/` | `mint/composition.html` | Single composition detail; XCOPY-style large title, Detail + Media accordions, "Other works" strip |
| `/mint/random/` | `mint/random.html` | Random-allocation mint UI (1-10 at a time), wallet connect, tiered pricing display |

### `/mint/` (the feed grid)

- Black motif, square tiles only, no rounded corners.
- Header: profile icon + connect-wallet button (top right); the title `The Feed`
  with a red `Mint` link to the right that leads to `/mint/random/`.
- Hover (or focus) on any tile reveals the work title at the bottom, wrapping
  to two lines when needed.
- Tagline: *"When an unsuspecting viewer enters The Feed, a psychic trance
  begins to implant competing identities within >"*.
- Search box with auto-hiding placeholder when typing.
- Hidden from public: media-stale warnings, network/contract pills.

### `/mint/<slug>/` (single composition)

- Two-column layout matching XCOPY work-detail: square live composition on the
  left, compact metadata stack on the right.
- Detail accordion (closed by default):

| Field | Source |
| --- | --- |
| Type | `composition.type` or `Live feed` |
| Supply | `1 of 1000` |
| Standard | `ERC-721` |
| Dimensions | `1080x1080` |
| Storage | `IPFS` |
| Metadata | `Onchain` |
| Contract | `Ethereum` |
| Size | `nft_state.media.collector_45s.size_label` (or `preview_15s` fallback) |

- Media accordion (closed by default): download link for the 45-second
  collector file, last-updated timestamp, and a list of older versions with
  their own download URLs and sizes.
- Footer: `Live work` link, `Opensea` listings link.
- Header is identical to `/mint/` (same wallet menu).

### `/mint/random/` (the actual mint page)

- Header copy: *"1000 total supply. Up to ten works are randomly assigned per
  mint. Finiliar NFT holders mint at 0.05 ETH, public mint is 0.07 ETH, and
  the last 20% (tokens 801-1000) mint at 0.1 ETH."*
- Quantity selector 1-10; default = 1.
- Live ETH total computed against the 0.07 ETH public price for display; the
  contract `quote(buyer, qty)` is the source of truth at submit time.
- Three price-tier cards: Finiliar, Public, Last 20%.
- Connect-wallet button uses `window.ethereum.request({ method: 'eth_requestAccounts' })`.
- No composition previews are shown — the assignment is meant to be a surprise.

### Backend helpers (`djangoscrap/views.py`)

| Helper | Role |
| --- | --- |
| `_mintable_compositions_queryset` | Source of truth for which works can appear in the feed |
| `_nft_public_state` | Builds the per-composition state (URLs, sizes, dates, versions) used by the mint templates |
| `_media_size_label`, `_date_label` | Format helpers for the detail panel |
| `_related_mint_rows` | Random subset of compositions for the "Other works" strip |
| `mint_random_page` view | Returns the count, count_options, and price labels for `/mint/random/` |

---

## 5. NFT media pipeline

Each composition produces a small set of standardised assets registered in
`CompositionMediaAsset` (current renders) and `CompositionNFT` (per-version
snapshots).

| Kind | Use | Specs |
| --- | --- | --- |
| `poster` | OpenSea fallback / square thumbnail | 1080×1080 JPEG q=92 |
| `preview_15s` | `/mint/` grid + OpenSea `animation_url` | 720×720, 10 s, 24 fps, H.264, AAC 64 kbit, < 1 MB |
| `collector_45s` | Owner / collector download | 1080×1080, 45 s, H.264 CRF 23, AAC 128 kbit |

Re-renders are gated by a `sha256` source-signature so the pipeline is a
no-op when nothing changed and automatic when it has. The full operational
spec — capture pipeline, fingerprint payload, storage layout, versioning,
runbook, quality budget, metadata schema, failure modes, roadmap — lives in
[`NFT_MEDIA_GENERATION_PLAN.md`](NFT_MEDIA_GENERATION_PLAN.md).

---

## 6. Deploy checklist

### Pre-deploy (testnet)

1. Fill `contracts/.env`:
   - `FINILIAR_ADDRESS` (Sepolia mock or actual Finiliar contract on the chosen network)
   - `ROYALTY_RECEIVER` (Safe address)
   - `ROYALTY_BPS` (default `700`)
   - `BASE_URI` (placeholder ok at this stage)
   - `DEPLOYER_PRIVATE_KEY`, `SEPOLIA_RPC`, `ETHERSCAN_API_KEY`
2. `make test-ci` — must pass clean with the 1000-fuzz profile.
3. `make deploy-sepolia` — verifies on Etherscan automatically.
4. From the deployer EOA: do not call any owner functions yet; instead transfer
   ownership to the Sepolia Safe immediately to mirror mainnet topology.
5. Soak for at least 1-2 weeks: stress mint, OpenSea preview, marketplace
   listings, royalty enforcement.

### Mainnet deploy

1. Pin metadata + media to **two** providers (Pinata + nft.storage). Optional:
   third copy on Arweave.
2. Compute `keccak256(orderedMetadata.json)` and publish it on the project
   site **before** calling `setSaleOpen(true)`.
3. `make deploy-mainnet` (signed by hardware wallet, slow mode, verify on).
4. Owner sequence (each tx submitted via the Safe):
   1. `setProvenance(<keccak256 hex>)`
   2. `setBaseURI("ipfs://<final-cid>/")`
   3. (Optional) `setRoyalty(safe, 700)` if the constructor value needs adjusting
   4. `setSaleOpen(true)` at the announced launch time
5. Update Django settings:
   - `NFT_ETH_CONTRACT_ADDRESS = "0x..."`
   - `NFT_ETH_CHAIN_ID = 1`
   - `NFT_ETH_NETWORK_NAME = "ethereum"`
   - `NFT_ETH_MARKETPLACE_BASE_URL = "https://opensea.io/assets/ethereum"`
6. Deploy the Django release that surfaces the live address; smoke-test
   `/mint/`, `/mint/<slug>/`, and `/mint/random/` against mainnet RPC.

### Post-launch ops

- Monitor `Minted` events; alert on unusually large per-tx values, refund
  failures, or `Paused` flips.
- Keep `withdraw(safe)` operational and sweep periodically; do **not** leave
  large balances on the contract.
- Track `totalMinted()` against marketing milestones (e.g. announce when the
  last 20% phase opens at token #800).

---

## 7. Known weaknesses and mitigations

| Risk | Status | Mitigation |
| --- | --- | --- |
| `block.prevrandao` is biasable by validators | Accepted for this scale | Swap `_drawRandomTokenId` for Chainlink VRF if secondary value spikes |
| Holder discount uses live `balanceOf` and is loanable | Accepted | If sniping is observed, deploy a Merkle-snapshot variant of `priceFor` |
| Refund failure reverts mint | By design | Documented; `RejectingReceiver` covers it in tests |
| Wallet cap (50) is generous | Configurable via redeploy only | Tighten before deploy if broader distribution is desired |
| Live composition vs frozen edition expectations | Documented in metadata | `description` field must explicitly state the work continues to evolve on-chain reference, even though the snapshot media is fixed |
| Sale toggle / pause keys are owner-controlled | Owner is the multisig | Require ≥2 signers on the Safe; document the recovery procedure |

---

## 8. Pointers

- Media generation plan → [`NFT_MEDIA_GENERATION_PLAN.md`](NFT_MEDIA_GENERATION_PLAN.md)
- Foundry dev guide → [`contracts/README.md`](contracts/README.md)
- High-level system → [`BIG_PICTURE_README.md`](BIG_PICTURE_README.md)
- Hosting + budget → [`PRODUCTION_DEPLOYMENT_PLAN.md`](PRODUCTION_DEPLOYMENT_PLAN.md)
- Contract source → [`contracts/src/TheFeed.sol`](contracts/src/TheFeed.sol)
- Public templates → `djangoscrap/templates/mint/{site,composition,random}.html`
- Backend mint helpers → `djangoscrap/views.py` (`_mintable_compositions_queryset`, `_nft_public_state`, `mint_random_page`)
- NFT media kinds → `djangoscrap/nft_media.py`
