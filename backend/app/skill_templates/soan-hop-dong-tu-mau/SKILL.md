---
name: soan-hop-dong-tu-mau
description: Dùng khi cần soạn bản nháp hợp đồng (mua bán, dịch vụ, thuê, cộng tác viên) từ một mẫu có sẵn, xuất file Word. Use when the user wants to draft a contract from a template, fill in parties and terms, and create a .docx file (hợp đồng, soạn hợp đồng, phụ lục, contract draft).
compatibility: Cần skill docx (Word) và máy tính ảo hoặc môi trường chạy mã của Agent OS.
metadata:
  review: phap-ly
---

# Soạn hợp đồng từ mẫu

**Mẫu tham khảo — cần người am hiểu pháp lý/kế toán xem trước khi dùng chính thức.** Đây chỉ là bản nháp để tiết kiệm thời gian, không phải tư vấn pháp lý.

## Thông tin cần có
Hỏi một lần, gộp lại, bỏ qua những gì đã biết:
- Loại hợp đồng và hai bên: tên, địa chỉ, mã số thuế hoặc CCCD, người đại diện và chức vụ.
- Nội dung chính: hàng hóa/dịch vụ, số lượng, giá, thuế, tiến độ, địa điểm.
- Thanh toán: số tiền, các đợt, hạn, tài khoản.
- Thời hạn hợp đồng, điều kiện chấm dứt, phạt vi phạm (nếu có), luật và nơi giải quyết tranh chấp.

Nếu người dùng đính kèm hợp đồng mẫu của họ, hãy giữ nguyên cấu trúc và chỉ điền hoặc sửa chỗ cần thiết (dùng chức năng sửa của skill `docx`, bật theo dõi thay đổi để người dùng duyệt). Nếu không có mẫu, dùng khung ở [các điều khoản thường gặp](references/dieu-khoan-thuong-gap.md).

## Cách làm
1. Soạn nội dung bằng tiếng Việt đầy đủ dấu, đánh số Điều 1, Điều 2... Mỗi điều ngắn, một ý.
2. Chỗ chưa có thông tin: để dấu `[...]` kèm tên trường, ví dụ `[mã số thuế bên B]`. Tuyệt đối không tự bịa số tiền, ngày, tên.
3. Tạo file Word bằng skill `docx`, tên file dạng `hop-dong-<loai>-<ben-b>`.
4. Trả lời ngắn: các điều khoản đã dựng, danh sách chỗ còn để trống, và nhắc một dòng là cần luật sư hoặc người có chuyên môn xem lại trước khi ký.

## Lưu ý
- Không thêm điều khoản bất lợi hoặc bất thường cho bên nào mà người dùng không yêu cầu.
- Số tiền ghi cả số và chữ. Ngày theo dạng `23/10/2026`.
- Hợp đồng giá trị lớn, liên quan đất đai, lao động, hoặc có yếu tố nước ngoài: nhắc người dùng bắt buộc nhờ chuyên gia.

## Thử ngay
> Soạn nháp hợp đồng dịch vụ thiết kế website giữa Công ty TNHH Hoa Sen (bên A) và anh Nguyễn Văn Nam (bên B, freelancer), giá 25.000.000 đ chia 2 đợt, hoàn thành trong 30 ngày.
