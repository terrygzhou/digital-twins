# Feature Gap — digital-twins vs. nashsu/llm_wiki

> Working doc, 2026-11-14. Compiled from a shallow clone of
> `nashsu/llm_wiki` (kept at `.llm_wiki-research/`) against the current
> digital-twins codebase (`digital_twins/`, v0.15.0).
>
> **License note:** `nashsu/llm_wiki` is **GPL-3.0**; digital-twins is MIT.
> Anything ported from the repo into digital-twins is a copyleft obligation
> for the merged artifact. Prefer *inspiration-level* reuse over code reuse.
>
> Scope discipline (AGENTS.md): this doc is **analysis only**. Nothing here
> is a new BR; owner decisions Q1–Q10 are locked and out of scope.

---

## 1. Shared ground (not a gap)

- Chunked + embedding-based semantic search; cross-references between docs;
  a "wiki"-style layer derived from raw material.
- Multiple input formats: PDF, Office docs, EPUB, web clips, folders, URLs
  (llm_wiki via MinerU/local/cloud parsing; digital-twins via 9 source types
  incl. fs/imap/session).
- BYO LLM endpoint (llm_wiki: OpenAI-compatible, per-project routing;
  digital-twins: `KB_LLM__ENDPOINT`).
- MCP exposure of the KB to agents.

## 2. Digital-twins advantages (keep / don't regress)

- **User-supplied backends** (Qdrant + Neo4j, per-service Docker-or-cloud
  endpoint config) vs. llm_wiki's file/SQLite-only store.
- **Multi-user**: accounts, roles, MCP ACL, per-endpoint auth — llm_wiki is
  single-user desktop.
- **Service-ized, headless, PyPI-distributed** with spec-kit governance —
  llm_wiki is a personal desktop app (Tauri, GPL-3.0).
- **Deterministic, dedup-safe ingest** (one point, not four — NFR-1/14) —
  llm_wiki's two-step CoT ingest is LLM-driven and non-deterministic.

## 3. Feature gaps in digital-twins vs. llm_wiki

Priority legend: **P0** = big-value / low-risk; **P1** = worth it if
resources free up; **P2** = nice-to-have or strategic (likely out of scope
for digital-twins).

