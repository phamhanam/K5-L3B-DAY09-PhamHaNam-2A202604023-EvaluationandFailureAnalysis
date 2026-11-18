# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---:|---:|---:|---:|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | Đạt (PASS) | dùng (monitor) |
| helpful | 100 | 0.375 | 1.000 | 0.300 | Không đạt (FAIL) | loại |

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú / Sửa rubric |
|---|---|---|---|---|---|
| 1 | H-008 (`tone_ok`) | PASS | FAIL | Người | Câu dùng "nha", "check giúp em" thiếu trang trọng; bổ sung danh sách từ ngữ suồng sã vào rubric `tone_ok`. |
| 2 | H-029 (`tone_ok`) | PASS | FAIL | Người | Câu "vui lòng chờ... không cần nhắn thêm" mang sắc thái đuổi khách; thêm quy tắc bắt buộc kiểm tra thái độ từ chối giao tiếp. |
| 3 | H-042 (`tone_ok`) | FAIL | PASS | Người | Câu "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." hoàn toàn lịch sự; làm rõ trong rubric rằng thông báo thời gian hoàn tiền chuẩn không phải lỗi tone. |
| 4 | HL-016 (`helpful`) | PASS | FAIL | Người | Chỉ xin lỗi chung chung ("thành thật xin lỗi... trân trọng sự kiên nhẫn") mà không nêu kết quả xử lý hay bước tiếp theo; rubric `helpful` phải chấm FAIL nếu thiếu hành động/kết quả cụ thể. |
| 5 | HL-035 (`helpful`) | PASS | FAIL | Người | Lặp lại lời xin lỗi nhưng không giải quyết câu hỏi của khách; tách bạch tiêu chí lịch sự (`tone_ok`) khỏi tính hữu ích (`helpful`) trong prompt judge. |
| 6 | HL-015 (`helpful`) | PASS | FAIL | Người | Chỉ báo đã hoàn 55.000đ mà không giải thích căn cứ/phần chênh lệch khi đơn có yêu cầu khác; yêu cầu rubric kiểm tra đủ lý do quyết định. |

## Kết luận
- Tiêu chí bị loại và lý do (dẫn ma trận nhầm lẫn): **`helpful` bị loại** vì `κ = 0.375 < 0.6` và `Recall = 0.300 < 0.7` (`tp=9, fp=0, fn=21, tn=70`). Judge bỏ sót 21/30 ca FAIL thực tế (`fn=21`), làm tỷ lệ lỗi quan sát bị nén xuống `0.09` dù tỷ lệ thực hiệu chỉnh theo Rogan–Gladen là `0.30`. Nếu dùng làm gate, hệ thống sẽ cho lọt các câu trả lời sáo rỗng hoặc thiếu thông tin.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai; cần nhãn người mới để có held-out thật.
