## Purpose
Behavior contract for the MCP HTTP transport (`serve-mcp --transport http`):
authenticated MCP tool requests must complete with a well-formed JSON
response on the threaded HTTP server, including under concurrency, for
every tool the dispatch layer supports.

## ADDED Requirements

### Requirement: Authenticated MCP HTTP requests complete successfully
When the HTTP MCP transport is served through the CLI entry point
(`digital-twins serve-mcp --transport http`), a POST to `/mcp` carrying a
valid bearer auth header (service, personal, or session token) and a JSON body
`{"tool": <name>, "args": {...}}` SHALL return HTTP 200 with the JSON body
produced by the dispatch layer for the requested tool. The connection SHALL
NOT be reset by the server, and no request-handling thread SHALL terminate
with an uncaught exception on a db-touching request path (auth verify, role
resolution, audit write).

#### Scenario: Valid service-token request returns the tool payload
- **WHEN** the server is started via the CLI with a default-opened state
  DB, and a client POSTs `/mcp` with a valid service bearer header and
  `{"tool": "kb_health", "args": {}}`
- **THEN** the client receives HTTP 200 with a JSON body matching the
  `kb_health` dispatch payload (not a reset/empty connection)

#### Scenario: 401 path remains unchanged
- **WHEN** a client POSTs `/mcp` with no Authorization header (or an
  unknown token)
- **THEN** the client receives HTTP 401 with the unauthorized error body,
  as before

#### Scenario: Known-but-dead auth still fails closed
- **WHEN** a client presents an auth value the authenticator accepts the
  shape of but that resolves to a dead/removed account
- **THEN** the client receives HTTP 403 and no tool is executed

### Requirement: The threaded HTTP server tolerates the CLI default connection
The transport SHALL be operable with the connection exactly as the CLI's
`serve-mcp` opens it (the state DB's default connection, i.e. the
thread-identity guard enabled). The transport layer, not the caller, is
responsible for making db access safe on request-handler threads: the
transport SHALL provide its own thread-safe state-DB access for the
authenticator, role resolution, and the dispatch audit path, while
leaving the connection semantics of every other consumer of the state DB
unchanged.

#### Scenario: Handler built the CLI way handles a valid request
- **WHEN** a handler is wired to a default-opened state DB connection
  (the shape the CLI produces) and a valid service-token request is
  POSTed through a real `ThreadingHTTPServer` on an ephemeral port
- **THEN** the request completes with HTTP 200 and no
  `sqlite3.ProgrammingError` (thread-identity violation) is raised in
  any handler thread

#### Scenario: Other consumers connection semantics are unchanged
- **WHEN** the scheduler loop, web server, or stdio transport open the
  state DB through their existing entry points
- **THEN** their connection creation and threading behavior are
  unchanged from before this change (the transport-local fix does not
  alter shared state-DB connection semantics)

### Requirement: Concurrent valid requests do not corrupt shared state
Concurrent POST requests to `/mcp` that carry valid auth tokens SHALL
complete without interleaving corruption of the shared db state: db-
touching sections of a request (auth verify, role resolution, dispatch
audit write) SHALL be serialized across handler threads. The
serialization SHALL NOT extend to the KB tool bodies' external-service
I/O (Qdrant / Neo4j / LLM), so the threaded server retains its
concurrency benefit for that work.

#### Scenario: Parallel valid requests all complete
- **WHEN** N concurrent clients POST valid service-token requests to
  `/mcp` against the running threaded server
- **THEN** every request receives a well-formed JSON response and no
  request observes interleaved/corrupt db state

#### Scenario: Concurrent requests do not serialize external I/O
- **WHEN** two requests invoke KB tools whose bodies perform external
  (non-state-DB) I/O at the same time
- **THEN** the external I/O sections may overlap (the request lock is
  not held across tool-body network calls), while state-DB writes remain
  serialized

## MODIFIED Requirements
(none)

## REMOVED Requirements
(none)
