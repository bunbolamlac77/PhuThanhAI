# artifacts/

Generated QA evidence lives here on the machine that produced it.

`artifacts/qa/` is **git-ignored on purpose**. The files record absolute
`/Users/...` paths, the local model snapshot path, and model descriptions of
private footage. Publishing them to a public repository would leak both the
operator's filesystem layout and the content of personal video.

Regenerate the full S00 evidence set locally at any time:

```bash
python3 tools/run_s00_gate.py --source /path/to/footage.mp4
```

The frame smoke needs real footage and has **no default path**, because a media
path is private operational data. Pass `--source` or set `PHUTHANHAI_SMOKE_SOURCE`.

That writes `artifacts/qa/S00/{gate.md,gate.json,doctor.json,doctor_deep.json,tests.json,pytest.xml,vlm_smoke.json,ram_repeats.json,server_e2e.json,network_check.json,orphans.json,device_metrics.json}`.

Nothing in the application depends on these files; they are proof, not runtime data.
Runtime data belongs in `~/Library/Application Support/PhuThanhAI/`.
