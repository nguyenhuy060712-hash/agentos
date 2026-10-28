---
name: doi-chieu-cong-no
description: Dùng khi cần đối chiếu công nợ phải thu, phải trả với khách hàng hoặc nhà cung cấp từ sổ sách, file Excel, soạn thư xác nhận công nợ và nhắc nợ. Use when the user wants to reconcile receivables or payables, compare ledgers, find differences, or draft a debt confirmation or payment reminder (đối chiếu công nợ, nhắc nợ, receivables reconciliation).
compatibility: Cần skill xlsx để đọc sổ và máy tính ảo hoặc môi trường chạy mã của Agent OS.
metadata:
  review: ke-toan
---

# Đối chiếu công nợ

**Mẫu tham khảo — cần người am hiểu pháp lý/kế toán xem trước khi dùng chính thức.** Kết quả chỉ để hỗ trợ; số liệu cuối cùng do kế toán xác nhận.

## Thông tin cần có
- Đối tượng đối chiếu (khách hàng hoặc nhà cung cấp) và kỳ đối chiếu (từ ngày đến ngày).
- Hai nguồn số liệu: sổ của mình và sổ/bảng kê của đối tác, hoặc sổ của mình để làm thư xác nhận. Tệp ở `/tmp/`, đọc bằng skill `xlsx`.
- Số dư đầu kỳ nếu không có trong tệp.

## Cách làm
1. Đọc cả hai nguồn, chuẩn hóa cột: ngày, số chứng từ, diễn giải, phát sinh nợ, phát sinh có. Tính bằng pandas, không tính nhẩm.
2. Ghép từng dòng theo số chứng từ (nếu không có thì theo ngày và số tiền). Sau đó tính: số dư đầu kỳ, tổng phát sinh nợ, tổng phát sinh có, số dư cuối kỳ của mỗi bên.
3. Liệt kê chênh lệch thành bảng: chứng từ có ở bên này mà thiếu ở bên kia, cùng chứng từ nhưng khác số tiền, khác kỳ ghi nhận, trùng lặp. Nguyên nhân thường gặp ở [mẫu đối chiếu](references/mau-doi-chieu.md); ghi rõ là "có thể do", đừng khẳng định.
4. Xuất file Excel (skill `xlsx`): trang "Tổng hợp", trang "Chênh lệch". Nếu cần thư xác nhận hoặc nhắc nợ, soạn thêm bản nháp Word hoặc email theo mẫu.
5. Thư nhắc nợ: lịch sự, nêu số hóa đơn, số tiền, hạn đã quá, đề nghị thanh toán hoặc phản hồi trước một ngày cụ thể. Không đe dọa.
6. Trả lời ngắn: số dư hai bên, chênh lệch, số dòng chênh lệch, việc cần làm tiếp, và nhắc kế toán rà soát lại trước khi gửi cho đối tác.

## Lưu ý
- Không bịa số dư hay chứng từ. Dữ liệu thiếu thì liệt kê ra.
- Tiền ghi `1.200.000 đ`, ngày `23/10/2026`.
- Chỉ gửi thư xác nhận khi người dùng đã duyệt.

## Thử ngay
> Đối chiếu công nợ tháng 9 với khách hàng Công ty An Phát: file so-cua-minh.xlsx và file khach-gui.xlsx đính kèm. Cho mình bảng các khoản chênh lệch và bản nháp thư xác nhận.
