# Capability: web-app — ingest-run error observability

## ADDED Requirements

### Requirement: Ingest-run failures log a server-side traceback
When a web-triggered ingest run (`POST /api/ingest/run`) raises an
unexpected exception, the web handler SHALL log the full traceback to the
`digital_twins` logger before returning the `500` response. The response
body SHALL remain the generic
`{"error": "ingest run failed: see server log for details"}` (no
traceback leakage in the HTTP response).

#### Scenario: Unhandled exception in a pipeline run
- **WHEN** a pipeline run raises an unhandled exception during
  `POST /api/ingest/run`
- **THEN** the server log contains a traceback line (`Traceback (most
  recent call last):`) followed by the exception type and message, and the
  HTTP response is `500` with the same generic error body.
