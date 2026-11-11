# PhuThanhAI V3.0 — QA tài liệu và hợp đồng dữ liệu

> **Ngày:** 2026-11-11 • **Loại QA:** kiểm tra offline của bộ đặc tả/schema/fixtures bằng Python trong môi trường tạo tài liệu. **KHÔNG phải QA ứng dụng trên MacBook Pro M1**.

## Đã kiểm

- [x] Có đúng bốn tài liệu lõi `00_MASTER_ARCH`, `01_SPEC_VIDEO_ANALYZER`, `02_SPEC_POV_PIPELINE`, `03_DATA_CONTRACT`.
- [x] Có hai schema V2 legacy giữ nguyên cấu trúc: `AnalysisReport/1.0.0`, `ProjectBundle/1.0.0`.
- [x] Có schema V3 mới: `WorkspaceExtension/1.0.0`, `WorkspacePackage/1.0.0`, resolve `$ref` cục bộ.
- [x] `golden_v2.project_bundle.json` qua structural validator.
- [x] `golden_v3.workspace_package.json` qua structural + semantic validator.
- [x] Fixture `invalid_v3.cross_project.json` bị semantic validator từ chối.
- [x] Fixture `invalid_v3.out_of_bounds_span.json` bị semantic validator từ chối.
- [x] Golden `.md` portable có checksum đúng và roundtrip byte canonical JSON.
- [x] Sửa checksum sai làm import `.md` thất bại.
- [x] `tools/validate_specs.py` không phụ thuộc network; dùng local JSON schemas và golden fixtures.

## Chưa kiểm và phải làm trên máy đích

- [ ] Chưa chạy FastAPI/React/SwiftUI ứng dụng thật.
- [ ] Chưa chạy Qwen3-VL, Qwen3-8B, Kokoro hoặc FFmpeg encode trên Mac M1 Pro 16 GB.
- [ ] Chưa đo RAM, swap, memory pressure, benchmark 1,000–5,000 assets, UX thực tế.
- [ ] Chưa kiểm Spotlight/.app, quyền truy cập user folder, WKWebView, codec ProRes/H.265, HTTP Range, proxy mapping.
- [ ] Chưa kiểm quality descriptions đa thể loại/video thật, các sự kiện bị bỏ sót và tiếng Việt/giọng miền Nam.

**Kết luận:** Bộ đặc tả V3 và fixture đã được kiểm tra ở mức schema/semantics mẫu; **chưa** thể khẳng định ứng dụng 100/100 hoặc bất kỳ gate G0–G8 PASS.
