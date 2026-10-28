---
name: cong-van-nd30
description: Dùng khi cần soạn công văn, thư mời, tờ trình, văn bản hành chính theo thể thức Nghị định 30/2020/NĐ-CP, xuất file Word. Use when the user wants an official letter or administrative document in Vietnamese government format (công văn, văn bản hành chính, Nghị định 30/2020, official letter, V/v).
compatibility: Cần skill docx (Word) và máy tính ảo hoặc môi trường chạy mã của Agent OS.
metadata:
  review: phap-ly
---

# Soạn công văn theo Nghị định 30/2020/NĐ-CP

**Mẫu tham khảo — cần người am hiểu pháp lý/kế toán xem trước khi dùng chính thức.** Thể thức và cách đánh số cụ thể còn tùy quy chế văn thư của từng cơ quan, doanh nghiệp.

## Thông tin cần có
- Cơ quan/doanh nghiệp ban hành (và cơ quan chủ quản nếu có), địa danh.
- Gửi ai (cơ quan, đơn vị, cá nhân), nội dung cần đề nghị hoặc thông báo, căn cứ nếu có.
- Người ký: chức vụ và họ tên. Số và ký hiệu văn bản (nếu chưa có thì để `[số]/CV-[viết tắt]`).
- Nơi nhận khác (nếu có).

## Cách làm
1. Dựng bố cục theo [thể thức công văn](references/the-thuc-nd30.md): quốc hiệu và tiêu ngữ, tên cơ quan, số và ký hiệu, địa danh và ngày tháng, trích yếu "V/v ...", kính gửi, nội dung, chữ ký, nơi nhận.
2. Nội dung: mở đầu nêu lý do hoặc căn cứ, thân bài nêu đề nghị rõ ràng theo từng ý, kết bằng câu đề nghị phối hợp và lời cảm ơn. Văn phong trang trọng, ngắn gọn, không viết tắt tùy tiện.
3. Tạo file Word bằng skill `docx`. Lưu ý helper mặc định dùng font Noto Sans; thể thức yêu cầu font Times New Roman cỡ 13-14, nên nhắc người dùng đổi font và căn lề khi hoàn thiện.
4. Chỗ chưa biết giữ dạng `[...]`. Không tự bịa số văn bản, ngày, tên người ký.
5. Trả lời ngắn: tóm tắt nội dung, danh sách chỗ còn để trống, và nhắc một dòng là cần người có chuyên môn (văn thư/pháp chế) xem lại.

## Thử ngay
> Soạn công văn của Công ty TNHH Minh Phát gửi Sở Công Thương TP. Đà Nẵng, đề nghị xác nhận hồ sơ đăng ký hội chợ thương mại tháng 12/2026. Người ký: Giám đốc Trần Thị Mai.
