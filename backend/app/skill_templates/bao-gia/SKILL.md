---
name: bao-gia
description: Dùng khi cần lập báo giá gửi khách (hàng hóa, dịch vụ, thi công) thành file Word hoặc Excel. Use when the user asks to create a quote, quotation or price offer (báo giá, bảng giá, chào giá, quote, invoice-style price list) for a customer.
metadata:
  review: none
---

# Lập báo giá

Mục tiêu: ra một bản báo giá gọn, đúng số, có thể gửi khách ngay.

## Thông tin cần có
Chỉ hỏi những gì người dùng chưa nói, gộp thành MỘT câu hỏi:
- Tên khách hàng (và người liên hệ).
- Danh sách hạng mục: tên, đơn vị tính, số lượng, đơn giá.
- Thuế VAT (mặc định 8% hoặc 10% tùy mặt hàng; hỏi nếu không rõ), chiết khấu nếu có.
- Thời hạn hiệu lực báo giá (mặc định 15 ngày), điều kiện thanh toán, thời gian giao hàng.

Nếu cửa hàng đã có bảng giá trong tệp đính kèm hoặc trong bộ nhớ, lấy giá từ đó; không tự bịa đơn giá.

## Cách làm
1. Tính từng dòng: thành tiền = số lượng x đơn giá. Cộng tổng, trừ chiết khấu, rồi cộng VAT. Tự kiểm lại phép tính một lần nữa trước khi xuất file.
2. Viết số theo kiểu Việt Nam: `1.200.000 đ`, ngày `23/10/2026`. Ghi số tiền bằng chữ ở dòng tổng cộng.
3. Dùng skill `docx` để tạo file Word (báo giá gửi khách), hoặc skill `xlsx` nếu người dùng muốn bảng Excel để tự sửa. Mặc định tạo Word.
4. Bố cục: tiêu đề "BÁO GIÁ", thông tin bên gửi và bên nhận, bảng hạng mục (STT, tên, ĐVT, SL, đơn giá, thành tiền), dòng tổng, điều kiện (hiệu lực, thanh toán, giao hàng), lời kết và chỗ ký.
5. Trả lời ngắn: tổng tiền, hạn hiệu lực, và những giả định bạn đã dùng (ví dụ "đã tính VAT 10%").

## Lưu ý
- Giá và chiết khấu là quyết định của chủ cửa hàng. Nếu thiếu giá, hỏi lại, đừng đoán.
- Đừng ghi cam kết nào người dùng chưa nói (bảo hành, tiến độ).

## Thử ngay
> Lập báo giá cho Công ty An Phát: 20 bộ bàn ghế văn phòng giá 1.850.000 đ/bộ, 2 tủ hồ sơ giá 2.400.000 đ/cái, giao trong 7 ngày, VAT 10%, hiệu lực 15 ngày.
