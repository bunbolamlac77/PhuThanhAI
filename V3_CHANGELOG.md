# PhuThanhAI V3.0 — Những thay đổi so với V2.0

| Lĩnh vực | V2.0 | V3.0 | Giá trị / đánh đổi |
|---|---|---|---|
| Trọng tâm | Video Analyzer + clip notes | **Review-first Universal Media Library** | Không đợi AI vẫn xem/ghi chú được hàng trăm clip |
| UI | Gradio 4 tabs | **FastAPI + React TS/Vite UI + SwiftUI launcher mỏng** | Giải quyết virtualization/timeline/inline note tốt hơn nhưng cần bước build frontend |
| Xem video | Preview + proxy khi cần | **Một active player + HTTP Range + lazy proxy bounded LRU** | Ít cache, ít RAM và đĩa, vẫn cần test H265/ProRes thực tế |
| Ghi chú | Theo project/clip/time range | Autosave revision-aware, conflict handling, source vs wall clock, provenance | Không mất/ghi đè note khi đổi clip nhanh hoặc crash |
| Sampling | Quét hai lượt, temporal event | Evidence refinement có benchmark, **single-frame fallback** | Không cam kết multi-frame video khi runtime MLX chưa chứng minh |
| Cache | Re-analysis tối thiểu | Deterministic invalidation DAG (notes vs raw evidence vs narrative) | Sửa note/report không chạy lại inference |
| Định dạng | V2 ProjectBundle + AnnotationSet | **Giữ nguyên V2**, thêm WorkspaceExtension + WorkspacePackage V3 | Import ngược V2, không thêm field phá `additionalProperties: false` |
| Chất lượng | Gate G0–G8 + rubric | Test set đa thể loại + semantic checks + p95 user workflows | Chấm được bằng bằng chứng, không điểm đẹp mang tính tuyên bố |
| RAM | Slot model lớn | ResourceGovernor memory pressure + mode Eco + metrics + worker reap | Phức tạp tăng có kiểm soát, bảo vệ M1 Pro 16GB |
| POV | 3 scripts + voice/render | Dựng preview có thể chỉnh + cache theo beat/shot | Không phải làm lại toàn bộ video khi sửa 1 câu |
| Bảo mật | Offline localhost | API origin/host/auth + asset ID access + path traversal tests | Hạn chế browser-to-localhost và lộ media files |

## Quyết định loại bỏ khỏi MVP (cố ý)

- Không chạy AI router 0.6B thường trực; dùng scheduler code xác định.
- Không đưa thêm model ASR/STT, nhận diện khuôn mặt, nhận diện danh tính, smart face tracking trước G5.
- Không tạo proxy cho toàn bộ video ngay lúc scan; không tự sao chép video 4K.
- Không xây 23 bảng SQLite hoặc hàng chục JSON-RPC method; API và subprocess JSONL là đủ.
- Không phụ thuộc cloud, API key, việc đăng TikTok hay trending data realtime.
- Không code POV engine trước khi gate Analyzer G0–G5 PASS.

## 5 rủi ro V3 cần kiểm định sớm

1. **Chi phí migration UI từ Gradio sang React**: S00 prototype/Spike đo chức năng player, offline bundling, WebView; chỉ 1 frontend để giảm nợ kỹ thuật.
2. **Multi-frame Qwen3-VL trên `mlx-vlm` đã cài** chưa được chứng minh. G0 kiểm single frame; G3 benchmark 2/4 frames hoặc fallback.
3. **Preview ProRes/HDR/VFR** có thể không phát trực tiếp trong WKWebView. Proxy on-demand và PTS mapping bắt buộc.
4. **SQLite note autosave khi crash** cần kiểm transactional persistence/rapid switching; không chấp nhận "đã lưu" trước khi commit.
5. **Báo cáo AI có thể dài**; AI-friendly và portable tách riêng. Token budget phải tôn trọng timeline, không cắt bỏ footage cuối.

## Đánh giá

V3 là **đặc tả kiến trúc tối ưu hơn V2**, không phải tuyên bố ứng dụng hiện tại đạt 100/100. Chỉ số nội dung, performance và stability phải lấy từ Mac thực tế, người dùng review footage thật và log kiểm thử.

## S05.4 — Smart analysis (partial)

- SmartInferencePlanner (smart/balanced/full) over verified S02 manifests, with
  per-frame selection/drop reasons and explicit partial visual coverage.
- Per-frame cache keyed by source/frame hash, model/runtime, prompt and generation
  settings; survives batch/order changes and rejects corrupt entries.
- Inspector AI mode/progress/ETA/In-Out/cancel/resume; no notes/player changes.
- Measured real clip: Smart 2 frames / 22.45 s vs Full 3 frames / 39.19 s;
  frame-cache rerun 2/2 hits. Governor refusal (`MEMORY_PRESSURE_RISING`) recorded.
- Not completed: persistent-worker A/B, adaptive-resolution/token matrix,
  human-labelled quality/event recall. G5 still NOT VERIFIED.

## S05.4R — Performance/correctness closure (partial)

- Hard frame budgets; scan cache metric restoration/determinism; plan invalidation.
- VideoToolbox scan default with fallback; real 12.1 s clip cold sampling 21.2 → 6.5 s.
- Corrected one-pass benchmark: cold/warm sampling and end-to-end timing; Smart
  2/3 frames, 23.92 s end-to-end vs Full 32.86 s; cache rerun 2/2.
- Execution status separated from visual coverage.
- Still not done: adaptive matrix, persistent worker, per-frame durable commit,
  labelled quality/endurance. GATE FAIL; G5 NOT VERIFIED.

