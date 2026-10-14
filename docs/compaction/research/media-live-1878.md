# Live image compaction with GLM 5.3 Flash

> Verification record for the media-handling decision in [strategies.md](../strategies.md#media-handling), measured on 2026-10-14 for [#1878](https://github.com/sokolaidev/maf-extensions/issues/1878) and [#1942](https://github.com/sokolaidev/maf-extensions/pull/1942). This is a narrow image smoke test, not general multimodal qualification.

The completed run passed **14 checks over 10 live model requests**. Two synthetic warehouse images retained all eight planted facts through image description, user-turn summarization and a fresh model answer using only the compacted history. Restoring the JSON session snapshot reproduced the same projected messages without another description or summary request. The [raw evidence](media-live-1878.json) preserves model outputs, usage, checks, fixture hashes and the first attempt's harness failure.

## Setup

The tested source was `d4e968fd3e796bdd5390f0ef294129001cd3541a`. The harness ran on Python 3.13.12 with agent-framework-core 1.21.0, agent-framework-ollama 1.0.0b261008, the Ollama Python client 0.5.3 and Ollama server 0.40.1. Requests went through a local Ollama endpoint to **Ollama Cloud** using `glm-5.3-flash:cloud`; every response identified its model as `glm-5.3-flash`. Inference did not run on the local machine. GPT-6 Luna was not tested.

Each 640 by 384 PNG had a white background, black text and a row of colored rectangles: warehouse A17, 3 red crates, inspection PASSED; warehouse B29, 5 blue crates, inspection PASSED. A three-turn history had a protected head, the image-bearing middle turn, and a protected tail. The single-image histories shared the middle message ID and the text `Read this image.` so changing only the image exercised replay identity isolation. The multiple-image turn placed text before, between and after the images.

The composed strategy used `MediaTokenEstimator(CharacterEstimatorTokenizer())` with its default 1,024-token image cost, a 1,000-token input budget, a 3,000-token summary-input budget, zero minimum band share, four remembered requests and the default user-summary prompt and mode. The hook sent one image and the full turn's text to GLM with instructions to retain its identifier, count, color and inspection result. Summary and recall requests used the same model. Each request had a 1,600-token output limit and a 90-second harness deadline. No sampling or thinking override was supplied.

## Results

| Check | Observed result |
|---|---|
| Default preservation | Full image-bearing turn retained; zero model requests |
| Single image with an opt-in description hook | All four facts retained; original excluded only after accepting the summary |
| Version 4 JSON snapshot and restore | Identical projected messages; zero additional requests; no base64 media payload in the snapshot |
| Different image with the same message ID and text | A new description and summary request; B29 facts retained instead of reusing A17 |
| Two images in a mixed-content turn | Both warehouse descriptions combined; all eight facts retained |
| Fresh live recall from compacted history | A17: 3 red crates, PASSED; B29: 5 blue crates, PASSED |
| Usage reporting | Every response reported positive input usage; 2,896 input and 2,976 output tokens across the completed run |

The single-image history's **local estimate** fell from 1,148 to 198 tokens. This is the estimator's measurement, not provider-billed savings. A separate matched request pair (`Reply with OK.` without and with A17) reported 16 and 340 input tokens: an incremental **324 tokens** for this image. The default 1,024-token media estimate was therefore conservative for this fixture, but this does not establish an upper bound for other dimensions, media or providers. Summed request latency was 23.001 seconds; this was not a throughput benchmark.

## Limits and retained failures

The first attempt completed image compaction, replay, recall and usage probes, then failed a harness assertion that compared the returned model name with the local `:cloud` routing suffix still attached. The corrected assertion checks equality after removing only that suffix; the second full run passed. Both attempts' outputs are retained in the evidence. No package implementation change was required by this live run.

Descriptions were useful but not exact transcriptions. Giving a per-image hook the full mixed turn prompted unnecessary commentary about an unavailable second image, and one description claimed a second warehouse appeared at the top of the layout despite receiving only one image. The first attempt also returned a literal `</think>` delimiter in one description. The final summaries and recall answers kept the planted facts correctly, but hosts should evaluate their description prompts and model output handling for their own content. These observations do not qualify arbitrary description fidelity or resistance to instructions embedded in media.

The run covered inline PNG images, the default user-summary mode and sequential JSON snapshot restoration. It did not verify audio, video, PDFs, hosted files, remote image URLs, concurrent replicas, provider failures, long histories, adversarial media or other summary modes against a live model. Existing unit coverage remains the evidence for failure handling and the other strategy branches. The fixed media estimate remains configurable; this one-image usage measurement does not justify changing its default.
