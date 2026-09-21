# The 60-second demo

Script for a screen recording. Terminal only, no slides. Record with any
screen recorder, or `pip install asciinema` for terminal-native capture.
Do one dry run — everything below is verified to work, but the *first*
`memory_set` in step 3 loads the embedding model (~10s) if the cache-cold
window applies; either warm it beforehand or let the pause ride and narrate
it ("first call pays the model load").

Terminal setup before recording: one window in the repo root, venv python
on deck, and `memory.db` from the live runs still present.

---

**0. Open** (~5s)

```
type README.md | more          # quick scroll past the badge — interviewers skim
cls
```

> "This is a shared-memory MCP server I built between my hand-rolled MCP
> client and Claude Desktop — no SDK in the client, raw JSON-RPC over stdio."

**1. The audit trail is the spine** (~15s)

```
venv\Scripts\python.exe tools\inspect_memory.py --db memory.db
```

> "Everything is attributed. Claude Desktop's writes, my raw client's
> writes, per-client activity — all in one SQLite file."

```
venv\Scripts\python.exe tools\inspect_memory.py --db memory.db --key live/color
```

> "This key was contested by both clients. The log shows every write in
> order — this is how I debug 'who overwrote what'."

**2. Semantic recall** (~20s)

```
venv\Scripts\python.exe tools\inspect_memory.py --db memory.db --searches
venv\Scripts\python.exe -X utf8 -c "import sys; sys.path.insert(0, 'client'); from raw_client import RawMCPClient; c = RawMCPClient([sys.executable, 'server/memory_server.py', 'memory.db'], env={'MCP_CLIENT_ID': 'demo'}); c.initialize(); print(c.call_tool('memory_search', {'query': 'what does the user like their screens to look like', 'top_k': 2}).get('content')[0]['text']); c.close()"
```

> "Search by meaning, not key. The query shares no words with the stored
> fact — cosine 0.43 versus 0.04 for the distractor. That's an 80MB local
> embedding model, no vector database, one SQLite table."

**3. The concurrency story** (~15s)

```
venv\Scripts\python.exe tools\lock_demo.py
```

Wait for the FAIL line.

> "With a zero busy-timeout, a contended write is rejected in 20
> milliseconds with the lock holder identified. Flip one env var and it
> queues instead. The bug this project caught — an undrained stderr pipe
> wedging a server under sustained errors — is in the README."

**4. Close** (~5s)

```
venv\Scripts\python.exe -m pytest tests\ --tb=no -q
```

> "37 integration tests, real subprocesses, real wire protocol. Lint and a
> three-Python-version matrix run on every push, and the lean server
> ships as a Docker image."
