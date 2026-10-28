---
name: bao-cao-ban-hang-tu-excel
description: Dùng khi người dùng gửi file Excel/CSV doanh số và cần báo cáo bán hàng tuần, tháng, quý có số tổng, so sánh kỳ trước, top sản phẩm. Use when the user attaches a sales spreadsheet and wants a sales report (báo cáo bán hàng, doanh thu, so sánh kỳ trước, top sản phẩm, sales report from xlsx).
compatibility: Cần skill xlsx (đọc, tính) và máy tính ảo hoặc môi trường chạy mã của Agent OS.
metadata:
  review: none
---

# Báo cáo bán hàng từ Excel

Mục tiêu: biến file số liệu thô thành một báo cáo ngắn mà chủ cửa hàng đọc 2 phút là hiểu.

## Cách làm
1. Tệp người dùng gửi nằm ở `/tmp/<tên tệp>`. Mở bằng skill `xlsx` (lệnh `inspect`, rồi `read`) để xem có những trang tính và cột nào. Đừng giả định tên cột; nếu không rõ cột nào là ngày, sản phẩm, số lượng, doanh thu thì hỏi một câu.
2. Làm sạch trước khi tính: bỏ dòng trống, dòng tổng sẵn có, số lưu dạng chữ (ví dụ "1.200.000"), ngày sai định dạng. Ghi lại bao nhiêu dòng bị bỏ và vì sao.
3. Tính bằng pandas trong máy tính ảo, không tính nhẩm:
   - Tổng doanh thu, số đơn, giá trị đơn trung bình.
   - So với kỳ trước (nếu dữ liệu có): chênh lệch tuyệt đối và phần trăm.
   - Top 5 sản phẩm / khách hàng / chi nhánh theo doanh thu; mặt hàng giảm mạnh nhất.
4. Tạo file Excel kết quả bằng skill `xlsx`: một trang "Tóm tắt", một trang "Chi tiết". Giữ số là số để Excel còn cộng được.
5. Trả lời ngắn theo thứ tự: 3 con số quan trọng nhất; 2-3 nhận xét (cái gì tăng, giảm, bất thường); 1-2 gợi ý hành động. Không dán nguyên cả bảng vào câu trả lời.

## Lưu ý
- Nêu rõ kỳ báo cáo và nguồn (tên tệp). Nếu dữ liệu thiếu ngày hoặc có dấu hiệu trùng đơn, nói thẳng thay vì che đi.
- Doanh thu hiển thị `1.200.000 đ`; phần trăm một chữ số thập phân.
- Không gửi dữ liệu khách hàng ra ngoài; chỉ làm việc trong máy tính ảo.

## Thử ngay
> Đây là file doanh số tháng 9 (đính kèm). Làm báo cáo bán hàng: tổng doanh thu, so với tháng 8, top 5 sản phẩm bán chạy và sản phẩm giảm nhiều nhất.
