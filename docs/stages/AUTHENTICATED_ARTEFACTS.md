# Stage: authenticated artefacts and native runtime acceptance

The previous local API exempted all owner-pack URLs from session authentication.
That exception is removed. Desktop and browser downloads now send the session
credential in a header, never in a URL, and verify size, media type and SHA-256
against the API's ETag. Browser CORS exposes only the integrity header.

The native process saves a validated document into a newly created private
0700 directory with a 0600 file. PDFs open through the system viewer; HTML files
are revealed in the file manager rather than automatically executed in a browser.
The browser client downloads a validated Blob instead of navigating to an
unauthenticated URL. Tokens are not embedded in files. Privileged IPC requires
the trusted main frame, and unexpected new windows are denied.

A hash verifies bytes, not whether a PDF or HTML document is harmless. The local
copy remains unencrypted and may remain in the OS temporary directory until OS
cleanup. This is not the encrypted-storage/retention work from the broader audit.
The generated HTML retains its original contents and is not claimed sanitised.

## Verification

Unit tests exercise authenticated requests, unsafe origins and identifiers,
MIME/type checks, digest and size mismatches, chunked limits and private unique
cache files. An API integration test proves that unauthenticated requests fail
and the allowed browser origin can read the authenticated integrity receipt.

CI additionally launches the actual Electron entry point on macOS against a
fresh synthetic FastAPI database. CDP checks the app:// origin, rendered DOM,
preload bridge, disabled renderer Node access, authenticated snapshot access and
the real PDF-opening IPC. It records a synthetic screenshot and JSON receipt.
This is runtime proof only after the macOS job succeeds, not a notarised installer
or acceptance on Daniel's Mac, and not a live bank/model test.

## Primary reference

Electron contributors. (n.d.). *shell*. Retrieved September 7, 2026, from
https://www.electronjs.org/docs/latest/api/shell
