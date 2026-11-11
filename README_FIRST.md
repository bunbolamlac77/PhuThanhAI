# PhuThanhAI V3.0 — Bắt đầu từ đây

**Trạng thái bàn giao:** Đây là **đặc tả kiến trúc, contract và dữ liệu mẫu kiểm thử**, chưa phải mã nguồn ứng dụng đã được triển khai. Thiết kế hướng tới chuẩn nghiệm thu 100 điểm, nhưng chưa được chạy/benchmark trên Mac M1 Pro của chủ dự án.

## 1. Có gì trong bộ ZIP?

```text
PhuThanhAI_V3.0/
├── README_FIRST.md                   # File này — hướng dẫn cho AI Coder
├── IMPLEMENTATION_CHECKLIST.md       # Checklist S00–S08 có gate và tiêu chí PASS
├── V3_CHANGELOG.md                  # V2 → V3: thay đổi, quyết định, giữ tương thích
├── docs/
│   ├── 00_MASTER_ARCH.md             # Kiến trúc + UI + backend + RAM + gates
│   ├── 01_SPEC_VIDEO_ANALYZER.md     # Thư viện / preview / notes / VLM / report
│   ├── 02_SPEC_POV_PIPELINE.md       # POV sau Gate G5, 3 kịch bản/voice/render
│   └── 03_DATA_CONTRACT.md           # Định dạng V2/V3, provenance, timecode
├── contracts/
│   ├── analysis.schema.json          # Legacy V2 AnalysisReport/1.0.0 (copy)
│   ├── project_bundle.schema.json    # Legacy V2 ProjectBundle/1.0.0 (copy)
│   ├── workspace_extension.schema.json
│   └── workspace_package.schema.json
├── tests/fixtures/
│   ├── golden_v2.project_bundle.json
│   ├── golden_v3.workspace_package.json
│   ├── golden_v3.workspace_report_portable.md
│   ├── invalid_v3.cross_project.json
│   └── invalid_v3.out_of_bounds_span.json
└── tools/
    └── validate_specs.py            # Offline schema/semantic/roundtrip checks
```

## 2. Thứ tự đọc cho AI Coder (không đảo)

1. `docs/00_MASTER_ARCH.md` — không được tự ý thay stack/4 tab/giới hạn RAM.
2. `docs/03_DATA_CONTRACT.md` — import được V2, không trộn ghi chú với thông tin AI nhìn thấy.
3. `docs/01_SPEC_VIDEO_ANALYZER.md` — S00–S05 phải hoàn thiện phần thư viện, xem lại, ghi chú và phân tích đa thể loại.
4. `IMPLEMENTATION_CHECKLIST.md` — nhận từng gate, làm xong và test thật mới bước kế.
5. `docs/02_SPEC_POV_PIPELINE.md` — chỉ dùng để định hình interface cho S06–S08; **chưa code engine POV trước G5**.

## 3. Kiểm tra tài liệu/schema — được phép chạy trước code ứng dụng

```bash
cd "/Users/phuthanh/APP_CaNhan/PhuThanhAI"
python3 tools/validate_specs.py
```

Script yêu cầu `jsonschema >=4.18` và `referencing`; không tải từ mạng. Nếu chưa có dependencies, tạo môi trường mới hoặc dùng runtime đã cài tương thích **sau khi G0 kiểm tra**, không sửa môi trường AIVideo.

Lệnh này xác minh **fixtures và schemas**, **không** chứng minh model MLX chạy trên Mac của bạn hay app đã tồn tại. Kiểm tra e2e/benchmark trên Mac là công việc của AI Coder khi triển khai.

## 4. Cài đặt bộ tài liệu (không đụng PhuThanhAI cũ)

Với ZIP `PhuThanhAI_V3.0_Technical_Blueprint.zip` tải vào Downloads:

```bash
mkdir -p "/Users/phuthanh/APP_CaNhan/PhuThanhAI"
unzip -n "$HOME/Downloads/PhuThanhAI_V3.0_Technical_Blueprint.zip" \
  -d "/Users/phuthanh/APP_CaNhan"
```

**Quan trọng:** ZIP chứa thư mục top-level `PhuThanhAI_V3.0`, không phải ứng dụng. Không giải nén thẳng vào PhuThanhAI nếu muốn giữ bản V2 không bị đè. Sau khi backup tài liệu V2, copy nội dung V3 vào `PhuThanhAI/docs`, `contracts`, `tests`, `tools` theo kế hoạch để AI Coder triển khai. Nếu đặt ZIP vào repo hiện tại, không xóa bản V2 trước khi xác minh mọi contract.

**Có thể dùng cách gọn hơn:** giải nén ZIP vào một thư mục `~/Downloads/PhuThanhAI_V3.0`, rồi đưa cả thư mục đó cho DeepSeek và yêu cầu **chỉ thực hiện S00**. Không cần di chuyển source app đang làm.

## 5. Prompt chuẩn gửi DeepSeek

> Bạn là Lead Mac AI Systems Engineer cực kỳ khắt khe. Tôi đang xây **PhuThanhAI V3.0** tại `/Users/phuthanh/APP_CaNhan/PhuThanhAI` trên Mac M1 Pro 16 GB. Đọc trọn `README_FIRST.md`, `docs/00_MASTER_ARCH.md`, `docs/03_DATA_CONTRACT.md`, `docs/01_SPEC_VIDEO_ANALYZER.md` và `IMPLEMENTATION_CHECKLIST.md`; tài liệu POV chỉ dùng để hiểu interface tương lai. Chỉ triển khai **S00**; không tự làm S01 nếu G0 chưa PASS. Sử dụng model đã tải sẵn offline và không thay đổi AIVideo. Viết **toàn bộ file code cụ thể**, test/unit/CLI doctor, lệnh chạy trên máy, kết quả thật và danh sách file. Báo PASS chỉ nếu có logs thực và đo RAM. Nếu thiếu file/code để kiểm tra, yêu cầu READ_FILE cụ thể, không bịa test. Giữ hợp đồng V2/V3, note provenance, 4 tab, 1 heavy-model slot. Mẫu kết quả: `MILESTONE / CHANGED FILES / COMMANDS RUN / TEST RESULTS / DEVICE METRICS / GATE PASS|FAIL|NOT VERIFIED / NEXT STEP`.

## 6. Bốn quyết định đã khóa ở V3

- **UI:** FastAPI + React/TypeScript/Vite compiled local, SwiftUI launcher mỏng; không vừa xây React vừa xây Gradio. Có fallback browser nội bộ khi WKWebView lỗi.
- **Model:** VLM Qwen3-VL 4B/LLM Qwen3-8B/Kokoro reuse snapshots read-only; scheduler lần lượt; không thêm agent AI quản lý thường trực.
- **Data:** V2 `AnalysisReport` + `ProjectBundle` GIỮ NGUYÊN; V3 `WorkspaceExtension`/`WorkspacePackage` mới; note user và raw evidence ở hai lớp.
- **Process:** Thư viện/preview/note được dùng trước AI; Phase 1 xong G0–G5 mới viết POV G6–G8.

## 7. Điều kiện được gọi là bàn giao hoàn chỉnh

App chạy offline trên Mac đích, người dùng mở từ Spotlight và xem/ghi chú hàng trăm clip không treo, có file MD dễ đọc ChatGPT; worker AI tự quản RAM; crash không mất note; import V2 & V3 không hỏng; ghi rõ limitation; có kết quả benchmark/golden video dataset, mã nguồn và log. **Không được gán 100/100 chỉ từ việc viết tài liệu.**