| # | Feature (llm_wiki) | Gap detail | Priority | License / effort note |
|---|---|---|---|---|
| G-01 | **LLM-authored wiki layer** ("two-step chain-of-thought" ingest: analyze → generate/maintain interlinked markdown pages) | digital-towns is ingest + lookup only; no agent-authored persistent wiki. This is llm_wiki's *core* differentiator. | **P2** (strategic) | New subsystem, not a small feature. Would need its own spec. Design-only note: could sit *on top of* the existing Qdrant/Neo4j truth store. |
| G-02 | **Multimodal image ingestion** — extract embedded images from PDFs, vision-LLM captions, image-aware search results with lightbox + jump-to-source | digital-twins has no image extraction/captioning pipeline at all. | **P1** | Moderate. New source/chunk extension + vision-LLM hook. Design-only. |
| G-03 | **In-app knowledge graph** — 4-signal relevance (direct links, source overlap, Adamic-Adar, type affinity) + **Louvain community detection** + "surprising connections / knowledge gaps" insights | digital-twins writes to Neo4j but has no in-app graph analytics, no community detection, no insight UI. | **P1** | Neo4j-based community detection is feasible (`GdsPageRank` / Louvain Cypher); low-risk analysis layer on existing graph. Design-only. |
| G-04 | **Deep Research** — LLM-optimized search topics, multi-query web search (Tavily / SerpApi / SearXNG), auto-ingest results into wiki | digital-twins has no web-search / deep-research surface. | **P2** | Depends on G-01 (results land in a wiki) or on a new "research" source type. Design-only. |
| G-05 | **Rust-backend chat agent** — tool-using runtime with wiki/source/graph/web retrieval, workspace file generation, shell approval, cancellation, streaming tool events | digital-twins' MCP is a tool server (search/ingest/health); no agent runtime, no tool-use loop. | **P2** | Strategic. The MCP server already exposes the tool surface; the missing piece is a *client-side* agent loop. Out of scope for digital-twins' current BR set. |
| G-06 | **Agent Skills** — scan & enable local `SKILL.md` folders, `/skill` selection, on-demand skill instructions | digital-twins has no skill registry. | **P2** | Trivial to add as a file-scanning feature; low value standalone. Design-only. |
| G-07 | **Persistent ingest queue** — serial processing with crash recovery, cancel, retry, progress visualization | digital-twins' scheduler runs the whole pipeline per schedule; no per-document queue, no retry/cancel granularity. | **P1** | Extends `digital_twins/scheduler/` + `ingest/`; moderate effort. Design-only. |
| G-08 | **Folder import with auto-watch** — recursive import preserving directory structure; folder context as LLM classification hint; source-folder auto-watch for external changes | digital-twins' `fs` source is path-scoped but has no auto-watch / external-change detection. | **P1** | Moderate. Inotify/FSEvents watcher + re-ingest trigger. Design-only. |
| G-09 | **Chrome web clipper** — one-click capture + auto-ingest | digital-twins has no browser extension. | **P2** | Separate repo; pairs well with the `custom` source. Design-only. |
| G-10 | **Local HTTP API** (`127.0.0.1:19828`, JSON API for projects/search/chat) | digital-twins has an internal web server + MCP; the HTTP API shape is different (REST endpoints for a desktop app, not a generic KB API). | **P2** | digital-twins already covers this via web server + MCP; not a real gap. Note for completeness. |
| G-11 | **Async review system** — LLM flags items for human judgment with predefined actions (Create Page / Deep Research / Skip) + pre-generated search queries | digital-twins has no human-in-the-loop review queue. | **P2** | Depends on G-01 / G-05. Design-only. |
| G-12 | **Generated-outputs preview** — agent-created markdown / HTML / images / other workspace files appear as previewable outputs | digital-twins has no "agent produced this file" surface. | **P2** | Couples to G-05. Design-only. |
| G-13 | **Mermaid diagram rendering** in chat and preview | digital-twins' web UI has no Mermaid renderer. | **P2** | Small frontend addition; low standalone value. Design-only. |
| G-14 | **EPUB / MOBI / Org-mode parsing** (via MinerU / local / cloud PDF pipeline) | digital-twins' source set is fs/imap/session — no e-book or Org-mode parser. | **P1** | Moderate. Add `epub` source type + mineru/epub integration. Design-only. |
| G-15 | **Source-grounded retrieval** ("Read Sources Only" mode) | digital-twins' search already chunks raw content; no explicit "sources-only" toggle. | **P1** | Low effort. Flag on the MCP search tool. Design-only. |
| G-16 | **Project management & migration** — export/import complete project archives across devices, rebuild wiki index from existing pages | digital-twins has no portable project archive / cross-device migration. | **P1** | Couples to G-01. Design-only. |
| G-17 | **Per-project model routing** — configure models per project, route Chat and Ingest independently, custom providers / headers / streaming | digital-twins has a single global LLM endpoint; no per-project routing. | **P2** | Medium. Extends `digital_twins/config/`. Design-only. |
| G-18 | **Batch URL import** (with auto-ingest) | digital-twins has no URL source. | **P2** | New source type; pairs with G-04. Design-only. |
| G-19 | **Tavily / SerpApi / SearXNG integrations** (web-search providers) | digital-twins has no web-search provider layer. | **P2** | New dependency; pairs with G-04. Design-only. |
| G-20 | **Obsidian-compatible vault layout** (`.obsidian/`, markdown pages, auto-generated) | digital-twins has no file-based wiki artifact. | **P2** | Couples to G-01. Design-only. |

## 4. Recommended read (no owner sign-off yet — analysis only)

- **P0:** none. Every gap is P1 or P2 under the current BR set.
- **P1 shortlist** (highest value-per-effort if resources open up):
  - **G-02** multimodal image ingestion (moderate, self-contained)
  - **G-03** Neo4j community detection + graph insights (low-risk analysis layer on existing graph)
  - **G-07** persistent ingest queue with crash recovery (extends scheduler/ingest)
  - **G-14** EPUB / MOBI / Org-mode source types (moderate, additive)
  - **G-15** source-grounded retrieval toggle (low effort, cheap)
- **P2 (strategic):** G-01 LLM-authored wiki layer is the single biggest
  conceptual gap. Everything in §3 (G-04, G-05, G-11, G-12, G-16, G-20) is
  downstream of it. If it's ever pursued, it would need its own BR-13 spec.
- **License guard:** treat llm_wiki as *inspiration-level* reference only;
  GPL-3.0 code in → GPL-3.0 out, which conflicts with digital-twins' MIT
  and the PyPI distribution model.

## 5. Evidence

- `nashsu/llm_wiki` source (cloned at `.llm_wiki-research/`):
  - `README.md` — feature list, tech stack, license (GPL-3.0)
  - `llm-wiki.md` — Karpathy design doc (core idea: LLM builds & maintains
    a persistent interlinked wiki; three layers: raw sources → wiki → index)
  - `mcp-server/README.md` — MCP proxies the local HTTP API at
    `127.0.0.1:19828`; token auth
  - `src-tauri/Cargo.toml` — Rust backend, `lancedb = 0.27.2`
- digital-twins source:
  - `digital_twins/` — config / sources / ingest / state / scheduler / mcp /
    web / accounts / auth / user_config / health / cli
  - `requirement.md` — BR-11, NFR-12..18; owner decisions Q1–Q12 locked
  - `AGENTS.md` — portability guards (no host paths, no `python3.12` pin,
    no `CUDA_VISIBLE_DEVICES`)
- digital-twins is MIT (verified: `LICENSE`, `pyproject.toml:license`);
  llm_wiki is GPL-3.0 (verified: `.llm_wiki-research/LICENSE`).
