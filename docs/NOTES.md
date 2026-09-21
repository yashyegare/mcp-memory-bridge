# Engineering notes — failures found and fixed

The README covers what the project does. This file covers what *broke* —
because the debugging stories are the most transferable part of the project.
Each was found live, in this codebase, and each generalizes beyond MCP.

## 1. Stale spawned servers (config ≠ running processes)

**Symptom.** Claude Desktop's connector panel showed the memory tools, but
every call errored — while the same server worked perfectly under the raw
client at the same moment.

**Diagnosis.** Server-side forensics (`MCP_DEBUG_LOG` recorder) showed the
tool calls never arrived. Checking process creation times against the config
file's mtime gave it away: every `claude.exe` process predated the config
edit. Desktop had loaded the new config but kept running server subprocesses
from the **old spawn spec** (old env, old paths). Closing the window on
Windows only minimizes to tray — "restarted" wasn't.

**Fix.** Kill the spawned server processes (or fully quit the host from the
tray) after any config change. Config is read at spawn time; it does not
propagate to already-running subprocesses.

**Generalizes to.** Any host-managed subprocess: config changes are not
hot-reloads. If a client works under one harness and fails under another,
compare what each harness actually spawned before blaming the server.

## 2. Cross-thread SQLite crash (thread-affinity, load-dependent)

**Symptom.** Tool calls intermittently failed with `sqlite3.ProgrammingError:
SQLite objects created in a thread can only be used in that same thread` —
only under concurrent load, never in quiet testing.

**Diagnosis.** The MCP SDK dispatches tool calls onto different worker
threads. Python's `sqlite3` forbids sharing a connection across threads by
default. It failed *sometimes* because early low-traffic calls happened to
land on the connection-creating thread — a latent bug that scheduling
visibility turned into a flaky one.

**Fix** (commit `cb94544`). `check_same_thread=False` plus one server-wide
lock (`_SERVER_LOCK`, an `RLock`) serializing every tool call. With writes
already serialized, a read/write lock split would buy concurrency this
workload doesn't need.

**Generalizes to.** "Works when tested, breaks under load" is almost always
a latent ordering/threading assumption, not a resource limit. A server-side
recorder that logs args + traceback for every call — regardless of what the
host does with the process's stderr — turned this from a heisenbug into a
five-minute diagnosis.

## 3. The undrained stderr wedge (it happened twice)

**Symptom, first time.** Under sustained lock contention, a client saw 13
consecutive read timeouts; its server had stopped responding entirely — but
only when its stderr was captured, never when it went to a terminal.

**Mechanism.** The host captured stderr through a pipe but only read it
after the process exited. mcp 2.x logs every tool error to stderr; past
~64 KB, the OS pipe buffer fills and the server **blocks on its next stderr
write, forever**. Every subsequent call times out. Real MCP hosts (Claude
Desktop included) drain stderr continuously — which is exactly why polished
tooling never shows this failure.

**Fix, part one.** The client now drains stderr on a daemon thread (bounded
tail retained for post-mortems).

**Symptom, second time — different cause, same mechanism.** Months of the
fix holding, then a latency benchmark hung at 190 s. The culprit wasn't
error volume this time: sentence-transformers' tqdm progress bars write to
stderr on **every `encode()` call**, and ~200 warm-up writes filled the same
undrained pipe with progress-bar refreshes. Every standalone probe had used
`capture_stderr=False`, which is why the bug kept "proving" it didn't exist.

**Fix, part two.** `show_progress_bar=False` on encode, and the client
drains stderr by default (`capture_stderr=False` remains for zero-copy
stress runs).

**Generalizes to.** A captured-but-not-consumed stream is a hidden coupling
between your logging volume and your liveness. Same lesson as a full disk
from logs — except the pipe buffer fills in seconds. The structural fix is
to drain, not to hope the volume stays low.

## 4. Honorable mention: the import-order env footgun

`HF_HUB_OFFLINE=1` was set at runtime to stop Hugging Face Hub's freshness
checks from hanging tool calls on a flaky network — but the setting happened
*after* `huggingface_hub` had already been imported, and the library had
snapshotted its config at import time. Setting `os.environ` after the import
was a silent no-op, and the load went online anyway. Now the cache is
detected by **filesystem** (no HF import involved), the env var is set
*before* any HF import, and fresh machines without the cache stay online so
the one-time download still works.
