# Big Picture Deployment Vision

Last updated: April 2026

This document captures the long-range concept for the composition system so day-to-day decisions can stay aligned with the core artistic and technical direction.

## Core Experience

The target installation is a **3x3 grid of screens**.

Two valid gallery runtime modes:

1. **Full-live wall (current delivery priority)**  
   All 9 screens render full live compositions in sync.
2. **Narrative-center wall (concept mode)**  
   Center thought tile + 8 surrounding associative screens.

Narrative-center remains part of product vision; full-live mode is prioritized
for operational reliability and visual parity in gallery deployment.

## High-Level Behavior Model

Treat the system as **9 synchronized streams** at runtime:

- All screens advance on a shared clock/tick.
- Players schedule swaps against a server-issued `start_at_unix_ms`.
- Drift targets are operationally measured (`<=300ms` target, `>700ms` critical).
- Curatorial logic stays deterministic per tick for easier debugging and replay.

## Associative Image Traversal

Compositions should be selected by weighted association, not simple random choice:

- Hashtag overlap
- Mood fit (arousal, anxiety, tenderness, grief, etc.)
- Novelty/diversity score
- Recency penalties (avoid immediate repeats)
- Narrative continuity constraints

### Suggested Fallback Strategy

1. Exact hashtag + mood match
2. Partial hashtag overlap
3. Related mood cluster
4. Curated safe fallback pool

## Deployment Implications

This vision impacts deployment architecture directly.

- **Local-first runtime (default)**  
  Primary target is fully local execution on the installation machine (or local network), reducing cloud cost/latency and improving reliability during exhibitions.

- **Stateful orchestrator required**  
  A central service tracks current thought state, active tags, per-tile history, and transition timing.

- **Timed state feed required**  
  Each screen must receive `tick_id`, `start_at_unix_ms`, and composition assignment,
  then locally schedule swap at that exact wall-clock boundary.

- **Preload/caching required**  
  Each tile should preload its next composition/video to avoid visual gaps.

- **Media generation strategy**  
  Do not generate final media at playback time. Pre-render assets and sequence them in runtime.

- **Graph/index layer needed**  
  Hashtags are MVP-capable, but long term the system should maintain an association graph with weighted edges.

## Local-First Deployment Profile

Default operating mode is local.

- Run Django app, orchestrator, database, and media storage on the same machine (or LAN-only host)
- Serve all playback clients from local URLs (for example `127.0.0.1` or private subnet)
- Keep media assets and composition renders on local disk/NAS
- Use cloud services only as optional augmentation (for example occasional AI generation), not as core runtime dependencies

### Why Local-First

- Lower operational cost
- Better control during live installations
- Fewer failure points from internet/API outages
- Lower latency for synchronized multi-screen transitions

### Cloud as Optional Layer

If cloud is used, treat it as non-blocking:

- Background enrichment jobs
- Periodic metadata sync/backup
- Optional remote monitoring

Playback and orchestration should continue functioning when offline.

## System Components (Conceptual)

- **Thought Engine**: updates central conscious state
- **Association Engine**: selects next compositions per tile based on weighted rules
- **Orchestrator**: emits timed state updates/instructions
- **Playback Clients**: render each tile and perform smooth transitions
- **Memory Layer**: short-term (anti-repeat) and long-term (motif return)

## Primary Risks and Mitigations

- **Repetition loops**  
  Mitigation: per-tile recent-history blacklist + global diversity scoring.

- **Latency / stutter**  
  Mitigation: preload next assets + crossfade transitions.

- **Sparse metadata graph**  
  Mitigation: enrich ingestion with inferred tags and mood labels.

- **Narrative incoherence**  
  Mitigation: enforce a central scene-state contract that all side threads obey.

## MVP Roadmap

### Phase 1 (Playable Prototype)

- 3x3 layout active
- Center thought updates every 10-20 seconds
- Side tiles select by hashtag overlap
- Basic recency blocking

### Phase 2 (Narrative Coherence)

- Add mood-state vector
- Weighted transition scoring
- Shared scene-state constraints

### Phase 3 (Production Deployment)

- Real-time orchestration (WebSocket/SSE)
- Robust preloading and cache policy
- Monitoring/observability for dropped frames and sync drift
- Failover fallback content queues
- Offline resilience checks (internet disconnected operation)

## Collector + NFT Layer

The detailed launch plan (contract, Foundry test suite, public mint surface,
deploy checklist, known weaknesses) lives in
[`NFT_LAUNCH_README.md`](NFT_LAUNCH_README.md). Summary for this big-picture
doc:

- `The Feed` is an ERC-721 collection with a hard cap of **1000 tokens**,
  random allocation at mint time, and tiered pricing (0.05 / 0.07 / 0.10 ETH).
- Public surface is three Django routes: `/mint/`, `/mint/<slug>/`,
  `/mint/random/`.
- Smart contract source + Foundry tests live in [`contracts/`](contracts/README.md).

### Priority rule

The gallery engine remains the primary product. The NFT layer extends it
without introducing hard dependencies that could break local exhibition
playback — chain integrations stay optional, and the collector dashboard
must continue to work even if blockchain APIs are unavailable.

## Definition of Success

The system is successful when viewers perceive:

- coherent evolution, not random shuffling
- emotional continuity across all active screens
- recurring motifs with variation
- smooth, uninterrupted transitions across all 9 screens

---

Use this file as the "north star" reference whenever implementation trade-offs arise.
